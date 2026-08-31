"""Regressions for the two release blockers found in external review.

Both are safety boundaries, so they are tested at the seam rather than through
the GUI: the failures are timing-dependent and would not reproduce reliably
through Tk.
"""

import unittest

from src.services import claude_command
from src.services.write_grant import granted_after_approval
from tests.support import enable_implementation_writes


class ClaudeHooksAreDisabledTest(unittest.TestCase):
    """Blocker 1: plan mode stops the model's tools, not the project's hooks.

    `claude -p` treats the folder as trusted and runs whatever the target
    project's .claude/settings.json declares. A SessionStart hook is arbitrary
    shell with the user's rights, so without --safe-mode a level-1 read-only
    consultation could still rewrite files. Reproduced against the real CLI:
    the hook created a marker file in plan mode with --tools "".
    """

    def setUp(self) -> None:
        enable_implementation_writes(self)

    def _argv(self, grant=None):
        return claude_command.build(
            claude_command.ClaudeRunSpec(prompt="p"), "/bin/claude", grant=grant
        )

    def test_read_only_runs_disable_customizations(self) -> None:
        self.assertIn("--safe-mode", self._argv())

    def test_granted_runs_disable_customizations_too(self) -> None:
        from pathlib import Path

        grant = granted_after_approval(
            run_id="r", agent="claude", project_root=Path("/tmp/p"), approved=True
        )
        self.assertIn("--safe-mode", self._argv(grant))

    def test_safe_mode_precedes_the_mode_flags(self) -> None:
        """So it can never be parsed as an argument to --permission-mode."""
        argv = self._argv()
        self.assertLess(argv.index("--safe-mode"), argv.index("--permission-mode"))

    def test_bare_is_not_used(self) -> None:
        """--bare also skips hooks, but forces ANTHROPIC_API_KEY auth, which
        would move the user onto API-key billing and ignore their login."""
        self.assertNotIn("--bare", self._argv())


class _StubTab:
    """The approval-gate half of ProjectTab, with Tk and threads left out."""

    def __init__(self, owned_run_id: str | None = None):
        from src.gui.project_tab import ProjectTab

        self.tab = ProjectTab.__new__(ProjectTab)
        self.tab._approval_gate = None
        self.tab._approval_dialog = None
        self.tab.append_log = lambda *args, **kwargs: None
        self.tab.active_run = (
            type("Run", (), {"run_id": owned_run_id})() if owned_run_id else None
        )


class StaleApprovalTest(unittest.TestCase):
    """Blocker 2: a dialog left over from a cancelled run could answer
    whichever gate happened to be installed, approving write access for a run
    the user was never shown."""

    def setUp(self) -> None:
        from src.gui.project_tab import ApprovalGate

        self.ApprovalGate = ApprovalGate

    def test_a_superseded_dialog_cannot_answer_the_current_gate(self) -> None:
        tab = _StubTab().tab
        old_gate = self.ApprovalGate("run1")
        tab._approval_gate = old_gate
        new_gate = self.ApprovalGate("run2")
        tab._approval_gate = new_gate

        tab._resolve_approval(old_gate, True, "old dialog")

        self.assertFalse(new_gate.event.is_set(), "run2 must still be waiting")
        self.assertEqual(new_gate.answer, {}, "run2 must not be approved")

    def test_approval_is_refused_when_the_tab_owns_another_run(self) -> None:
        tab = _StubTab(owned_run_id="runOTHER").tab
        gate = self.ApprovalGate("runX")
        tab._approval_gate = gate

        tab._resolve_approval(gate, True, "")

        self.assertFalse(gate.answer["approved"])
        self.assertTrue(gate.event.is_set(), "the worker must not stay blocked")

    def test_the_matching_gate_still_approves_normally(self) -> None:
        tab = _StubTab(owned_run_id="runA").tab
        gate = self.ApprovalGate("runA")
        tab._approval_gate = gate

        tab._resolve_approval(gate, True, "go")

        self.assertTrue(gate.answer["approved"])
        self.assertEqual(gate.answer["feedback"], "go")
        self.assertTrue(gate.event.is_set())

    def test_cancel_before_the_dialog_exists_still_releases_the_worker(self) -> None:
        """The request can sit in the GUI queue while the run is cancelled."""
        tab = _StubTab(owned_run_id="runB").tab
        gate = self.ApprovalGate("runB")
        tab._approval_gate = gate

        tab._close_approval_dialog()

        self.assertTrue(gate.event.is_set())
        self.assertFalse(gate.answer["approved"])
        self.assertIsNone(tab._approval_gate, "the gate must not outlive the run")

    def test_a_worker_only_discards_its_own_gate(self) -> None:
        tab = _StubTab().tab
        newer = self.ApprovalGate("run2")
        tab._approval_gate = newer

        tab._discard_approval_gate(self.ApprovalGate("run1"))

        self.assertIs(tab._approval_gate, newer)

    def test_resolving_twice_is_harmless(self) -> None:
        tab = _StubTab(owned_run_id="runC").tab
        gate = self.ApprovalGate("runC")
        tab._approval_gate = gate

        tab._resolve_approval(gate, False, "")
        tab._resolve_approval(gate, True, "second press")

        self.assertFalse(gate.answer["approved"], "the first answer stands")


if __name__ == "__main__":
    unittest.main()
