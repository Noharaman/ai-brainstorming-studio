import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from src.services import autonomy_controller as ac
from src.services.implementation_plan import (
    ApprovalDecision,
    ImplementationPlan,
    parse,
)
from src.services.refinement_loop import MAX_TEST_REPAIR_ATTEMPTS, RefinementLoop
from src.services.role_orchestrator import RoleOrchestrator
from tests.support import (
    enable_all_slots,
    enable_automatic_tests,
    enable_implementation_writes,
)


class LevelDefinitionTest(unittest.TestCase):
    def test_no_level_can_write_while_writes_are_withdrawn(self) -> None:
        """The kill switch overrides the capability table, so a stored level
        from a build where 3 could implement cannot reopen the path."""
        for level in (ac.LEVEL_CONSULT, ac.LEVEL_PLAN, ac.LEVEL_IMPLEMENT):
            with self.subTest(level=level):
                self.assertFalse(ac.grants_write(level))

    def test_only_the_top_level_can_write_when_enabled(self) -> None:
        enable_implementation_writes(self)
        self.assertFalse(ac.grants_write(ac.LEVEL_CONSULT))
        self.assertFalse(ac.grants_write(ac.LEVEL_PLAN))
        self.assertTrue(ac.grants_write(ac.LEVEL_IMPLEMENT))

    def test_the_gui_offers_only_read_only_levels(self) -> None:
        for label, level in ac.AUTOMATION_LEVELS.items():
            with self.subTest(label=label):
                self.assertFalse(ac.capabilities_for(level).can_implement)

    def test_labels_and_levels_round_trip(self) -> None:
        for label, level in ac.AUTOMATION_LEVELS.items():
            self.assertEqual(ac.label_to_level(label), level)
            self.assertEqual(ac.level_to_label(level), label)

    def test_retired_labels_migrate_down_to_an_offered_level(self) -> None:
        """A tab saved when level 3 implemented must reopen on a level the
        menu actually has, not sit on one that is gone."""
        self.assertEqual(ac.label_to_level("1: 相談のみ"), ac.LEVEL_CONSULT)
        for retired in (
            "3: 実装案まで",
            "3: 実装・テストまで",
            "3: 実装まで（テストは手動）",
            "4: 実装・テストまで",
            "5: 差分確認待ちまで",
        ):
            with self.subTest(label=retired):
                self.assertEqual(ac.label_to_level(retired), ac.LEVEL_PLAN)

    def test_an_unknown_label_falls_back_to_the_default(self) -> None:
        self.assertEqual(ac.label_to_level("なにこれ"), ac.DEFAULT_LEVEL)

    def test_stored_integers_outside_the_range_are_clamped(self) -> None:
        self.assertEqual(ac.normalize_level(4), ac.LEVEL_IMPLEMENT)
        self.assertEqual(ac.normalize_level(5), ac.LEVEL_IMPLEMENT)
        self.assertEqual(ac.normalize_level(99), ac.DEFAULT_LEVEL)

    def test_the_label_a_level_shows_matches_what_it_does(self) -> None:
        """The bug this rewrite fixes: a label promising more than it ran."""
        for level, fragment in ((1, "相談"), (2, "実装案")):
            self.assertIn(fragment, ac.level_to_label(level))
        # No offered level implements, and none of their labels says it does.
        for label, level in ac.AUTOMATION_LEVELS.items():
            self.assertFalse(ac.capabilities_for(level).can_implement, label)

    def test_no_label_promises_automatic_tests_while_they_are_off(self) -> None:
        """A label saying "テストまで" while config.RUN_TESTS_AUTOMATICALLY is
        False would be the same lie this module was rewritten to remove."""
        from src import config

        if config.RUN_TESTS_AUTOMATICALLY:
            self.skipTest("automatic tests are enabled")
        for label in ac.AUTOMATION_LEVELS:
            self.assertNotIn("テストまで", label)

    def test_round_counts_come_from_the_capabilities(self) -> None:
        orchestrator = RoleOrchestrator()
        for level in ac.AUTOMATION_LEVELS.values():
            self.assertEqual(
                orchestrator.round_count_for_level(level),
                ac.capabilities_for(level).rounds,
            )


class _FakeWorkspace:
    """Records artifacts instead of touching the filesystem."""

    def __init__(self) -> None:
        self.artifacts: dict[str, str] = {}

    def write_session_artifact(self, session_id: str, name: str, content: str) -> None:
        self.artifacts[name] = content


class ImplementationGateTest(unittest.TestCase):
    """The phase must not write without an explicit, human approval."""

    def setUp(self) -> None:
        enable_implementation_writes(self)
        self.loop = RefinementLoop.__new__(RefinementLoop)
        self.calls: list[str] = []
        self.workspace = _FakeWorkspace()

    def _phase(self, level: int, approval_callback=None, **overrides):
        kwargs = dict(
            project_root=Path("/tmp/nonexistent-project"),
            user_request="req",
            session_id="s1",
            workspace=self.workspace,
            results={},
            round_summaries=[],
            available_agents=set(),
            role_rounds=[],
            automation_level=level,
            agent_selection=None,
            run_context=None,
            approval_callback=approval_callback,
            cancel_event=None,
            progress=None,
            enter=lambda state: self.calls.append(f"state:{state}"),
            emit=lambda message: self.calls.append(f"emit:{message}"),
        )
        kwargs.update(overrides)
        return asyncio.run(self.loop._implementation_phase(**kwargs))

    def test_read_only_levels_never_reach_the_gate(self) -> None:
        for level in (ac.LEVEL_CONSULT, ac.LEVEL_PLAN):
            with self.subTest(level=level):
                asked = []
                outcome = self._phase(
                    level, approval_callback=lambda req: asked.append(req)
                )
                self.assertFalse(outcome.attempted)
                self.assertFalse(outcome.approved)
                self.assertEqual(asked, [], "the user must not even be asked")

    def test_no_approval_callback_means_no_implementation(self) -> None:
        outcome = self._phase(ac.LEVEL_IMPLEMENT, approval_callback=None)
        self.assertFalse(outcome.attempted)
        self.assertTrue(any("承認" in note for note in outcome.notes))

    def test_no_eligible_implementer_stops_the_phase(self) -> None:
        outcome = self._phase(
            ac.LEVEL_IMPLEMENT, approval_callback=lambda req: ApprovalDecision(True)
        )
        self.assertFalse(outcome.attempted)
        self.assertFalse(outcome.approved)

    def test_a_raising_callback_does_not_grant_write_access(self) -> None:
        def explode(request):
            raise RuntimeError("dialog blew up")

        with patch.object(
            RefinementLoop, "_choose_implementer", return_value="codex"
        ), patch.object(
            RefinementLoop, "_build_implementation_plan_text", return_value="## 変更概要\nx"
        ):
            outcome = self._phase(ac.LEVEL_IMPLEMENT, approval_callback=explode)
        self.assertFalse(outcome.attempted)
        self.assertFalse(outcome.approved)

    def test_rejection_leaves_files_untouched(self) -> None:
        with patch.object(
            RefinementLoop, "_choose_implementer", return_value="codex"
        ), patch.object(
            RefinementLoop, "_build_implementation_plan_text", return_value="## 変更概要\nx"
        ):
            outcome = self._phase(
                ac.LEVEL_IMPLEMENT,
                approval_callback=lambda req: ApprovalDecision(
                    approved=False, feedback="やめて"
                ),
            )
        self.assertFalse(outcome.attempted)
        self.assertTrue(any("やめて" in note for note in outcome.notes))

    def test_cancellation_at_the_gate_is_not_a_rejection_note(self) -> None:
        with patch.object(
            RefinementLoop, "_choose_implementer", return_value="codex"
        ), patch.object(
            RefinementLoop, "_build_implementation_plan_text", return_value="## 変更概要\nx"
        ):
            outcome = self._phase(
                ac.LEVEL_IMPLEMENT,
                approval_callback=lambda req: ApprovalDecision(
                    approved=False, cancelled=True
                ),
            )
        self.assertFalse(outcome.attempted)
        self.assertEqual(outcome.notes, [])


class ImplementerChoiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = RefinementLoop.__new__(RefinementLoop)

    def test_an_agent_that_failed_never_gets_the_grant(self) -> None:
        class Result:
            def __init__(self, ok):
                self.ok = ok

        rounds = RoleOrchestrator().build_plan({"claude", "codex", "gemini"}, 3)
        results = {"claude": Result(False), "codex": Result(True), "gemini": Result(False)}
        chosen = self.loop._choose_implementer(rounds, {"claude", "codex", "gemini"}, results)
        self.assertEqual(chosen, "codex")

    def test_no_successful_agent_means_no_implementer(self) -> None:
        rounds = RoleOrchestrator().build_plan({"claude"}, 3)
        self.assertIsNone(self.loop._choose_implementer(rounds, {"claude"}, {}))


class PlanParsingTest(unittest.TestCase):
    def test_headings_survive_decoration_and_renaming(self) -> None:
        plan = parse(
            "## **変更概要**\n概要文\n### 対象ファイル:\n- a.py\n"
            "実装手順\n1. やる\n## テスト\n- t\n## リスク・注意点\n- r\n"
        )
        self.assertEqual(plan.summary, "概要文")
        self.assertEqual(plan.target_files, ("a.py",))
        self.assertEqual(plan.steps, ("やる",))
        self.assertTrue(plan.is_parsed)

    def test_unparsed_text_is_preserved_for_display(self) -> None:
        plan = parse("見出しのない自由記述")
        self.assertFalse(plan.is_parsed)
        self.assertEqual(plan.raw_text, "見出しのない自由記述")
        self.assertEqual(plan.render(), "見出しのない自由記述")

    def test_empty_plan_is_not_parsed(self) -> None:
        self.assertFalse(parse("").is_parsed)
        self.assertFalse(ImplementationPlan().is_parsed)



class _FakeCliRunner:
    """Records every CLI invocation the phase makes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def run_all(
        self,
        prompts,
        cwd,
        automation_level,
        progress,
        cancel_event,
        agent_selection,
        grant=None,
        run_id="",
    ):
        from src.models import CommandResult

        agent = list(prompts)[0]
        self.calls.append({"agent": agent, "prompt": prompts[agent], "grant": grant})
        return (
            {agent: CommandResult(agent=agent, command=["x"], ok=True, status="ok", stdout="ok")},
            [],
        )


class _ScriptedTestRunner:
    """Returns a scripted pass/fail sequence, one entry per invocation."""

    script: list = []
    runs = 0

    def run(self, project_root, cancel_event=None, timeout_seconds=600):
        from src.services.test_runner import TestOutcome

        index = min(_ScriptedTestRunner.runs, len(_ScriptedTestRunner.script) - 1)
        passed = _ScriptedTestRunner.script[index]
        _ScriptedTestRunner.runs += 1
        return TestOutcome(ran=True, command=("t",), passed=passed, output="output")


class TestRepairLoopTest(unittest.TestCase):
    """Failing tests get a bounded number of fix attempts, never an open loop."""

    def setUp(self) -> None:
        self.loop = RefinementLoop.__new__(RefinementLoop)
        self.runner = _FakeCliRunner()
        self.loop.cli_runner = self.runner
        _ScriptedTestRunner.runs = 0

    def _run(self, script):
        from src.services.refinement_loop import RefinementLoop as RL

        # This class is about the repair loop itself, so it enables both
        # capabilities that ship withdrawn.
        enable_implementation_writes(self)
        enable_automatic_tests(self)

        _ScriptedTestRunner.script = script
        with patch.object(RL, "_choose_implementer", return_value="codex"), patch.object(
            RL, "_build_implementation_plan_text", return_value="## 変更概要\nx"
        ), patch(
            "src.services.refinement_loop.ProjectTestRunner", _ScriptedTestRunner
        ):
            return asyncio.run(
                self.loop._implementation_phase(
                    project_root=Path("."),
                    user_request="r",
                    session_id="s",
                    workspace=_FakeWorkspace(),
                    results={},
                    round_summaries=[],
                    available_agents={"codex"},
                    role_rounds=[],
                    automation_level=ac.LEVEL_IMPLEMENT,
                    agent_selection=None,
                    run_context=None,
                    approval_callback=lambda request: ApprovalDecision(approved=True),
                    cancel_event=None,
                    progress=None,
                    enter=lambda state: None,
                    emit=lambda message: None,
                )
            )

    def test_passing_tests_trigger_no_repair(self) -> None:
        outcome = self._run([True])
        self.assertTrue(outcome.test_passed)
        self.assertEqual(outcome.repair_attempts, 0)
        self.assertEqual(len(self.runner.calls), 1, "implementation only")

    def test_a_failure_is_repaired_and_rechecked(self) -> None:
        outcome = self._run([False, True])
        self.assertTrue(outcome.test_passed)
        self.assertEqual(outcome.repair_attempts, 1)
        self.assertEqual(len(self.runner.calls), 2, "implementation + one repair")

    def test_repair_is_bounded(self) -> None:
        outcome = self._run([False])
        self.assertFalse(outcome.test_passed)
        self.assertEqual(outcome.repair_attempts, MAX_TEST_REPAIR_ATTEMPTS)
        self.assertEqual(
            len(self.runner.calls), MAX_TEST_REPAIR_ATTEMPTS + 1, "must not loop forever"
        )
        self.assertTrue(any("確認してください" in note for note in outcome.notes))

    def test_repairs_reuse_the_same_grant(self) -> None:
        """A repair must not need, or invent, a second approval."""
        self._run([False])
        grants = [call["grant"] for call in self.runner.calls]
        self.assertTrue(all(g is not None for g in grants))
        self.assertEqual(len({id(g) for g in grants}), 1)
        self.assertTrue(all(g.agent == "codex" for g in grants))

    def test_the_repair_prompt_forbids_disabling_tests(self) -> None:
        """Otherwise the cheapest way to make tests pass is to delete them."""
        self._run([False, True])
        repair_prompt = self.runner.calls[1]["prompt"]
        self.assertIn("削除・スキップ・無効化", repair_prompt)


class OutcomePersistenceTest(unittest.TestCase):
    """The diff tab must survive reopening a room."""

    def _make_outcome(self, **overrides):
        from src.services.implementation_plan import ImplementationOutcome

        base = dict(
            implementer="codex",
            attempted=True,
            approved=True,
            changed_files=("a.py", "b.py (新規)"),
            diff_text="--- a\n+++ b\n",
            diff_stat="2 files changed",
            test_command="python3 -m unittest",
            test_passed=True,
            test_output="OK",
            revert_hint="git reset --hard abc123",
            repair_attempts=1,
            notes=["note"],
        )
        base.update(overrides)
        return ImplementationOutcome(**base)

    def test_round_trip_preserves_what_the_diff_tab_shows(self) -> None:
        from src.services.implementation_plan import outcome_from_dict, outcome_to_dict

        restored = outcome_from_dict(outcome_to_dict(self._make_outcome()))
        self.assertEqual(restored.implementer, "codex")
        self.assertEqual(restored.changed_files, ("a.py", "b.py (新規)"))
        self.assertEqual(restored.test_command, "python3 -m unittest")
        self.assertTrue(restored.test_passed)
        self.assertEqual(restored.repair_attempts, 1)
        self.assertIn("git reset --hard abc123", restored.revert_hint)

    def test_a_read_only_run_stores_nothing(self) -> None:
        from src.services.implementation_plan import ImplementationOutcome, outcome_to_dict

        self.assertIsNone(outcome_to_dict(ImplementationOutcome()))
        self.assertIsNone(outcome_to_dict(None))

    def test_missing_or_foreign_data_loads_as_nothing(self) -> None:
        from src.services.implementation_plan import outcome_from_dict

        self.assertIsNone(outcome_from_dict(None))
        self.assertIsNone(outcome_from_dict({}))
        self.assertIsNone(outcome_from_dict({"attempted": False}))
        self.assertIsNone(outcome_from_dict("not a dict"))

    def test_unknown_keys_do_not_break_loading(self) -> None:
        """A room written by a newer build must still open."""
        from src.services.implementation_plan import outcome_from_dict, outcome_to_dict

        data = outcome_to_dict(self._make_outcome())
        data["some_future_field"] = 1
        self.assertEqual(outcome_from_dict(data).implementer, "codex")

    def test_a_huge_diff_is_capped(self) -> None:
        """chat_rooms.json is rewritten on every turn; it must not carry a
        megabyte patch for the life of the room."""
        from src.services.implementation_plan import (
            PERSISTED_DIFF_LIMIT,
            outcome_to_dict,
        )

        data = outcome_to_dict(self._make_outcome(diff_text="x" * (PERSISTED_DIFF_LIMIT * 3)))
        self.assertLess(len(data["diff_text"]), PERSISTED_DIFF_LIMIT + 500)
        self.assertTrue(data["diff_truncated"])

    def test_secrets_in_the_diff_are_redacted_before_being_stored(self) -> None:
        from src.services.implementation_plan import outcome_to_dict

        secret = "sk-ant-" + "a" * 40
        data = outcome_to_dict(self._make_outcome(diff_text=f"+KEY={secret}"))
        self.assertNotIn(secret, data["diff_text"])


class TestsAreNotRunAutomaticallyTest(unittest.TestCase):
    """Approving an implementation is not approving "run arbitrary code as me".

    Running the suite executes code the AI has just written, with this app's
    own rights, unrestricted network and the full parent environment. Until
    that happens inside an OS sandbox, the command is detected and shown.
    """

    def setUp(self) -> None:
        enable_implementation_writes(self)
        self.loop = RefinementLoop.__new__(RefinementLoop)
        self.messages: list[str] = []

    def test_the_flag_ships_off(self) -> None:
        from src import config

        self.assertFalse(config.RUN_TESTS_AUTOMATICALLY)

    def test_the_command_is_reported_but_not_run(self) -> None:
        from src.services.implementation_plan import ImplementationOutcome

        outcome = ImplementationOutcome(attempted=True)
        with patch(
            "src.services.test_runner.detect_command",
            return_value=("python3", "-m", "unittest"),
        ), patch("src.services.test_runner.ProjectTestRunner.run") as run:
            self.loop._report_manual_test_command(
                Path("."), outcome, self.messages.append
            )
        run.assert_not_called()
        self.assertEqual(outcome.test_command, "python3 -m unittest")

    def test_no_verdict_is_claimed_for_a_suite_that_never_ran(self) -> None:
        """Reporting pass/fail for something not executed is worse than
        reporting nothing."""
        from src.services.implementation_plan import ImplementationOutcome

        outcome = ImplementationOutcome(attempted=True)
        with patch(
            "src.services.test_runner.detect_command",
            return_value=("python3", "-m", "unittest"),
        ):
            self.loop._report_manual_test_command(
                Path("."), outcome, self.messages.append
            )
        self.assertIsNone(outcome.test_passed)
        self.assertTrue(any("手動" in note for note in outcome.notes))

    def test_an_unrecognised_project_says_so(self) -> None:
        from src.services.implementation_plan import ImplementationOutcome

        outcome = ImplementationOutcome(attempted=True)
        with patch("src.services.test_runner.detect_command", return_value=None):
            self.loop._report_manual_test_command(
                Path("."), outcome, self.messages.append
            )
        self.assertEqual(outcome.test_command, "")
        self.assertIsNone(outcome.test_passed)

    def test_the_implementation_phase_does_not_reach_the_runner(self) -> None:
        """End to end: an approved run must not execute the suite."""
        from src.models import CommandResult

        class _Runner:
            async def run_all(self, prompts, cwd, automation_level, progress,
                              cancel_event, agent_selection, grant=None, run_id=""):
                agent = list(prompts)[0]
                return (
                    {agent: CommandResult(agent=agent, command=["x"], ok=True,
                                          status="ok", stdout="ok")},
                    [],
                )

        self.loop.cli_runner = _Runner()
        with patch.object(
            RefinementLoop, "_choose_implementer", return_value="gemini"
        ), patch.object(
            RefinementLoop, "_build_implementation_plan_text", return_value="## 変更概要\nx"
        ), patch(
            "src.services.refinement_loop.ProjectTestRunner"
        ) as runner_cls:
            outcome = asyncio.run(
                self.loop._implementation_phase(
                    project_root=Path("."),
                    user_request="r",
                    session_id="s",
                    workspace=_FakeWorkspace(),
                    results={},
                    round_summaries=[],
                    available_agents={"gemini"},
                    role_rounds=[],
                    automation_level=ac.LEVEL_IMPLEMENT,
                    agent_selection=None,
                    run_context=None,
                    approval_callback=lambda request: ApprovalDecision(approved=True),
                    cancel_event=None,
                    progress=None,
                    enter=lambda state: None,
                    emit=lambda message: None,
                )
            )
        runner_cls.assert_not_called()
        self.assertTrue(outcome.attempted)
        self.assertIsNone(outcome.test_passed)



class UiTextMatchesEnabledSlotsTest(unittest.TestCase):
    """The run button said "3社に同時ブレスト依頼" for a build that launches no
    external CLI at all — the same false promise the level menu used to make.
    These strings are derived from the slot switches, so they cannot drift."""

    def setUp(self) -> None:
        from src.gui import project_tab

        self.pt = project_tab

    def test_no_open_slots_names_lm_studio_only(self) -> None:
        self.assertEqual(self.pt.enabled_cli_slots(), [])
        self.assertIn("LM Studio", self.pt.run_button_label())
        self.assertIn("LM Studio", self.pt.consultation_hint())
        self.assertIn("LM Studio", self.pt.integrating_message())

    def test_no_open_slots_never_claims_three(self) -> None:
        for text in (
            self.pt.run_button_label(),
            self.pt.consultation_hint(),
            self.pt.integrating_message(),
        ):
            with self.subTest(text=text):
                self.assertNotIn("3社", text)
                for vendor in ("Claude", "Antigravity", "Codex"):
                    self.assertNotIn(vendor, text)

    def test_one_open_slot_names_that_one(self) -> None:
        enable_all_slots(self)
        from src import config

        config.CODEX_SLOT_ENABLED = False
        config.ANTIGRAVITY_SLOT_ENABLED = False
        self.assertEqual(self.pt.enabled_cli_slots(), ["Claude"])
        self.assertIn("Claude", self.pt.run_button_label())
        self.assertNotIn("3社", self.pt.run_button_label())

    def test_all_open_slots_counts_them(self) -> None:
        enable_all_slots(self)
        self.assertEqual(
            self.pt.enabled_cli_slots(), ["Claude", "Antigravity", "Codex"]
        )
        self.assertIn("3社", self.pt.run_button_label())


class DisabledSlotIsNotShownAsAFaultTest(unittest.TestCase):
    """A closed slot is a decision, not a broken login. Showing it red sends
    users off re-authenticating a CLI that was never going to run."""

    def test_the_badge_is_not_the_error_colour(self) -> None:
        from src.gui.components import header_status_bar as hsb

        class _Status:
            status = "slot_disabled"

        icon, background, _hover, _text = hsb.status_visual(_Status())
        self.assertNotEqual(icon, "🔴")
        self.assertNotEqual(background, "#7F1D1D", "must not use the error red")

    def test_the_guidance_says_it_is_deliberate(self) -> None:
        from src.services import cli_status

        guidance = cli_status.guidance_for("slot_disabled")
        self.assertIn("不具合ではありません", guidance)
        self.assertIn("safety-model", guidance)



class ClosedSlotsAreReportedConsistentlyTest(unittest.TestCase):
    """All three lamps must agree when all three slots are closed.

    codex was the odd one out: _check_claude and _check_antigravity each
    consulted the slot switch, but the generic _check_command did not, so the
    header showed two paused slots and one merely-unverified one.
    """

    def _statuses(self):
        from src.services.health_checker import HealthChecker

        return {s.name: s.status for s in HealthChecker().check_all()}

    def test_every_installed_but_closed_slot_reports_slot_disabled(self) -> None:
        import shutil

        statuses = self._statuses()
        for command, name in (("claude", "claude"), ("agy", "Antigravity(agy)"), ("codex", "codex")):
            if not shutil.which(command):
                continue  # not installed here; nothing to report about
            with self.subTest(agent=name):
                self.assertEqual(statuses.get(name), "slot_disabled")

    def test_lm_studio_is_not_reported_as_a_closed_slot(self) -> None:
        """It is the one thing this build does use."""
        statuses = self._statuses()
        self.assertNotEqual(statuses.get("LM Studio"), "slot_disabled")


if __name__ == "__main__":
    unittest.main()
