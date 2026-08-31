"""What each automation level actually does.

Until 2026-08-31 this module was a label-to-int map and nothing more. The GUI
offered five levels up to "5: 差分確認待ちまで", but only three integers were
ever read: `role_orchestrator` mapped them to 1/2/3 rounds and `cli_adapters`
emitted a warning for level >= 3. Every level ran read-only. Someone choosing
"実装・テストまで" got three rounds of conversation and no edits, with no way
to tell from the UI that implementation was not implemented.

The levels are now three, and each capability below is checked at the point
where it takes effect, so a level cannot promise something the run does not do.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AutomationCapabilities:
    """What a level is allowed to do. Read at the point of effect, never
    inferred from the integer at a call site."""

    level: int
    label: str
    #: Author/Critic/Verifier rotations before anything is decided.
    rounds: int
    #: Whether the chair produces a structured implementation plan.
    builds_plan: bool
    #: Whether the run may reach the approval gate and grant write access.
    #: False here is what keeps `WriteGrant` unreachable for levels 1-2.
    can_implement: bool
    #: Whether the run concerns itself with the project's tests at all.
    #: Whether they are *executed* is a separate question, gated on
    #: `config.RUN_TESTS_AUTOMATICALLY`; while that is off the command is
    #: detected and shown for the user to run themselves.
    runs_tests: bool


LEVEL_CONSULT = 1
LEVEL_PLAN = 2
LEVEL_IMPLEMENT = 3

_CAPABILITIES: dict[int, AutomationCapabilities] = {
    LEVEL_CONSULT: AutomationCapabilities(
        level=LEVEL_CONSULT,
        label="1: 相談のみ",
        rounds=1,
        builds_plan=False,
        can_implement=False,
        runs_tests=False,
    ),
    LEVEL_PLAN: AutomationCapabilities(
        level=LEVEL_PLAN,
        label="2: 実装案まで",
        rounds=3,
        builds_plan=True,
        can_implement=False,
        runs_tests=False,
    ),
    # Defined but not offered. Kept so stored levels resolve and so the
    # write path stays testable; `config.IMPLEMENTATION_WRITES_ENABLED`
    # decides whether it can do anything, and today it cannot.
    LEVEL_IMPLEMENT: AutomationCapabilities(
        level=LEVEL_IMPLEMENT,
        label="3: 実装まで（現在無効）",
        rounds=3,
        builds_plan=True,
        can_implement=True,
        runs_tests=True,
    ),
}

#: Levels the GUI offers. The implementing level is withheld while writes are
#: off: showing a choice that silently does nothing is the failure this module
#: was rewritten to remove.
def _selectable_levels() -> list[int]:
    from src import config

    levels = sorted(_CAPABILITIES)
    if config.IMPLEMENTATION_WRITES_ENABLED:
        return levels
    return [level for level in levels if not _CAPABILITIES[level].can_implement]


#: GUI option-menu values, in order.
AUTOMATION_LEVELS: dict[str, int] = {
    _CAPABILITIES[level].label: level for level in _selectable_levels()
}

DEFAULT_LEVEL = LEVEL_PLAN

#: Saved tabs from before the five-level menu was replaced. The old level 3
#: ("実装案まで") already meant "three rounds, no writes", so it maps to the new
#: level 2 of the same name. Old 4 and 5 claimed implementation they never
#: performed; they map to the level that now really implements, because that is
#: what the user was asking for when they picked them.
_LEGACY_LEVELS: dict[int, int] = {
    1: LEVEL_CONSULT,
    2: LEVEL_PLAN,
    3: LEVEL_PLAN,
    4: LEVEL_IMPLEMENT,
    5: LEVEL_IMPLEMENT,
}

#: Every known label, including ones no longer offered.
_CAPABILITIES_BY_LABEL: dict[str, int] = {
    caps.label: level for level, caps in _CAPABILITIES.items()
}

_LEGACY_LABELS: dict[str, int] = {
    "1: 相談のみ": LEVEL_CONSULT,
    "2: 計画まで": LEVEL_PLAN,
    "3: 実装案まで": LEVEL_PLAN,
    "4: 実装・テストまで": LEVEL_IMPLEMENT,
    "5: 差分確認待ちまで": LEVEL_IMPLEMENT,
    # The label used between 2026-08-31 and the sandbox work, when automatic
    # test execution was withdrawn.
    "3: 実装・テストまで": LEVEL_IMPLEMENT,
}


def label_to_level(label: str) -> int:
    """Resolve a GUI label, including labels saved by an older build.

    A stored level the GUI no longer offers is migrated down to the highest
    one it does, so a tab saved when level 3 implemented reopens as level 2
    rather than sitting on a choice that is not in the menu.
    """
    if label in AUTOMATION_LEVELS:
        return AUTOMATION_LEVELS[label]
    resolved = _LEGACY_LABELS.get(label)
    if resolved is None:
        resolved = _CAPABILITIES_BY_LABEL.get(label, DEFAULT_LEVEL)
    return _to_selectable(resolved)


def _to_selectable(level: int) -> int:
    """The nearest level the GUI actually offers."""
    offered = set(AUTOMATION_LEVELS.values())
    if level in offered:
        return level
    below = [candidate for candidate in offered if candidate < level]
    return max(below) if below else DEFAULT_LEVEL


def level_to_label(level: int) -> str:
    return capabilities_for(level).label


def normalize_level(level: int) -> int:
    """Clamp any integer — including a stored 4 or 5 — onto a real level.

    Does NOT drop to a selectable level: `capabilities_for()` must still be
    able to describe the implementing level for the code paths that remain.
    Use `label_to_level()` for anything driving the GUI.
    """
    if level in _CAPABILITIES:
        return level
    return _LEGACY_LEVELS.get(level, DEFAULT_LEVEL)


def capabilities_for(level: int) -> AutomationCapabilities:
    return _CAPABILITIES[normalize_level(level)]


def grants_write(level: int) -> bool:
    """The single predicate every write-side decision must go through.

    Gate 1 of three. The kill switch is checked here rather than only in the
    capability table so that a stored level from a build where level 3 could
    implement cannot reopen the path.
    """
    from src import config

    if not config.IMPLEMENTATION_WRITES_ENABLED:
        return False
    return capabilities_for(level).can_implement
