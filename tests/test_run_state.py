from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from src.models import CommandResult
from src.services.health_checker import HealthChecker
from src.services.question_manager import QuestionManager
from src.services.refinement_loop import RefinementLoop
from src.services.run_state import (
    INTERRUPTIBLE_STATES,
    STATE_LABELS,
    STATE_MARKERS,
    TERMINAL_STATES,
    InvalidRunStateTransition,
    RunState,
    RunStateMachine,
    allowed_transitions,
)


class RunStateTransitionTest(unittest.TestCase):
    def test_a_run_starts_idle(self) -> None:
        machine = RunStateMachine("run-1")
        self.assertIs(machine.state, RunState.IDLE)
        self.assertFalse(machine.is_active)
        self.assertFalse(machine.is_terminal)

    def test_the_consultation_only_path_is_legal_end_to_end(self) -> None:
        """The phases today's loop actually walks through."""
        machine = RunStateMachine("run-1")
        for state in (
            RunState.PREPARING,
            RunState.PLANNING,
            RunState.INTEGRATING,
            RunState.COMPLETED,
        ):
            machine.transition_to(state)
        self.assertIs(machine.state, RunState.COMPLETED)

    def test_the_approval_path_is_legal_end_to_end(self) -> None:
        """Not reachable from the loop yet, but the vocabulary the approval
        gate will be built on must already connect up."""
        machine = RunStateMachine("run-1")
        for state in (
            RunState.PREPARING,
            RunState.PLANNING,
            RunState.WAITING_APPROVAL,
            RunState.IMPLEMENTING,
            RunState.TESTING,
            RunState.REVIEWING,
            RunState.INTEGRATING,
            RunState.COMPLETED,
        ):
            machine.transition_to(state)
        self.assertIs(machine.state, RunState.COMPLETED)

    def test_implementing_is_unreachable_without_passing_through_approval(self) -> None:
        """The whole point of the gate: no path from planning straight to
        write access."""
        machine = RunStateMachine("run-1", RunState.PLANNING)
        with self.assertRaises(InvalidRunStateTransition):
            machine.transition_to(RunState.IMPLEMENTING)
        self.assertIs(machine.state, RunState.PLANNING)

    def test_a_paused_run_resumes_through_approval_not_into_implementing(self) -> None:
        machine = RunStateMachine("run-1", RunState.PAUSED)
        self.assertFalse(machine.can_transition_to(RunState.IMPLEMENTING))
        self.assertTrue(machine.can_transition_to(RunState.WAITING_APPROVAL))

    def test_every_non_terminal_state_can_be_cancelled(self) -> None:
        for state in INTERRUPTIBLE_STATES:
            with self.subTest(state=state):
                machine = RunStateMachine("run-1", state)
                machine.transition_to(RunState.CANCELLING)
                self.assertIs(machine.state, RunState.CANCELLING)

    def test_every_non_terminal_state_can_fail(self) -> None:
        for state in set(RunState) - TERMINAL_STATES:
            with self.subTest(state=state):
                machine = RunStateMachine("run-1", state)
                machine.transition_to(RunState.FAILED)
                self.assertIs(machine.state, RunState.FAILED)

    def test_terminal_states_have_no_way_out(self) -> None:
        for state in TERMINAL_STATES:
            with self.subTest(state=state):
                self.assertEqual(allowed_transitions(state), frozenset())
                machine = RunStateMachine("run-1", state)
                for target in RunState:
                    if target is state:
                        continue
                    with self.assertRaises(InvalidRunStateTransition):
                        machine.transition_to(target)

    def test_a_cancelling_run_cannot_be_cancelled_again(self) -> None:
        self.assertNotIn(RunState.CANCELLING, allowed_transitions(RunState.CANCELLING))

    def test_re_entering_the_current_state_is_a_no_op(self) -> None:
        machine = RunStateMachine("run-1", RunState.PLANNING)
        machine.transition_to(RunState.PLANNING)
        self.assertEqual(len(machine.history), 1)

    def test_history_records_each_move_in_order(self) -> None:
        machine = RunStateMachine("run-1")
        machine.transition_to(RunState.PREPARING)
        machine.transition_to(RunState.PLANNING)
        self.assertEqual(
            [state for state, _ in machine.history],
            [RunState.IDLE, RunState.PREPARING, RunState.PLANNING],
        )
        timestamps = [ts for _, ts in machine.history]
        self.assertEqual(timestamps, sorted(timestamps))


class RunStateSettleTest(unittest.TestCase):
    def test_a_normal_finish_settles_as_completed(self) -> None:
        machine = RunStateMachine("run-1", RunState.INTEGRATING)
        self.assertIs(machine.settle(), RunState.COMPLETED)

    def test_a_cancelled_run_settles_as_cancelled_not_completed(self) -> None:
        """A cancellation landing just before the final answer must not be
        reported to the user as a success."""
        machine = RunStateMachine("run-1", RunState.CANCELLING)
        self.assertIs(machine.settle(), RunState.CANCELLED)

    def test_settle_is_idempotent_and_keeps_the_first_outcome(self) -> None:
        machine = RunStateMachine("run-1", RunState.CANCELLING)
        machine.settle()
        self.assertIs(machine.settle(), RunState.CANCELLED)
        self.assertIs(machine.fail(), RunState.CANCELLED)

    def test_fail_after_a_clean_finish_does_not_rewrite_the_outcome(self) -> None:
        machine = RunStateMachine("run-1", RunState.INTEGRATING)
        machine.settle()
        self.assertIs(machine.fail(), RunState.COMPLETED)

    def test_settle_from_idle_completes_rather_than_hanging(self) -> None:
        machine = RunStateMachine("run-1")
        self.assertIs(machine.settle(), RunState.COMPLETED)


class RunStateConcurrencyTest(unittest.TestCase):
    def test_concurrent_transitions_leave_history_a_single_consistent_chain(self) -> None:
        """The worker thread advances phases while the GUI thread may cancel;
        both must not land in history as if both had applied."""
        machine = RunStateMachine("run-1", RunState.PLANNING)
        outcomes: list[str] = []
        barrier = threading.Barrier(2)

        def attempt(target: RunState) -> None:
            barrier.wait()
            try:
                machine.transition_to(target)
                outcomes.append("ok")
            except InvalidRunStateTransition:
                outcomes.append("rejected")

        threads = [
            threading.Thread(target=attempt, args=(RunState.INTEGRATING,)),
            threading.Thread(target=attempt, args=(RunState.CANCELLING,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        # Both moves are individually legal from `planning`, so both succeed;
        # what must hold is that history is a single consistent chain rather
        # than two interleaved writes.
        self.assertEqual(len(machine.history), 1 + outcomes.count("ok"))
        self.assertIn(machine.state, {RunState.INTEGRATING, RunState.CANCELLING})


class RunStatePresentationTest(unittest.TestCase):
    def test_every_state_has_a_japanese_label_and_a_marker(self) -> None:
        for state in RunState:
            with self.subTest(state=state):
                self.assertTrue(STATE_LABELS[state].strip())
                glyph, colour = STATE_MARKERS[state]
                self.assertTrue(colour.startswith("#"))
                self.assertIsInstance(glyph, str)

    def test_finished_and_idle_tabs_draw_no_marker(self) -> None:
        for state in (RunState.IDLE, RunState.COMPLETED, RunState.CANCELLED):
            self.assertEqual(STATE_MARKERS[state][0], "")

    def test_states_needing_the_user_are_visually_distinct_from_busy_ones(self) -> None:
        approval_glyph, approval_colour = STATE_MARKERS[RunState.WAITING_APPROVAL]
        busy_glyph, busy_colour = STATE_MARKERS[RunState.PLANNING]
        self.assertNotEqual((approval_glyph, approval_colour), (busy_glyph, busy_colour))


class _LabelStub:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, text: str) -> None:
        self.text = text


class ProjectTabRunStateTest(unittest.TestCase):
    """ProjectTab derives `running`/`cancelling` from the machine now, so they
    can no longer disagree with each other."""

    def _tab(self, state: RunState | None) -> "ProjectTab":
        from src.gui.project_tab import ProjectTab

        tab = ProjectTab.__new__(ProjectTab)
        tab.tab_id = "tab-1"
        tab.app = None
        tab.status_label = _LabelStub()
        tab.logged: list[str] = []
        tab.append_log = tab.logged.append
        tab.run_state_machine = None if state is None else RunStateMachine("run-1", state)
        return tab

    def test_a_tab_with_no_run_is_idle_and_not_running(self) -> None:
        tab = self._tab(None)
        self.assertIs(tab.run_state, RunState.IDLE)
        self.assertFalse(tab.running)
        self.assertFalse(tab.cancelling)

    def test_a_cancelling_run_still_counts_as_running(self) -> None:
        """The run slot is still held while subprocesses are torn down, so a
        second run on the same folder must not be allowed to start yet."""
        tab = self._tab(RunState.CANCELLING)
        self.assertTrue(tab.running)
        self.assertTrue(tab.cancelling)

    def test_a_finished_run_is_neither_running_nor_cancelling(self) -> None:
        for state in TERMINAL_STATES:
            with self.subTest(state=state):
                tab = self._tab(state)
                self.assertFalse(tab.running)
                self.assertFalse(tab.cancelling)

    def test_an_illegal_reported_state_is_logged_not_raised(self) -> None:
        """A wiring bug in the loop's phase reporting must degrade the status
        display, not kill the user's run."""
        tab = self._tab(RunState.PLANNING)
        tab.apply_run_state(RunState.IMPLEMENTING)
        self.assertIs(tab.run_state, RunState.PLANNING)
        self.assertTrue(any("run state" in line for line in tab.logged))

    def test_a_legal_reported_state_updates_the_status_label(self) -> None:
        tab = self._tab(RunState.PREPARING)
        tab.apply_run_state(RunState.PLANNING)
        self.assertIs(tab.run_state, RunState.PLANNING)
        self.assertEqual(tab.status_label.text, STATE_LABELS[RunState.PLANNING])

    def test_a_state_arriving_after_the_run_ended_is_dropped(self) -> None:
        tab = self._tab(None)
        tab.apply_run_state(RunState.PLANNING)
        self.assertIs(tab.run_state, RunState.IDLE)


class _ChairAlwaysAvailable:
    def available(self) -> bool:
        return True


class RefinementLoopStateReportingTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_run_with_no_usable_cli_reports_preparing_then_completed(self) -> None:
        """The 'no CLI passed preflight' path still produces a real final
        answer, so it must settle as completed, not failed — and the states it
        reports must be a legal chain for a real RunStateMachine."""
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = _ChairAlwaysAvailable()
        loop.question_manager = QuestionManager()

        async def fake_preflight_all(self, cwd, automation_level, prefer_rtk, progress, cancel_event):
            return (
                {"claude": CommandResult(agent="claude", command=[], ok=False, status="command_missing")},
                [],
            )

        seen: list[RunState] = []
        original_preflight = HealthChecker.preflight_all
        HealthChecker.preflight_all = fake_preflight_all
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            await loop.run(
                Path(tmp_dir.name),
                "test request",
                2,
                None,
                threading.Event(),
                None,
                None,
                seen.append,
            )
        finally:
            HealthChecker.preflight_all = original_preflight
            tmp_dir.cleanup()

        self.assertEqual(seen, [RunState.PREPARING, RunState.COMPLETED])

        machine = RunStateMachine("run-1")
        for state in seen:
            machine.transition_to(state)
        self.assertIs(machine.state, RunState.COMPLETED)

    async def test_a_run_cancelled_before_it_starts_reports_no_phase_at_all(self) -> None:
        """Nothing was sent anywhere, so the tab should settle straight from
        its start state rather than being told it entered `preparing`."""
        loop = RefinementLoop.__new__(RefinementLoop)
        cancel_event = threading.Event()
        cancel_event.set()

        seen: list[RunState] = []
        await loop.run(Path("."), "test request", 2, None, cancel_event, None, None, seen.append)

        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
