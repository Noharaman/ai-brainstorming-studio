"""The plan a human approves, and the answer they give.

The approval gate needs something more specific than the prose final answer:
the user is being asked to let an AI edit their files, so they must see which
files, by which agent, measured against which commit. This module holds those
types and the parsing of the chair's structured plan.

Parsing is forgiving by design. The chair renames and redecorates its own
headings run to run (the same problem `chair_output` exists to absorb), and a
plan that fails to parse must degrade into "show the user the raw text and
still ask" rather than into "silently skip the approval gate".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from src.services.git_checkpoint import GitCheckpoint

#: Headings the chair is asked to produce, with the variants seen in practice.
_SECTION_PATTERNS: dict[str, tuple[str, ...]] = {
    "summary": ("変更概要", "概要", "実装概要", "summary"),
    "files": ("対象ファイル", "変更対象", "編集するファイル", "files"),
    "steps": ("実装手順", "手順", "作業手順", "steps"),
    "tests": ("テスト", "検証", "テスト方法", "tests"),
    "risks": ("リスク", "注意点", "リスク・注意点", "risks"),
}

_HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?\*{0,2}([^\n:：*]+)\*{0,2}\s*[:：]?\s*$")
_BULLET = re.compile(r"^\s*(?:[-*・>]+|\d+[.)]|[（(]\d+[）)])\s*")


@dataclass(frozen=True)
class ImplementationPlan:
    """What the implementer is being authorised to do."""

    summary: str = ""
    target_files: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    raw_text: str = ""
    #: Non-empty lines that appeared before any recognised heading and are
    #: therefore absent from every parsed section.
    dropped_text: str = ""

    @property
    def is_parsed(self) -> bool:
        """Whether anything structured was recovered.

        False means the GUI must fall back to showing `raw_text`; it never
        means the gate can be skipped.
        """
        return bool(self.summary or self.target_files or self.steps)

    @property
    def is_complete(self) -> bool:
        """Whether `render()` shows everything the chair wrote.

        Content under an unrecognised heading is absorbed into the section
        above it, but anything before the *first* recognised heading has no
        section to land in and would silently vanish. Approving a plan while
        part of it is hidden is the one failure this gate cannot tolerate —
        "delete .env" living in a section named something unexpected must not
        disappear from the screen. When this is False the caller must show
        `raw_text` instead.
        """
        return not self.dropped_text

    def render(self) -> str:
        lines: list[str] = []
        if self.summary:
            lines += ["## 変更概要", self.summary, ""]
        if self.target_files:
            lines += ["## 対象ファイル"] + [f"- {p}" for p in self.target_files] + [""]
        if self.steps:
            lines += ["## 実装手順"] + [f"{i}. {s}" for i, s in enumerate(self.steps, 1)] + [""]
        if self.tests:
            lines += ["## テスト"] + [f"- {t}" for t in self.tests] + [""]
        if self.risks:
            lines += ["## リスク・注意点"] + [f"- {r}" for r in self.risks] + [""]
        return "\n".join(lines).strip() or self.raw_text


@dataclass(frozen=True)
class ApprovalRequest:
    """Everything the user must see before granting write access."""

    run_id: str
    plan: ImplementationPlan
    implementer: str
    checkpoint: GitCheckpoint
    project_root: str

    def warnings(self) -> list[str]:
        return self.checkpoint.approval_warnings()


@dataclass(frozen=True)
class ApprovalDecision:
    """The human answer. `approved=False` is the default everywhere."""

    approved: bool = False
    #: Free-text correction when the user rejects but wants another attempt.
    feedback: str = ""
    #: True when the gate was resolved by cancellation or window close rather
    #: than by the user actually deciding. Kept distinct from a plain rejection
    #: so the run can report "cancelled" instead of "rejected".
    cancelled: bool = False


#: How the worker thread asks the human. The GUI implementation posts to the
#: Tk queue and blocks the worker until the main thread answers, the same
#: shape as `ProgressCallback` and `StateCallback`: the worker never touches
#: Tk itself. A caller with no GUI passes nothing, and the run stops at the
#: gate rather than implementing unattended.
ApprovalCallback = Callable[["ApprovalRequest"], "ApprovalDecision"]


@dataclass
class ImplementationOutcome:
    """What the implementation phase produced, for the final report."""

    implementer: str = ""
    attempted: bool = False
    approved: bool = False
    changed_files: tuple[str, ...] = ()
    diff_text: str = ""
    diff_stat: str = ""
    test_command: str = ""
    test_passed: bool | None = None
    test_output: str = ""
    revert_hint: str = ""
    #: How many times the implementer was asked to fix its own failing tests.
    repair_attempts: int = 0
    #: Untracked/ignored files that existed at approval and are now gone.
    lost_paths: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)


#: Diff text kept in chat_rooms.json. The full patch already lives under
#: `.ai-brainstorm/sessions/<id>/`, and the room file is read and rewritten on
#: every turn — letting a megabyte diff into it would make each save slower for
#: the rest of the room's life.
PERSISTED_DIFF_LIMIT = 40_000

_PERSISTED_FIELDS = (
    "implementer",
    "attempted",
    "approved",
    "changed_files",
    "diff_stat",
    "test_command",
    "test_passed",
    "test_output",
    "revert_hint",
    "repair_attempts",
    "lost_paths",
    "notes",
)


def outcome_to_dict(outcome: ImplementationOutcome | None) -> dict | None:
    """Serialize an outcome for the room history.

    Everything stored here comes from the user's own repository, so it goes
    through `secret_redactor` first: a diff can carry a key the AI happened to
    move between files, and this file is long-lived.
    """
    if outcome is None or not getattr(outcome, "attempted", False):
        return None

    from src.services import secret_redactor

    data: dict = {}
    for name in _PERSISTED_FIELDS:
        value = getattr(outcome, name, None)
        if isinstance(value, tuple):
            value = list(value)
        if isinstance(value, str):
            value = secret_redactor.redact(value)
        elif isinstance(value, list):
            value = [
                secret_redactor.redact(item) if isinstance(item, str) else item
                for item in value
            ]
        data[name] = value

    diff = secret_redactor.redact(outcome.diff_text or "")
    if len(diff) > PERSISTED_DIFF_LIMIT:
        diff = diff[:PERSISTED_DIFF_LIMIT] + "\n... (差分が大きいため省略) ...\n"
        data["diff_truncated"] = True
    data["diff_text"] = diff
    return data


def outcome_from_dict(data: dict | None) -> ImplementationOutcome | None:
    """Rebuild a stored outcome. Unknown or missing keys fall back to defaults
    so a room written by an older or newer build still loads."""
    if not isinstance(data, dict) or not data.get("attempted"):
        return None
    outcome = ImplementationOutcome()
    for name in _PERSISTED_FIELDS + ("diff_text",):
        if name not in data:
            continue
        value = data[name]
        if name in ("changed_files", "lost_paths") and isinstance(value, list):
            value = tuple(value)
        if name == "notes" and not isinstance(value, list):
            continue
        setattr(outcome, name, value)
    return outcome


def parse(text: str) -> ImplementationPlan:
    """Recover the structured plan from the chair's markdown."""
    if not text or not text.strip():
        return ImplementationPlan(raw_text=text or "")

    buckets: dict[str, list[str]] = {key: [] for key in _SECTION_PATTERNS}
    preamble: list[str] = []
    current: str | None = None

    for line in text.splitlines():
        section = _match_heading(line)
        if section is not None:
            current = section
            continue
        stripped = line.strip()
        if current is None:
            if stripped:
                preamble.append(stripped)
            continue
        if stripped:
            buckets[current].append(stripped)

    summary = " ".join(buckets["summary"]).strip()
    return ImplementationPlan(
        summary=summary,
        target_files=_as_items(buckets["files"]),
        steps=_as_items(buckets["steps"]),
        tests=_as_items(buckets["tests"]),
        risks=_as_items(buckets["risks"]),
        raw_text=text,
        dropped_text="\n".join(preamble),
    )


def _match_heading(line: str) -> str | None:
    match = _HEADING.match(line)
    if not match:
        return None
    # Normalize so "## **対象ファイル**:" and "対象ファイル" are the same key.
    label = match.group(1).strip().casefold().replace(" ", "")
    for key, variants in _SECTION_PATTERNS.items():
        for variant in variants:
            if label == variant.casefold():
                return key
    return None


def _as_items(lines: list[str]) -> tuple[str, ...]:
    items: list[str] = []
    seen: set[str] = set()
    for line in lines:
        item = _BULLET.sub("", line).strip().strip("`")
        if not item or item in seen:
            continue
        seen.add(item)
        items.append(item)
    return tuple(items)
