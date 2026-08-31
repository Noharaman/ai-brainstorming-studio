import unittest
from pathlib import Path

from src.services import write_grant
from tests.support import enable_implementation_writes
from src.services.cli_adapters import CliAdapters
from src.services.write_grant import (
    WriteGrant,
    WriteGrantError,
    granted_after_approval,
    grant_for,
)


class GrantConstructionTest(unittest.TestCase):

    def setUp(self) -> None:
        enable_implementation_writes(self)
    def test_a_grant_cannot_be_built_without_approval(self) -> None:
        with self.assertRaises(WriteGrantError):
            granted_after_approval(
                run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=False
            )

    def test_a_grant_requires_a_run_and_an_agent(self) -> None:
        for run_id, agent in (("", "codex"), ("r1", "")):
            with self.subTest(run_id=run_id, agent=agent):
                with self.assertRaises(WriteGrantError):
                    granted_after_approval(
                        run_id=run_id,
                        agent=agent,
                        project_root=Path("/tmp/p"),
                        approved=True,
                    )

    def test_a_grant_applies_only_to_its_own_agent_and_run(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        self.assertTrue(grant.applies_to("codex", "r1"))
        self.assertFalse(grant.applies_to("claude", "r1"))
        self.assertFalse(grant.applies_to("codex", "r2"))

    def test_grant_for_narrows_to_none(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        self.assertIs(grant_for(grant, "codex", "r1"), grant)
        self.assertIsNone(grant_for(grant, "claude", "r1"))
        self.assertIsNone(grant_for(grant, "codex", "other"))
        self.assertIsNone(grant_for(None, "codex", "r1"))


class ForgedGrantTest(unittest.TestCase):
    """A grant is only a grant if it came from the approval path.

    `WriteGrant` is a public dataclass, so nothing stops code constructing one
    directly and skipping `granted_after_approval()`'s approval check. The
    builders therefore verify provenance rather than trusting non-None.
    """

    def setUp(self) -> None:
        enable_implementation_writes(self)
        self.forged = WriteGrant(
            run_id="r1",
            agent="codex",
            project_root=Path("/tmp/p"),
            approved_at="now",
        )
        self.adapters = CliAdapters(prefer_rtk=False)

    def test_a_directly_constructed_grant_is_not_authentic(self) -> None:
        self.assertFalse(self.forged.is_authentic)
        real = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        self.assertTrue(real.is_authentic)

    def test_a_forged_grant_does_not_apply_to_anything(self) -> None:
        self.assertFalse(self.forged.applies_to("codex", "r1"))
        self.assertIsNone(grant_for(self.forged, "codex", "r1"))

    def test_a_forged_grant_leaves_every_cli_read_only(self) -> None:
        commands, _ = self.adapters.build_commands(
            {"claude": "p", "gemini": "p", "codex": "p"},
            grant=self.forged,
            run_id="r1",
        )
        self.assertIn("read-only", commands["codex"])
        self.assertIn("plan", commands["claude"])
        self.assertIn("plan", commands["gemini"])

    def test_claude_command_refuses_a_forged_grant_directly(self) -> None:
        """The last builder in the chain re-checks rather than trusting."""
        from src.services import claude_command

        forged_claude = WriteGrant(
            run_id="r1",
            agent="claude",
            project_root=Path("/tmp/p"),
            approved_at="now",
        )
        argv = claude_command.build(
            claude_command.ClaudeRunSpec(prompt="p"), "/bin/claude", grant=forged_claude
        )
        self.assertIn("plan", argv)
        self.assertNotIn("acceptEdits", argv)


class CodexCustomizationsAreDisabledTest(unittest.TestCase):
    """Codex runs hooks too, and they are outside the model sandbox.

    `codex features list` reports hooks, plugins, apps, browser_use and
    computer_use as stable and enabled by default, so `--sandbox read-only`
    does not contain a trusted SessionStart hook — the same hole --safe-mode
    closes for claude.
    """

    def setUp(self) -> None:
        enable_implementation_writes(self)
        self.adapters = CliAdapters(prefer_rtk=False)

    def _codex(self, **kwargs):
        commands, _ = self.adapters.build_commands({"codex": "p"}, **kwargs)
        return commands["codex"]

    def test_customizations_are_disabled_on_read_only_runs(self) -> None:
        argv = self._codex()
        for feature in ("hooks", "plugins", "apps"):
            with self.subTest(feature=feature):
                self.assertIn(feature, argv)
        self.assertIn("--disable", argv)

    def test_customizations_are_disabled_on_granted_runs_too(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        argv = self._codex(grant=grant, run_id="r1")
        self.assertIn("hooks", argv)

    def test_the_model_cannot_ask_its_way_out_of_the_sandbox(self) -> None:
        self.assertIn('approval_policy="never"', self._codex())

    def test_execpolicy_rules_are_declined_on_every_run(self) -> None:
        """`.rules` is a layer above the sandbox: a rule can permit a command
        to run outside it, and approval_policy="never" means nothing prompts
        when that happens. This is a safety boundary, not a provider or
        billing override, so it applies under existing_config too."""
        self.assertIn("--ignore-rules", self._codex())
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        self.assertIn("--ignore-rules", self._codex(grant=grant, run_id="r1"))

    def test_strict_config_applies_to_read_only_runs_too(self) -> None:
        """Read-only runs depend on these flags being understood as well, so
        fail-closed cannot be granted-runs-only."""
        self.assertIn("--strict-config", self._codex())

    def test_the_slot_is_closed_until_mcp_can_be_isolated(self) -> None:
        """MCP tools act outside the shell sandbox and could not be disabled
        per run on codex-cli 0.149.0 (see config.CODEX_SLOT_ENABLED)."""
        from src import config

        self.assertFalse(config.is_agent_slot_enabled("codex"))
        self.assertFalse(self.adapters.command_exists("codex"))

    def test_the_pinned_sandbox_is_fail_closed(self) -> None:
        """--strict-config: an unknown key must break the run visibly rather
        than silently widening the write boundary."""
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        argv = self._codex(grant=grant, run_id="r1")
        self.assertIn("--strict-config", argv)
        self.assertIn("sandbox_workspace_write.writable_roots=[]", argv)
        self.assertIn("sandbox_workspace_write.network_access=false", argv)


class GrantScopeIsCheckedByEveryBuilderTest(unittest.TestCase):
    """A grant naming one agent must not open another, even when handed
    straight to a final builder without going through grant_for()."""

    def setUp(self) -> None:
        enable_implementation_writes(self)

    def test_a_codex_grant_does_not_open_claude(self) -> None:
        from src.services import claude_command

        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        argv = claude_command.build(
            claude_command.ClaudeRunSpec(prompt="p"), "/bin/claude", grant=grant
        )
        self.assertIn("plan", argv)
        self.assertNotIn("acceptEdits", argv)

    def test_a_claude_grant_still_opens_claude(self) -> None:
        from src.services import claude_command

        grant = granted_after_approval(
            run_id="r1", agent="claude", project_root=Path("/tmp/p"), approved=True
        )
        argv = claude_command.build(
            claude_command.ClaudeRunSpec(prompt="p"), "/bin/claude", grant=grant
        )
        self.assertIn("acceptEdits", argv)

    def test_base_command_checks_the_agent_too(self) -> None:
        adapters = CliAdapters(prefer_rtk=False)
        grant = granted_after_approval(
            run_id="r1", agent="claude", project_root=Path("/tmp/p"), approved=True
        )
        argv = adapters._base_command("codex", "p", grant=grant)
        self.assertIn("read-only", argv)


class CommandsStayReadOnlyWithoutAGrantTest(unittest.TestCase):
    """The property this whole module exists to protect."""

    def setUp(self) -> None:
        enable_implementation_writes(self)
        self.adapters = CliAdapters(prefer_rtk=False)
        self.prompts = {"claude": "p", "gemini": "p", "codex": "p"}

    def _commands(self, **kwargs):
        commands, _ = self.adapters.build_commands(self.prompts, **kwargs)
        return commands

    def test_every_agent_is_read_only_at_every_level_without_a_grant(self) -> None:
        for level in (1, 2, 3, 4, 5):
            with self.subTest(level=level):
                commands = self._commands(automation_level=level)
                self.assertIn("plan", commands["claude"])
                self.assertIn("plan", commands["gemini"])
                self.assertIn("read-only", commands["codex"])

    def test_a_grant_only_opens_the_agent_it_names(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        commands = self._commands(automation_level=3, grant=grant, run_id="r1")
        self.assertIn("workspace-write", commands["codex"])
        # The other two stay in plan mode in the very same build.
        self.assertIn("plan", commands["claude"])
        self.assertIn("plan", commands["gemini"])

    def test_a_grant_from_another_run_does_not_open_anything(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        commands = self._commands(automation_level=3, grant=grant, run_id="r2")
        self.assertIn("read-only", commands["codex"])

    def test_claude_write_mode_never_bypasses_permissions(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="claude", project_root=Path("/tmp/p"), approved=True
        )
        commands = self._commands(automation_level=3, grant=grant, run_id="r1")
        self.assertIn("acceptEdits", commands["claude"])
        self.assertNotIn("bypassPermissions", commands["claude"])

    def test_gemini_keeps_its_sandbox_when_granted(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="gemini", project_root=Path("/tmp/p"), approved=True
        )
        commands = self._commands(automation_level=3, grant=grant, run_id="r1")
        self.assertIn("accept-edits", commands["gemini"])
        self.assertIn("--sandbox", commands["gemini"])

    def test_codex_never_gets_full_disk_access(self) -> None:
        grant = granted_after_approval(
            run_id="r1", agent="codex", project_root=Path("/tmp/p"), approved=True
        )
        commands = self._commands(automation_level=3, grant=grant, run_id="r1")
        self.assertNotIn("danger-full-access", commands["codex"])


if __name__ == "__main__":
    unittest.main()
