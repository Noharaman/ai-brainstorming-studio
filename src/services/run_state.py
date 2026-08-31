"""Explicit run state machine for a single brainstorm run.

Until now a run's progress was two independent booleans on `ProjectTab`
(`running` / `cancelling`) plus free-text progress lines. That is enough to
grey out a button, but it cannot express the states the approval flow needs
(`waiting_approval`, `implementing`, `paused`), and nothing prevented the two
booleans from disagreeing.

This module owns the states and the legal moves between them. It is
deliberately display- and policy-free: it knows nothing about Tk, about which
AI is running, or about what an approval means.

Scope note: `waiting_approval`, `implementing`, `reviewing`, `testing` and
`paused` are defined here but are **not reachable from today's run loop** —
the loop still goes preparing -> planning -> integrating -> completed, because
no Implementer has write access yet. They are declared now so the approval
gate can be built against a settled vocabulary rather than inventing states
mid-implementation.
"""

from __future__ import annotations

import threading
from datetime import datetime
from enum import Enum
from typing import Callable


class RunState(str, Enum):
    IDLE = "idle"
    PREPARING = "preparing"
    PLANNING = "planning"
    WAITING_APPROVAL = "waiting_approval"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    INTEGRATING = "integrating"
    CANCELLING = "cancelling"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


#: How a worker thread reports the phase it has entered. Mirrors
#: `ProgressCallback` / `StatusCallback`: the worker never touches Tk, it hands
#: the state to the GUI queue and the main thread applies it.
StateCallback = Callable[["RunState"], None]


TERMINAL_STATES = frozenset({RunState.COMPLETED, RunState.CANCELLED, RunState.FAILED})

#: States a run can be interrupted from. `cancelling` and `failed` are
#: reachable from every one of these without being listed individually below.
INTERRUPTIBLE_STATES = frozenset(set(RunState) - TERMINAL_STATES - {RunState.CANCELLING})

_FORWARD_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.IDLE: frozenset({RunState.PREPARING}),
    # `completed` directly from preparing is the "no CLI passed preflight"
    # path: it still produces a real final answer, so it is a success, not a
    # failure.
    RunState.PREPARING: frozenset({RunState.PLANNING, RunState.INTEGRATING, RunState.COMPLETED}),
    RunState.PLANNING: frozenset({RunState.WAITING_APPROVAL, RunState.INTEGRATING}),
    RunState.WAITING_APPROVAL: frozenset(
        {RunState.IMPLEMENTING, RunState.PLANNING, RunState.PAUSED, RunState.INTEGRATING}
    ),
    RunState.IMPLEMENTING: frozenset({RunState.TESTING, RunState.REVIEWING}),
    RunState.TESTING: frozenset({RunState.REVIEWING, RunState.IMPLEMENTING, RunState.INTEGRATING}),
    RunState.REVIEWING: frozenset(
        {RunState.PLANNING, RunState.IMPLEMENTING, RunState.INTEGRATING}
    ),
    RunState.INTEGRATING: frozenset({RunState.COMPLETED}),
    # Resuming a paused run re-enters the phase it was interrupted in. It never
    # resumes straight into `implementing` without an approval — the approval
    # gate is re-run, which is why `waiting_approval` is the only write-side
    # target here.
    RunState.PAUSED: frozenset({RunState.PLANNING, RunState.WAITING_APPROVAL}),
    RunState.CANCELLING: frozenset({RunState.CANCELLED}),
    RunState.COMPLETED: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.FAILED: frozenset(),
}

#: Japanese labels for the per-tab status line.
STATE_LABELS: dict[RunState, str] = {
    RunState.IDLE: "待機中",
    RunState.PREPARING: "準備中（スキャン・プリフライト）",
    RunState.PLANNING: "AI相談中",
    RunState.WAITING_APPROVAL: "承認待ち",
    RunState.IMPLEMENTING: "実装中",
    RunState.TESTING: "テスト実行中",
    RunState.REVIEWING: "レビュー中",
    RunState.INTEGRATING: "統合中（LM Studio）",
    RunState.CANCELLING: "キャンセル中",
    RunState.PAUSED: "一時停止",
    RunState.COMPLETED: "完了",
    RunState.CANCELLED: "キャンセル済み",
    RunState.FAILED: "失敗",
}

#: Tab-strip marker: (glyph, colour). An empty glyph draws nothing.
STATE_MARKERS: dict[RunState, tuple[str, str]] = {
    RunState.IDLE: ("", "#8a8a8a"),
    RunState.PREPARING: ("●", "#4a9eff"),
    RunState.PLANNING: ("●", "#4a9eff"),
    RunState.WAITING_APPROVAL: ("◆", "#f5a623"),
    RunState.IMPLEMENTING: ("●", "#f5a623"),
    RunState.TESTING: ("●", "#4a9eff"),
    RunState.REVIEWING: ("●", "#4a9eff"),
    RunState.INTEGRATING: ("●", "#4a9eff"),
    RunState.CANCELLING: ("●", "#8a8a8a"),
    RunState.PAUSED: ("❙❙", "#8a8a8a"),
    RunState.COMPLETED: ("", "#8a8a8a"),
    RunState.CANCELLED: ("", "#8a8a8a"),
    RunState.FAILED: ("✕", "#e05252"),
}


class InvalidRunStateTransition(Exception):
    """Raised when a caller asks for a move the state machine does not allow.

    This is a programming error, not a runtime condition. GUI code that applies
    states arriving from a worker thread should catch it and log, so a wiring
    bug degrades the status display instead of killing the user's run.
    """

    def __init__(self, current: RunState, requested: RunState) -> None:
        super().__init__(
            f"cannot move from {current.value} to {requested.value}"
        )
        self.current = current
        self.requested = requested


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    """Every state reachable from `state`, including the interruption edges."""
    if state in TERMINAL_STATES:
        return frozenset()
    allowed = set(_FORWARD_TRANSITIONS[state])
    allowed.add(RunState.FAILED)
    if state is not RunState.CANCELLING:
        allowed.add(RunState.CANCELLING)
    return frozenset(allowed)


class RunStateMachine:
    """The state of one run, safe to read from the GUI thread while a worker
    thread advances it.

    Bound to a `run_id` so a late transition from a superseded run can be
    rejected by the caller before it ever reaches the machine.
    """

    def __init__(self, run_id: str, state: RunState = RunState.IDLE) -> None:
        self.run_id = run_id
        self._lock = threading.Lock()
        self._state = state
        self._history: list[tuple[RunState, datetime]] = [(state, datetime.now())]

    @property
    def state(self) -> RunState:
        with self._lock:
            return self._state

    @property
    def history(self) -> list[tuple[RunState, datetime]]:
        with self._lock:
            return list(self._history)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        """Whether a worker is still expected to be doing something.

        `cancelling` counts as active: the CLI subprocesses are still being
        torn down and the run slot is still held.
        """
        return self.state not in TERMINAL_STATES and self.state is not RunState.IDLE

    def can_transition_to(self, state: RunState) -> bool:
        return state in allowed_transitions(self.state)

    def transition_to(self, state: RunState) -> RunState:
        """Move to `state`, or raise `InvalidRunStateTransition`.

        Re-entering the current state is a no-op rather than an error: a phase
        that reports progress more than once should not have to track whether
        it already announced itself.
        """
        with self._lock:
            if state is self._state:
                return self._state
            if state not in allowed_transitions(self._state):
                raise InvalidRunStateTransition(self._state, state)
            self._state = state
            self._history.append((state, datetime.now()))
            return state

    def settle(self) -> RunState:
        """Move to whichever terminal state the run actually ended in.

        Called on every worker exit path. A run that was asked to cancel
        settles as `cancelled` even though its last reported phase may have
        completed normally — otherwise a cancellation that landed just before
        the final answer would be reported to the user as a success.
        """
        with self._lock:
            if self._state in TERMINAL_STATES:
                return self._state
            target = (
                RunState.CANCELLED
                if self._state is RunState.CANCELLING
                else RunState.COMPLETED
            )
            self._state = target
            self._history.append((target, datetime.now()))
            return target

    def fail(self) -> RunState:
        """Terminal `failed`, unless the run already settled."""
        with self._lock:
            if self._state in TERMINAL_STATES:
                return self._state
            self._state = RunState.FAILED
            self._history.append((RunState.FAILED, datetime.now()))
            return RunState.FAILED

    def label(self) -> str:
        return STATE_LABELS[self.state]

    def marker(self) -> tuple[str, str]:
        return STATE_MARKERS[self.state]
