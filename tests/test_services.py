"""Regression tests for the service layer. Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src import config
from src.models import CommandResult, ScanResult
from src.services import agent_model_selector
from src.services import cli_adapters as cli_adapters_module
from src.services import (
    claude_command,
    cli_execution_policy,
    cli_status,
    secret_redactor,
)
from src.services import health_checker as health_checker_module
from src.services import process_runner as process_runner_module
from src.services.chair_agent import ChairAgent
from src.services.chat_room_manager import ChatRoomManager
from src.services.cli_adapters import CliAdapters
from src.services.cli_runner import CliRunner
from src.services.context_scanner import ContextScanner
from src.services.process_runner import ProcessRunner
from src.services.health_checker import HealthChecker
from src.services.lm_studio_manager import LMStudioManager
from src.services.prompt_builder import PromptBuilder
from src.services.refinement_loop import RefinementLoop
from src.services.response_preprocessor import ResponsePreprocessor
from src.services.role_orchestrator import RoleOrchestrator
from src.services.run_registry import ProjectRunRegistry
from src.services.tab_session_manager import TabSessionManager


def _claude_path() -> str:
    """Real claude binary: agent="claude" runs may only launch this."""
    import shutil as _shutil

    return _shutil.which("claude") or "claude"


def stub_claude_token(test_case: unittest.TestCase, token: str | None = "sk-ant-oat-TESTONLY-never-a-real-token") -> None:
    """Legacy no-op stub for backward compatibility in tests."""
    pass


def stub_claude_auth(test_case: unittest.TestCase, *, ok: bool = True) -> None:
    """Legacy no-op stub for backward compatibility in tests."""
    pass


# Re-exported so the many test classes in this file keep one import site.
# Ships closed; see tests/support.py for why each switch exists.
from tests.support import enable_all_slots  # noqa: F401


class ChairAgentCacheTest(unittest.TestCase):
    def test_invalidate_cache_allows_a_later_probe(self) -> None:
        """LM Studio auto-start is pointless if the offline result stays cached."""
        chair = ChairAgent(base_url="http://localhost:59999/v1")
        chair.available()
        self.assertIsNotNone(chair._available_cache)
        chair.invalidate_cache()
        self.assertIsNone(chair._available_cache)
        self.assertIsNone(chair._model_cache)


class CrossCliSharedMemoryTest(unittest.TestCase):
    def test_scanner_reads_shared_memory_even_though_runtime_dir_is_excluded_from_tree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
            memory_dir = root / ".ai-shared"
            memory_dir.mkdir()
            (memory_dir / "memory.md").write_text("# Shared\n- decision\n", encoding="utf-8")

            scan = ContextScanner(root).scan()

        self.assertEqual(scan.important_files["AGENTS.md"], "# Rules\n")
        self.assertEqual(
            scan.important_files[".ai-shared/memory.md"],
            "# Shared\n- decision\n",
        )
        self.assertFalse(any(".ai-shared" in item for item in scan.tree))

    def test_prompt_pack_keeps_rules_and_memory_verbatim_when_chair_summarizes(self) -> None:
        class _Chair:
            def chat(self, *args, **kwargs):
                return "chair summary"

        scan = ScanResult(
            project_root=Path("."),
            tree=[],
            important_files={
                "AGENTS.md": "# Rules\nmandatory-rule",
                ".ai-shared/memory.md": "# Shared\nuser-decision",
            },
            vendor_paths=["AGENTS.md"],
        )

        pack = PromptBuilder(_Chair()).build_context_pack(scan, "request", "raw")

        self.assertIn("mandatory-rule", pack)
        self.assertIn("user-decision", pack)
        self.assertIn("chair summary", pack)
        self.assertLessEqual(len(pack), config.MAX_CONTEXT_PACK_CHARS)

    def test_fallback_pack_also_keeps_rules_and_memory_verbatim(self) -> None:
        class _OfflineChair:
            def chat(self, *args, **kwargs):
                return ""

        scan = ScanResult(
            project_root=Path("."),
            tree=[],
            important_files={
                "AGENTS.md": "# Rules\nmandatory-rule",
                ".ai-shared/memory.md": "# Shared\nuser-decision",
            },
            vendor_paths=["AGENTS.md"],
        )

        pack = PromptBuilder(_OfflineChair()).build_context_pack(scan, "request", "raw")

        self.assertIn("mandatory-rule", pack)
        self.assertIn("user-decision", pack)
        self.assertIn("今回の依頼:", pack)
        self.assertLessEqual(len(pack), config.MAX_CONTEXT_PACK_CHARS)

    def test_shared_memory_is_redacted_before_prompt_injection(self) -> None:
        class _Chair:
            def chat(self, *args, **kwargs):
                return "chair summary"

        scan = ScanResult(
            project_root=Path("."),
            tree=[],
            important_files={
                ".ai-shared/memory.md": "# Shared\nAPI_TOKEN=abc123def456",
            },
            vendor_paths=[],
        )

        context, _, pack = PromptBuilder(_Chair()).build_context_documents(scan, "request")

        self.assertNotIn("abc123def456", context)
        self.assertNotIn("abc123def456", pack)
        self.assertIn("API_TOKEN", context)


class Python310CompatibilityTest(unittest.TestCase):
    def test_prompt_builder_imports_on_python_310(self) -> None:
        python310 = shutil.which("python3.10")
        if not python310:
            self.skipTest("python3.10 is not installed")
        project_root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        completed = subprocess.run(
            [python310, "-c", "import src.services.prompt_builder"],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

        self.assertEqual(
            completed.returncode,
            0,
            msg=(completed.stdout + "\n" + completed.stderr).strip(),
        )


class ChatRoomManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Path(tempfile.mkdtemp())
        self.manager = ChatRoomManager(self.project)

    def test_round_trip(self) -> None:
        room_id = self.manager.create_room("テスト")
        self.manager.append_turn(room_id, "依頼", "回答", "20260811-120000")
        rendered = self.manager.render_room(room_id)
        self.assertIn("依頼", rendered)
        self.assertIn("回答", rendered)

    def test_reading_a_corrupt_file_reports_error_without_writing(self) -> None:
        """Selecting/browsing a project must stay read-only even when history is corrupt."""
        self.manager.create_room("消えては困るルーム")
        self.manager.path.write_text("{broken", encoding="utf-8")

        manager = ChatRoomManager(self.project)
        before = manager.path.stat().st_mtime_ns
        self.assertEqual(manager.list_rooms(), [])
        self.assertIn("chat_rooms.json", manager.load_error)
        self.assertEqual(manager.path.stat().st_mtime_ns, before, "reading must not touch the file")
        self.assertEqual(
            list(self.manager.brainstorm_dir.glob("chat_rooms.corrupt-*.json")), [],
            "no backup should exist yet; nothing has written",
        )

    def test_writing_after_corruption_quarantines_the_original(self) -> None:
        self.manager.create_room("消えては困るルーム")
        self.manager.path.write_text("{broken", encoding="utf-8")

        manager = ChatRoomManager(self.project)
        manager.create_room("新しいルーム")  # first write action since corruption

        backups = list(self.manager.brainstorm_dir.glob("chat_rooms.corrupt-*.json"))
        self.assertEqual(len(backups), 1, "the unreadable file must be preserved on write")
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{broken")
        self.assertEqual([r["title"] for r in manager.list_rooms()], ["新しいルーム"])

    def test_write_leaves_no_temp_file(self) -> None:
        self.manager.create_room("アトミック書き込み")
        leftovers = list(self.manager.brainstorm_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_reading_rooms_does_not_mutate_the_project(self) -> None:
        self.manager.create_room("読み取り専用")
        before = self.manager.path.stat().st_mtime_ns
        ChatRoomManager(self.project).list_rooms()
        ChatRoomManager(self.project).active_room_id()
        self.assertEqual(self.manager.path.stat().st_mtime_ns, before)


class FinalAnswerAcceptanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = RefinementLoop.__new__(RefinementLoop)

    def test_short_but_well_formed_answer_is_accepted(self) -> None:
        """The app's own spec asks for short answers, so length must not disqualify one."""
        answer = (
            "結論:\nタブ化で問題ありません。\n"
            "採用する方針:\nCTkTabviewではなく自作タブバーを使います。\n"
            "実行済み:\n- 3AIの回答を統合\n"
            "次にやること:\n- 実装\n"
            "リスク・注意点:\n特になし\n"
        )
        self.assertTrue(self.loop._final_answer_uses_success(answer))

    def test_answer_that_only_promises_to_start_is_rejected(self) -> None:
        answer = "結論:\nこれから調査を開始します。\n次にやること:\n調査\n" + "詳細" * 200
        self.assertFalse(self.loop._final_answer_uses_success(answer))

    def test_answer_missing_required_sections_is_rejected(self) -> None:
        self.assertFalse(self.loop._final_answer_uses_success("よくわかりませんでした。" * 40))


class RoleOrchestratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.orchestrator = RoleOrchestrator()

    def test_round_count_follows_automation_level(self) -> None:
        # The three real levels: consult / plan / implement.
        self.assertEqual(self.orchestrator.round_count_for_level(1), 1)
        self.assertEqual(self.orchestrator.round_count_for_level(2), 3)
        self.assertEqual(self.orchestrator.round_count_for_level(3), 3)

    def test_legacy_levels_still_map_to_a_round_count(self) -> None:
        """Tabs saved by the five-level build must not crash on restore."""
        self.assertEqual(self.orchestrator.round_count_for_level(4), 3)
        self.assertEqual(self.orchestrator.round_count_for_level(5), 3)

    def test_roles_rotate_between_rounds(self) -> None:
        rounds = self.orchestrator.build_plan({"claude", "gemini", "codex"}, automation_level=3)
        self.assertEqual(len(rounds), 3)
        roles_per_round = [
            {a.agent: a.role for a in role_round.assignments} for role_round in rounds
        ]
        for agent in ("claude", "gemini", "codex"):
            assigned = [roles[agent] for roles in roles_per_round]
            self.assertEqual(len(set(assigned)), 3, f"{agent} should see every role")
        for roles in roles_per_round:
            self.assertEqual(len(set(roles.values())), 3, "roles must be unique within a round")

    def test_unavailable_agents_are_excluded(self) -> None:
        rounds = self.orchestrator.build_plan({"codex"}, automation_level=1)
        self.assertEqual([a.agent for a in rounds[0].assignments], ["codex"])
        self.assertEqual(self.orchestrator.build_plan(set(), automation_level=2), [])


class TabSessionManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.manager = TabSessionManager(self.dir / "open_tabs.json")

    def test_round_trip(self) -> None:
        project = self.dir / "project"
        project.mkdir()
        self.manager.save(
            [{"project_path": str(project), "room_id": "room_1", "automation_level": "2: 計画まで"}],
            active_index=0,
        )
        restored = self.manager.load()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["project_path"], str(project))
        self.assertEqual(restored[0]["room_id"], "room_1")

    def test_deleted_folders_are_dropped(self) -> None:
        self.manager.save([{"project_path": str(self.dir / "gone"), "room_id": ""}])
        self.assertEqual(self.manager.load(), [])

    def test_corrupt_file_does_not_raise(self) -> None:
        self.manager.file_path.write_text("{broken", encoding="utf-8")
        self.assertEqual(self.manager.load(), [])
        self.assertEqual(self.manager.active_index(), 0)

    def test_missing_file_is_empty(self) -> None:
        self.assertEqual(TabSessionManager(self.dir / "nope.json").load(), [])


class SessionIdUniquenessTest(unittest.TestCase):
    def test_many_ids_generated_back_to_back_are_all_unique(self) -> None:
        """Two tabs running the same project must not collide on session artifacts."""
        loop = RefinementLoop.__new__(RefinementLoop)
        ids = [loop._new_session_id() for _ in range(200)]
        self.assertEqual(len(ids), len(set(ids)))


class RoundEarlyStopTest(unittest.TestCase):
    def setUp(self) -> None:
        self.loop = RefinementLoop.__new__(RefinementLoop)

    def _result(self, status: str, ok: bool = False) -> CommandResult:
        return CommandResult(agent="x", command=[], ok=ok, status=status)

    def test_all_unrecoverable_statuses_stop_early(self) -> None:
        results = {
            "round1_claude_author": self._result("rate_limited"),
            "round1_codex_critic": self._result("command_missing"),
        }
        self.assertTrue(self.loop._round_failed_unrecoverably(results))

    def test_one_success_does_not_stop_early(self) -> None:
        results = {
            "round1_claude_author": self._result("ok", ok=True),
            "round1_codex_critic": self._result("rate_limited"),
        }
        self.assertFalse(self.loop._round_failed_unrecoverably(results))

    def test_a_transient_failure_does_not_stop_early(self) -> None:
        """Timeouts/general failures might succeed on retry, unlike rate limits."""
        results = {"round1_claude_author": self._result("timeout")}
        self.assertFalse(self.loop._round_failed_unrecoverably(results))

    def test_empty_round_does_not_stop_early(self) -> None:
        self.assertFalse(self.loop._round_failed_unrecoverably({}))

    def test_convergence_line_yes_stops(self) -> None:
        summary = "合意点:\n...\n収束(次ラウンド省略可否): はい\n"
        self.assertTrue(self.loop._round_converged(summary))

    def test_convergence_line_no_continues(self) -> None:
        summary = "合意点:\n...\n収束(次ラウンド省略可否): いいえ\n"
        self.assertFalse(self.loop._round_converged(summary))

    def test_missing_convergence_line_fails_open(self) -> None:
        """No chair (LM Studio down) -> compressed fallback text has no such line."""
        summary = "LM Studio が未起動または応答不可のため、このラウンドはCLI回答の圧縮版を暫定要約として使います。"
        self.assertFalse(self.loop._round_converged(summary))


class PreflightFalsePositiveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = ProcessRunner()

    def test_exit_zero_with_login_text_is_not_ok_when_strict(self) -> None:
        """Preflight (success_patterns set) must not trust exit=0 blindly."""
        status = self.runner._classify(0, "Please login at https://example.com", "", strict=True)
        self.assertNotEqual(status, "ok")
        self.assertEqual(status, "auth_required")

    def test_exit_zero_is_ok_when_not_strict(self) -> None:
        """Main-request output is free text; must not false-positive on words like 'login'."""
        status = self.runner._classify(0, "ここではlogin画面の実装について説明します。", "", strict=False)
        self.assertEqual(status, "ok")

    def test_exit_zero_with_clean_output_is_ok_even_when_strict(self) -> None:
        status = self.runner._classify(0, "OK", "", strict=True)
        self.assertEqual(status, "ok")

    def test_nonzero_exit_still_classified_by_text(self) -> None:
        status = self.runner._classify(1, "", "rate limit exceeded", strict=False)
        self.assertEqual(status, "rate_limited")

    def test_healthy_auth_and_quota_status_text_is_not_a_negative_signal(self) -> None:
        status = self.runner._classify(
            0,
            "Authenticated successfully.\nQuota remaining: 80%.\nOK",
            "",
            strict=True,
        )
        self.assertEqual(status, "ok")

    def test_login_method_banner_is_not_an_auth_failure(self) -> None:
        status = self.runner._classify(
            0,
            "Login method: browser. Authentication status: active.\nOK",
            "",
            strict=True,
        )
        self.assertEqual(status, "ok")

    def test_explicit_quota_exhaustion_is_rate_limited(self) -> None:
        status = self.runner._classify(0, "Quota exhausted", "", strict=True)
        self.assertEqual(status, "rate_limited")

    def test_structured_http_error_codes_are_still_detected(self) -> None:
        self.assertEqual(
            self.runner._classify(1, "", '{"code": 429}', strict=False),
            "rate_limited",
        )
        self.assertEqual(
            self.runner._classify(1, "", '{"code": 401}', strict=False),
            "auth_required",
        )


class PreflightJudgesTheWholeRunTest(unittest.TestCase):
    """Preflight decides whether a whole AI slot is usable, so it must weigh
    the ending of the run, not the first encouraging line of output."""

    OK = ("OK",)

    def setUp(self) -> None:
        self.runner = ProcessRunner()

    def _status(self, returncode: int, stdout: str, stderr: str = "") -> str:
        return self.runner._preflight_status(returncode, stdout, stderr, self.OK)

    def test_silence_at_exit_zero_is_not_ready(self) -> None:
        """A CLI that exits 0 without saying anything has demonstrated
        nothing; treating that as READY sends the real request to a slot that
        was never shown to answer."""
        self.assertEqual(self._status(0, ""), "empty_response")
        self.assertEqual(self._status(0, "   \n "), "empty_response")

    def test_a_startup_banner_alone_is_not_a_reply(self) -> None:
        """The gap this closes: "the CLI launched" is not "the model
        answered". A logged-out CLI can print its own notice and exit 0
        without ever reaching a model, and accepting that would send the real
        request to a slot that cannot serve it."""
        for banner in (
            "Telemetry initialized",
            "Update available",
            "Loading configuration complete",
        ):
            with self.subTest(banner=banner):
                self.assertEqual(self._status(0, banner), "no_expected_reply")

    def test_the_reply_may_carry_the_decoration_a_model_adds(self) -> None:
        """Exact-matching the raw bytes would fail a CLI that answered."""
        for reply in ("OK", "OK.", "ok", "**OK**", "`OK`", '"OK"', "OK!", "\x1b[32mOK\x1b[0m"):
            with self.subTest(reply=reply):
                self.assertEqual(self._status(0, reply), "ok")

    def test_the_reply_still_counts_when_banners_surround_it(self) -> None:
        self.assertEqual(self._status(0, "Update available\nOK\nSession saved"), "ok")

    def test_prose_is_not_accepted_as_the_reply(self) -> None:
        """Deliberate: once the answer is a sentence it no longer separates a
        working CLI from a stuck one, which also emits sentences."""
        self.assertEqual(self._status(0, "OK, ready to help."), "no_expected_reply")

    def test_a_negative_signal_outranks_the_expected_reply(self) -> None:
        self.assertEqual(self._status(0, "OK\nrate limit exceeded"), "rate_limited")

    def test_nonzero_exit_is_never_ready_even_with_the_reply(self) -> None:
        self.assertEqual(self._status(3, "OK"), "failed")


class PreflightWaitsForTheRealExitTest(unittest.IsolatedAsyncioTestCase):
    """The expected reply used to end the run on the spot, killing the process
    and reporting returncode=0 — an exit code that was never observed. These
    drive real processes because the bug lived in the wait/kill sequencing,
    not in the classification helpers."""

    def setUp(self) -> None:
        enable_all_slots(self)

    def _shorten_grace(self, seconds: float) -> None:
        original = config.PREFLIGHT_EXIT_GRACE_SECONDS
        config.PREFLIGHT_EXIT_GRACE_SECONDS = seconds
        self.addCleanup(setattr, config, "PREFLIGHT_EXIT_GRACE_SECONDS", original)

    async def test_a_reply_followed_by_a_failure_is_not_ok(self) -> None:
        """The regression: printing the expected reply and *then* failing was
        indistinguishable from a clean success."""
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo OK; sleep 0.3; exit 3"],
            cwd=Path("."),
            timeout_seconds=20,
            success_patterns=("OK",),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.returncode, 3)

    async def test_a_reply_followed_by_a_rate_limit_is_reported_as_one(self) -> None:
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo OK; sleep 0.3; echo 'rate limit exceeded' >&2; exit 1"],
            cwd=Path("."),
            timeout_seconds=20,
            success_patterns=("OK",),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "rate_limited")

    async def test_a_clean_reply_is_still_ok_with_a_real_exit_code(self) -> None:
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo OK"],
            cwd=Path("."),
            timeout_seconds=20,
            success_patterns=("OK",),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.returncode, 0)

    async def test_a_banner_only_run_does_not_pass_end_to_end(self) -> None:
        """Same gap as the unit test above, driven through the real launch
        path: exiting 0 with output is not enough on its own."""
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo 'Update available'; exit 0"],
            cwd=Path("."),
            timeout_seconds=20,
            success_patterns=("OK",),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "no_expected_reply")
        self.assertEqual(result.returncode, 0)

    async def test_a_process_that_never_exits_is_reported_as_such(self) -> None:
        """Answering and then hanging is its own situation: no exit code was
        seen, so it is neither a success nor a plain timeout."""
        self._shorten_grace(1)
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo OK; sleep 30"],
            cwd=Path("."),
            timeout_seconds=20,
            success_patterns=("OK",),
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "no_clean_exit")
        self.assertIn("OK", result.stdout)
        self.assertIn("exit status is unknown", result.stderr)
        # Waited out the grace window, not the whole preflight budget.
        self.assertLess(result.elapsed_seconds, 15)

    async def test_the_grace_window_cannot_outlast_the_timeout(self) -> None:
        """The grace window is a shortcut off the preflight budget, so a long
        default must not extend a short timeout."""
        self._shorten_grace(30)
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo OK; sleep 30"],
            cwd=Path("."),
            timeout_seconds=1,
            success_patterns=("OK",),
        )
        self.assertFalse(result.ok)
        self.assertLess(result.elapsed_seconds, 10)

    async def test_cancellation_during_the_grace_window_still_wins(self) -> None:
        cancel_event = threading.Event()
        self._shorten_grace(10)

        async def _cancel_soon() -> None:
            await asyncio.sleep(0.5)
            cancel_event.set()

        canceller = asyncio.create_task(_cancel_soon())
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sh", "-c", "echo OK; sleep 30"],
            cwd=Path("."),
            timeout_seconds=20,
            success_patterns=("OK",),
            cancel_event=cancel_event,
        )
        await canceller
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "cancelled")
        self.assertLess(result.elapsed_seconds, 15)


class LegacyGeminiFallbackTest(unittest.TestCase):
    def setUp(self) -> None:
        # These are about `agy` vs the legacy `gemini` binary, not about
        # whether the Antigravity slot currently ships open.
        enable_all_slots(self)

    def test_legacy_gemini_alone_is_not_treated_as_available(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = cli_execution_policy.active()
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = "/usr/local/bin/gemini"
        self.assertFalse(adapters.command_exists("gemini"))

    def test_agy_present_but_no_confirmed_safe_model_is_not_available(self) -> None:
        """Legacy strict mode only. It refused to run Antigravity until a human
        had graded a model, on the theory that this protected the user from the
        "Use AI Credits" toggle — which the app could never actually read."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = "/opt/homebrew/bin/agy"
        adapters.legacy_gemini_path = None
        catalog = {"gemini": {"models": [{"id": "gemini-a", "billing_status": "unknown"}]}}
        self.assertFalse(adapters.command_exists("gemini", catalog=catalog))

    def test_agy_present_with_a_safe_model_but_no_selector_default_is_not_available(self) -> None:
        """A subscription_safe model alone isn't enough: without
        selector_default, a chair-selection failure has no safe fallback to
        land on and would fall through to agy's own unverified local
        default. The slot must stay disabled until one safe model is also
        flagged selector_default. Legacy strict mode only."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = "/opt/homebrew/bin/agy"
        adapters.legacy_gemini_path = None
        catalog = {"gemini": {"models": [{"id": "gemini-a", "billing_status": "subscription_safe"}]}}
        self.assertFalse(adapters.command_exists("gemini", catalog=catalog))

    def test_agy_present_with_a_confirmed_safe_default_is_available(self) -> None:
        """Legacy strict mode only."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = "/opt/homebrew/bin/agy"
        adapters.legacy_gemini_path = None
        catalog = {
            "gemini": {
                "models": [
                    {"id": "gemini-a", "billing_status": "subscription_safe", "selector_default": True}
                ]
            }
        }
        self.assertTrue(adapters.command_exists("gemini", catalog=catalog))

    def test_installed_agy_is_available_regardless_of_billing_catalog(self) -> None:
        """Existing CLI Mode: an installed, configured Antigravity runs. Every
        real gemini catalog entry is still billing_status=unknown — under the
        previous policy that alone disabled the slot permanently, which is the
        concrete cost that motivated the change."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = cli_execution_policy.active()
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = "/opt/homebrew/bin/agy"
        adapters.legacy_gemini_path = None
        self.assertTrue(adapters.command_exists("gemini"))

    def test_command_always_targets_agy_never_legacy_gemini(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = cli_execution_policy.active()
        adapters.agy_path = "/opt/homebrew/bin/agy"
        command = adapters._base_command("gemini", "hello")
        self.assertEqual(command[0], "agy")
        self.assertIn("--sandbox", command)


class PreflightReportsDisabledSlotDistinctlyTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Deliberately does NOT call enable_all_slots(): this test is about
        # how a disabled slot is reported.
        stub_claude_auth(self)
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = False
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)

    async def test_disabled_slot_is_not_reported_as_a_missing_cli(self) -> None:
        """"CLI not found" would send someone off installing a CLI that's
        already installed; the reason is that the slot is switched off."""
        original_command_exists = CliAdapters.command_exists
        CliAdapters.command_exists = lambda self, agent, catalog=None: False
        try:
            results, _warnings = await HealthChecker().preflight_all(cwd=Path("."), automation_level=1)
        finally:
            CliAdapters.command_exists = original_command_exists

        # All three slots are closed by default now (claude here via setUp,
        # codex and Antigravity because their MCP servers cannot be isolated),
        # so every one reports the switched-off reason rather than a missing
        # executable. Enabling one shows the other branch still works.
        for agent in ("claude", "codex", "gemini"):
            with self.subTest(agent=agent):
                self.assertEqual(results[agent].status, "slot_disabled")
                self.assertIn("switched off", results[agent].stderr)


class ClaudeSlotDisabledTest(unittest.TestCase):
    """The Claude slot is closed outright while stored-Keychain and managed-
    settings billing paths remain open (see config.CLAUDE_SLOT_ENABLED)."""

    def setUp(self) -> None:
        stub_claude_auth(self)
        self._original_which = cli_adapters_module.shutil.which
        cli_adapters_module.shutil.which = lambda name: f"/usr/local/bin/{name}"
        self.addCleanup(setattr, cli_adapters_module.shutil, "which", self._original_which)
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = False
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)

    def test_claude_is_currently_disabled_regardless_of_catalog(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        catalog = {
            "claude": {
                "models": [
                    {"id": "sonnet", "billing_status": "subscription_safe", "selector_default": True}
                ]
            }
        }
        self.assertFalse(adapters.command_exists("claude", catalog=catalog))

    def test_build_commands_explains_why_claude_is_disabled(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        _commands, warnings = adapters.build_commands({"claude": "hi"}, automation_level=2)
        self.assertTrue(
            any("switched off" in w for w in warnings),
            f"expected a warning explaining the disabled slot, got: {warnings}",
        )


class DisabledSlotIsEnforcedAtLaunchTest(unittest.IsolatedAsyncioTestCase):
    """command_exists() is the early exit, but a disabled slot must not depend
    on every caller remembering to consult it — ProcessRunner.run() enforces
    the same predicate right before launching."""

    def setUp(self) -> None:
        # The "other agent" in this class stands for any agent whose slot is
        # open; which agents ship open is a separate decision.
        enable_all_slots(self)
        stub_claude_auth(self)
        stub_claude_token(self)

    def _force_slot(self, enabled: bool) -> None:
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = enabled
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)

    def _spy_on_subprocess_launch(self) -> list:
        launched: list = []
        original = asyncio.create_subprocess_exec

        async def _spy(*args, **kwargs):
            launched.append(args)
            return await original(*args, **kwargs)

        asyncio.create_subprocess_exec = _spy
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)
        return launched

    async def test_disabled_claude_never_reaches_subprocess_launch(self) -> None:
        self._force_slot(False)
        launched = self._spy_on_subprocess_launch()
        result = await ProcessRunner().run(
            agent="claude", command=["/bin/echo", "LEAKED"], cwd=Path("."), timeout_seconds=5
        )
        self.assertEqual(result.status, "slot_disabled")
        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "")
        self.assertEqual(launched, [], "no subprocess may be started for a disabled slot")

    async def test_an_enabled_slot_still_launches(self) -> None:
        """The gate must track the flag, not block claude unconditionally."""
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = True
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)
        result = await ProcessRunner().run(
            agent="claude",
            command=[],
            cwd=Path("."),
            timeout_seconds=15,
            claude_spec=claude_command.ClaudeRunSpec(prompt="hi"),
        )
        self.assertNotIn(result.status, {"security_blocked", "auth_setup_required"})

    async def test_other_agents_are_unaffected(self) -> None:
        result = await ProcessRunner().run(
            agent="gemini", command=["/bin/echo", "OK"], cwd=Path("."), timeout_seconds=10
        )
        self.assertEqual(result.status, "ok")

    async def test_cli_runner_result_keeps_the_disabled_reason(self) -> None:
        """The generic backfill at the end of run_all() used to flatten this
        to status="skipped" / "CLI missing or skipped.", losing the reason."""
        self._force_slot(False)
        results, _warnings = await CliRunner(prefer_rtk=False).run_all(
            {"claude": "hi"}, cwd=Path("."), automation_level=2
        )
        self.assertEqual(results["claude"].status, "slot_disabled")
        self.assertIn("switched off", results["claude"].stderr)

    def test_command_exists_applies_the_predicate_to_every_agent(self) -> None:
        """The slot predicate must not live inside the claude branch, or
        disabling a different agent later would silently not take effect."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = cli_execution_policy.active()
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        original = config.is_agent_slot_enabled
        config.is_agent_slot_enabled = lambda agent: agent != "codex"
        self.addCleanup(setattr, config, "is_agent_slot_enabled", original)
        self.assertFalse(adapters.command_exists("codex"))

    def test_health_badge_reports_the_disabled_slot(self) -> None:
        self._force_slot(False)
        original_which = health_checker_module.shutil.which
        health_checker_module.shutil.which = lambda name: f"/usr/local/bin/{name}"
        self.addCleanup(setattr, health_checker_module.shutil, "which", original_which)
        status = HealthChecker()._check_claude()
        self.assertFalse(status.available)
        self.assertIn("switched off", status.detail)


class HealthCheckActuallyRunsTest(unittest.TestCase):
    """Every other health test calls one `_check_*` helper or stubs the whole
    checker, so nothing exercised `check_all()` end to end — and removing an
    import it depended on broke the GUI's startup badge without failing a
    single test. This one deliberately stubs nothing but the CLI lookups."""

    def test_check_all_returns_a_badge_for_every_tool(self) -> None:
        original_which = health_checker_module.shutil.which
        health_checker_module.shutil.which = lambda name: None
        self.addCleanup(setattr, health_checker_module.shutil, "which", original_which)
        # auto_start_lms=False so a missing LM Studio doesn't try to launch it;
        # the chair probe itself is a 2s localhost connection either way.
        statuses = HealthChecker().check_all(auto_start_lms=False)
        self.assertEqual(
            [status.name for status in statuses],
            ["lms", "claude", "Antigravity(agy)", "codex", "LM Studio"],
        )


# Classes below that pin _STRICT document the pre-Existing-CLI-Mode behavior.
# That policy is no longer active, but the code paths stay until Phase E, so
# the tests stay too — pinned explicitly rather than relying on a default.
_STRICT = cli_execution_policy.STRICT_SUBSCRIPTION

_DUMMY_TOKEN = "sk-ant-oat-TESTONLY-never-a-real-token"
# A stand-in for the verified executable path callers now pass down. Nothing
# is ever launched from it: the tests that use it stub the probes.
_FAKE_CLAUDE = "/nonexistent/verified/claude"

# Obsolete ClaudeTokenStoreTest, ClaudeAuthGuardTest, and ClaudeTokenInjectionTest removed in Phase E





class ProcessRunnerRedactsAtTheSourceTest(unittest.IsolatedAsyncioTestCase):
    """Redacting inside ProcessRunner means the CommandResult is already
    clean, so persistence, the GUI, LM Studio re-submission and chat history
    all inherit it without each having to remember."""

    def setUp(self) -> None:
        enable_all_slots(self)
        stub_claude_auth(self)
        stub_claude_token(self, _DUMMY_TOKEN)

    def test_captured_output_is_scrubbed_with_the_runs_own_token(self) -> None:
        decoded = ProcessRunner()._decode(
            [f"leaked={_DUMMY_TOKEN}".encode()], _DUMMY_TOKEN
        )
        self.assertNotIn(_DUMMY_TOKEN, decoded)
        self.assertIn(secret_redactor.REDACTED, decoded)

    async def test_other_agents_output_is_untouched(self) -> None:
        result = await ProcessRunner().run(
            agent="gemini", command=["/bin/echo", "plain output"], cwd=Path("."), timeout_seconds=10
        )
        self.assertEqual(result.stdout.strip(), "plain output")


class ClaudeCommandIsVerifiedTest(unittest.IsolatedAsyncioTestCase):
    """Token injection keys off the agent name, so the command has to be
    checked too — otherwise any binary passed as agent="claude" would receive
    the credential in its environment, where redacting our captured output
    wouldn't help: the process could write it anywhere."""

    def setUp(self) -> None:
        enable_all_slots(self)
        stub_claude_auth(self)
        stub_claude_token(self, _DUMMY_TOKEN)

    def _spy_launches(self) -> list:
        launched: list = []
        original = asyncio.create_subprocess_exec

        async def _spy(*args, **kwargs):
            launched.append(args)
            return await original(*args, **kwargs)

        asyncio.create_subprocess_exec = _spy
        self.addCleanup(setattr, asyncio, "create_subprocess_exec", original)
        return launched

    async def test_raw_argv_is_ignored_and_safe_flags_are_always_applied(self) -> None:
        """A caller passing bypassPermissions (or anything else) must not be
        able to influence what actually launches."""
        launched = self._spy_launches()
        await ProcessRunner(policy=_STRICT).run(
            agent="claude",
            command=[_claude_path(), "--permission-mode", "bypassPermissions", "-p", "unsafe"],
            cwd=Path("."),
            timeout_seconds=15,
            claude_spec=claude_command.ClaudeRunSpec(prompt="hi"),
        )
        self.assertEqual(len(launched), 1)
        argv = list(launched[0])
        self.assertNotIn("bypassPermissions", argv)
        self.assertIn("--permission-mode", argv)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "plan")
        self.assertIn("--setting-sources", argv)
        self.assertIn("--tools", argv)

    async def test_a_run_without_a_spec_is_blocked(self) -> None:
        launched = self._spy_launches()
        result = await ProcessRunner(policy=_STRICT).run(
            agent="claude", command=["/bin/echo", "hi"], cwd=Path("."), timeout_seconds=5
        )
        self.assertEqual(result.status, "security_blocked")
        self.assertEqual(launched, [], "the token must not reach another program")

    async def test_other_agents_are_not_command_restricted(self) -> None:
        result = await ProcessRunner(policy=_STRICT).run(
            agent="gemini", command=["/bin/echo", "OK"], cwd=Path("."), timeout_seconds=5
        )
        self.assertEqual(result.status, "ok")


class IsolationFailureBlocksEvenTheProbeTest(unittest.IsolatedAsyncioTestCase):
    """The guard itself launches claude, so isolation has to be confirmed
    before it runs — otherwise a failed mkdtemp would still start a Claude
    process holding the token in an un-isolated environment."""

    def setUp(self) -> None:
        stub_claude_auth(self)
        stub_claude_token(self, _DUMMY_TOKEN)




class ExistingCliModeIsTheActivePolicyTest(unittest.TestCase):
    def test_exactly_one_policy_is_active(self) -> None:
        """Availability, preflight, and launch all read this. Two live
        policies is how a health badge starts promising something the launch
        then refuses."""
        self.assertIs(cli_execution_policy.active(), cli_execution_policy.EXISTING_CONFIG)

    def test_the_active_policy_defers_to_the_user(self) -> None:
        policy = cli_execution_policy.active()
        self.assertTrue(policy.inherit_user_environment)
        self.assertTrue(policy.inherit_user_cli_config)
        self.assertFalse(hasattr(policy, "inject_app_claude_token"))
        self.assertFalse(policy.gate_availability_on_billing)
        self.assertFalse(policy.wrap_ai_cli_in_rtk)

    def test_runner_adapters_and_health_share_one_policy(self) -> None:
        runner = CliRunner(prefer_rtk=False)
        self.assertIs(runner.policy, runner.adapters.policy)
        self.assertIs(runner.policy, runner.process_runner.policy)
        self.assertIs(HealthChecker().policy, cli_execution_policy.active())


class UserEnvironmentIsInheritedTest(unittest.TestCase):
    """Stripping the user's API keys and provider settings was an attempt to
    force a billing path this app could never actually guarantee. What it
    reliably did was break setups the user had chosen on purpose."""

    REPRESENTATIVE = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "CLAUDE_CODE_USE_BEDROCK",
        "ANTHROPIC_PROFILE",
    )

    def setUp(self) -> None:
        for name in self.REPRESENTATIVE:
            os.environ[name] = f"value-for-{name}"
            self.addCleanup(os.environ.pop, name, None)

    def test_representative_billing_vars_reach_the_child(self) -> None:
        env, _isolated = ProcessRunner()._child_env("claude")
        for name in self.REPRESENTATIVE:
            self.assertEqual(env.get(name), f"value-for-{name}", name)

    def test_the_child_environment_is_the_parent_environment(self) -> None:
        env, isolated = ProcessRunner()._child_env("claude")
        self.assertEqual(env, dict(os.environ))
        self.assertIsNone(isolated, "no throwaway profile directory under existing_config")

    def test_no_app_marker_is_injected(self) -> None:
        env, _isolated = ProcessRunner()._child_env("codex")
        self.assertNotIn("AI_BRAINSTORM_SUBSCRIPTION_ONLY", env)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)

    def test_strict_mode_still_strips_them(self) -> None:
        """The old behavior is still reachable and still tested until Phase E."""
        env, _isolated = ProcessRunner(policy=_STRICT)._child_env("codex")
        for name in self.REPRESENTATIVE:
            self.assertNotIn(name, env, name)


class InheritedSecretsAreNeverPrintedTest(unittest.TestCase):
    """Inheriting a credential and displaying it are different things. The
    app keeps the first and must still refuse the second — without reading
    any environment variable to work out what to look for."""

    def test_credential_shapes_are_redacted_without_being_told(self) -> None:
        samples = {
            "sk-ant-api03-" + "a" * 32,
            "sk-proj-" + "b" * 32,
            "AIza" + "C" * 32,
            "ya29." + "d" * 32,
            "ghp_" + "e" * 32,
            "AKIA" + "F" * 16,
        }
        for value in samples:
            cleaned = secret_redactor.redact(f"error: key {value} was rejected")
            self.assertNotIn(value, cleaned, value[:10])
            self.assertIn(secret_redactor.REDACTED, cleaned)

    def test_ordinary_output_survives(self) -> None:
        text = "Reading src/main.py and writing a plan. No secrets here."
        self.assertEqual(secret_redactor.redact(text), text)

    def test_redaction_does_not_read_the_environment(self) -> None:
        """Structural patterns and already-known in-process values only:
        enumerating os.environ to learn what to scrub would mean touching
        every credential on the machine."""
        source = Path("src/services/secret_redactor.py").read_text()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("import os", source)


class AiCliIsNotWrappedInRtkTest(unittest.TestCase):
    """Measured 2026-08-16: rtk saves 0% on all three AI CLIs and persists the
    full command line — and so the whole prompt — to its history database."""

    def _adapters(self) -> CliAdapters:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = cli_execution_policy.active()
        adapters.prefer_rtk = True
        adapters.rtk_path = "/opt/homebrew/bin/rtk"
        adapters.agy_path = "/opt/homebrew/bin/agy"
        adapters.legacy_gemini_path = None
        return adapters

    def test_no_agent_is_wrapped_even_when_rtk_is_installed(self) -> None:
        adapters = self._adapters()
        for agent in ("claude", "gemini", "codex"):
            self.assertFalse(adapters.uses_rtk(agent), agent)

    def test_built_argv_never_starts_with_rtk(self) -> None:
        adapters = self._adapters()
        commands, _warnings = adapters.build_commands(
            {"claude": "hi", "gemini": "hi", "codex": "hi"}
        )
        for agent, command in commands.items():
            self.assertNotEqual(command[0], "rtk", f"{agent}: {command}")

    def test_missing_rtk_does_not_issue_warning_under_existing_config(self) -> None:
        adapters = self._adapters()
        adapters.rtk_path = None
        _commands, warnings = adapters.build_commands(
            {"claude": "hi", "gemini": "hi", "codex": "hi"}
        )
        self.assertFalse(any("RTK not found" in w for w in warnings))


class ExistingCliModeRemovesTheOldGatesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        enable_all_slots(self)
        self.adapters = CliAdapters.__new__(CliAdapters)
        self.adapters.policy = cli_execution_policy.active()
        self.adapters.prefer_rtk = False
        self.adapters.rtk_path = None
        self.adapters.agy_path = "/opt/homebrew/bin/agy"
        self.adapters.legacy_gemini_path = None

    def test_claude_is_available_without_an_app_token_or_signature_check(self) -> None:
        if not shutil.which("claude"):
            self.skipTest("claude is not installed here")
        self.assertTrue(self.adapters.command_exists("claude"))

    def test_antigravity_is_available_with_an_ungraded_catalog(self) -> None:
        catalog = {"gemini": {"models": [{"id": "g", "billing_status": "unknown"}]}}
        self.assertTrue(self.adapters.command_exists("gemini", catalog=catalog))

    def test_claude_argv_carries_no_settings_isolation_and_no_model(self) -> None:
        command = self.adapters._base_command("claude", "hi")
        self.assertNotIn("--setting-sources", command)
        self.assertNotIn("--settings", command)
        self.assertNotIn("--model", command)

    def test_claude_argv_keeps_the_read_only_constraints(self) -> None:
        """These are about not letting an AI edit the user's project, which
        has nothing to do with billing and does not relax with the policy."""
        command = self.adapters._base_command("claude", "hi")
        self.assertEqual(command[command.index("--permission-mode") + 1], "plan")
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", command)
        self.assertEqual(command[-2], "-p")

    def test_codex_argv_no_longer_overrides_user_config(self) -> None:
        """Existing CLI Mode stopped replacing the user's auth/provider/model.

        `--ignore-rules` is deliberately NOT in this list. It declines the
        execpolicy layer, which can permit commands to run outside the
        sandbox; that is a safety boundary, not a billing or provider
        override, so it stays on every run. See
        test_codex_argv_declines_execpolicy_rules.
        """
        command = self.adapters._base_command("codex", "hi")
        joined = " ".join(command)
        for banned in (
            "--ignore-user-config",
            "forced_login_method",
            "model_provider",
            "openai_base_url",
            "chatgpt_base_url",
        ):
            self.assertNotIn(banned, joined, banned)

    def test_codex_argv_keeps_the_read_only_sandbox(self) -> None:
        command = self.adapters._base_command("codex", "hi")
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertIn("--ephemeral", command)

    def test_antigravity_argv_keeps_plan_and_sandbox(self) -> None:
        command = self.adapters._base_command("gemini", "hi")
        self.assertEqual(command[0], "agy")
        self.assertEqual(command[command.index("--mode") + 1], "plan")
        self.assertIn("--sandbox", command)
        self.assertEqual(command[-2], "-p")

    async def test_claude_runs_without_a_stored_token(self) -> None:
        """The token gate was the single reason an otherwise working Claude
        login could not be used."""
        stub_claude_token(self, None)
        captured: dict = {}
        original = process_runner_module.ProcessRunner._run_with_env

        async def _capture(_self, agent, command, *a, **kw):
            captured["command"] = command
            return CommandResult(agent=agent, command=command, ok=True, status="ok")

        process_runner_module.ProcessRunner._run_with_env = _capture
        self.addCleanup(setattr, process_runner_module.ProcessRunner, "_run_with_env", original)

        result = await ProcessRunner().run(
            agent="claude",
            command=[],
            cwd=Path("."),
            timeout_seconds=5,
            claude_spec=claude_command.ClaudeRunSpec(prompt="hi"),
        )
        self.assertEqual(result.status, "ok")
        self.assertNotIn("--model", captured["command"])


class NoLoginCommandIsEverInvokedTest(unittest.TestCase):
    """The app explains how to log in; it never does it. A setup command in
    the source would be a silent authentication change."""

    def test_no_login_or_setup_command_appears_in_launch_code(self) -> None:
        banned = ("setup-token", "codex login", "codex logout", "auth login", "gcloud auth")
        for name in (
            "cli_adapters.py",
            "cli_runner.py",
            "process_runner.py",
            "health_checker.py",
            "claude_command.py",
        ):
            source = Path("src/services") / name
            text = source.read_text()
            for phrase in banned:
                self.assertNotIn(phrase, text, f"{name} mentions {phrase}")


class RecoveryGuidanceTest(unittest.TestCase):
    def test_every_normalized_status_has_japanese_guidance(self) -> None:
        for status in (
            "command_missing",
            "auth_required",
            "rate_limited",
            "permission_error",
            "config_error",
            "timeout",
            "cancelled",
            "unknown",
        ):
            guidance = cli_status.guidance_for(status)
            self.assertNotEqual(guidance, status)
            self.assertTrue(guidance.strip())

    def test_auth_required_is_not_described_as_a_missing_cli(self) -> None:
        """Sending someone to reinstall a CLI that is installed and merely
        logged out is the most expensive wrong message this app can print."""
        guidance = cli_status.guidance_for("auth_required")
        self.assertIn("ログイン", guidance)
        self.assertNotIn("インストール", guidance)

    def test_guidance_never_offers_to_log_in_for_the_user(self) -> None:
        for status in ("auth_required", "config_error", "api_key_blocked"):
            self.assertNotIn("代行します", cli_status.guidance_for(status))

    def test_an_unknown_status_still_gets_guidance(self) -> None:
        self.assertEqual(
            cli_status.guidance_for("something-new"), cli_status.guidance_for("unknown")
        )

    def test_all_failed_output_names_each_agent(self) -> None:
        message = cli_status.all_failed_guidance(
            {"claude": "auth_required", "codex": "timeout"}
        )
        self.assertIn("claude", message)
        self.assertIn("codex", message)
        self.assertIn(cli_status.guidance_for("auth_required"), message)




class ClaudeArgvIsSingleSourcedTest(unittest.TestCase):
    def test_adapter_and_runner_build_the_same_argv(self) -> None:
        """Two definitions of the flags would be two chances to diverge."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        from_adapter = adapters._base_command("claude", "hi", "sonnet", "high")
        from_builder = claude_command.build(  # same policy as the adapter above
            claude_command.ClaudeRunSpec(prompt="hi", model_id="sonnet", effort="high"),
            _claude_path(),
            _STRICT,
        )
        self.assertEqual(from_adapter, from_builder)

    def test_an_unverified_model_is_replaced_with_the_confirmed_default(self) -> None:
        model_id, effort = agent_model_selector.validated_model_and_effort(
            "claude", "fable", "max"
        )
        self.assertEqual(model_id, "sonnet")

    def test_security_blocked_is_a_notable_status(self) -> None:
        explanation = cli_status.guidance_for("security_blocked")
        self.assertNotEqual(explanation, "security_blocked")


class ClaudeIsNeverWrappedTest(unittest.TestCase):
    """rtk is a separate process that would receive the child environment,
    including the injected token. Token savings aren't worth handing a
    credential to a binary this app doesn't audit."""

    def setUp(self) -> None:
        stub_claude_auth(self)

    def _adapters_with_rtk(self) -> CliAdapters:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = True
        adapters.rtk_path = "/usr/local/bin/rtk"
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        return adapters

    def test_claude_command_is_not_wrapped_in_rtk(self) -> None:
        commands, _warnings = self._adapters_with_rtk().build_commands(
            {"claude": "hi"}, automation_level=2
        )
        self.assertNotEqual(commands["claude"][0], "rtk")
        # Resolved absolute path, so the binary that was verified is the one
        # that runs.
        self.assertTrue(commands["claude"][0].endswith("claude"))
        self.assertTrue(commands["claude"][0].startswith("/"))

    def test_uses_rtk_is_false_for_claude_and_true_for_others(self) -> None:
        adapters = self._adapters_with_rtk()
        self.assertFalse(adapters.uses_rtk("claude"))
        self.assertTrue(adapters.uses_rtk("codex"))

    def test_other_agents_are_still_wrapped(self) -> None:
        commands, _warnings = self._adapters_with_rtk().build_commands(
            {"codex": "hi"}, automation_level=2
        )
        self.assertEqual(commands["codex"][0], "rtk")


class SecretRedactorTest(unittest.TestCase):
    """Assertions here compare booleans/lengths rather than echoing text, so
    a failure message can't print the secret it is checking for."""

    def test_token_is_replaced_everywhere_it_appears(self) -> None:
        redacted = secret_redactor.redact(
            f"before {_DUMMY_TOKEN} middle {_DUMMY_TOKEN} after", _DUMMY_TOKEN
        )
        self.assertNotIn(_DUMMY_TOKEN, redacted)
        self.assertEqual(redacted.count(secret_redactor.REDACTED), 2)

    def test_text_is_untouched_when_no_token_is_registered(self) -> None:
        self.assertEqual(secret_redactor.redact("nothing to hide", None), "nothing to hide")

    def test_very_short_secrets_are_not_redacted(self) -> None:
        """Blanking every occurrence of a short string would corrupt
        unrelated output."""
        self.assertEqual(secret_redactor.redact("abc def abc", "abc"), "abc def abc")

    def test_longest_secret_is_replaced_first(self) -> None:
        long_secret = f"{_DUMMY_TOKEN}-extended"
        redacted = secret_redactor.redact(long_secret, _DUMMY_TOKEN, long_secret)
        self.assertNotIn(_DUMMY_TOKEN, redacted)
        self.assertEqual(redacted, secret_redactor.REDACTED)






class ClaudeSafeDefaultGateTest(unittest.TestCase):
    """Claude gets the same fail-closed posture as gemini: command_exists()
    requires a confirmed billing_status=subscription_safe + selector_default
    model, not merely the `claude` executable being on PATH — so a missing
    or corrupt agent_models.json disables claude entirely instead of letting
    it fall through to an unverified local default.

    These assert the catalog gate itself, so they run with the slot flag
    forced on; the slot being off today is covered by ClaudeSlotDisabledTest."""

    def setUp(self) -> None:
        stub_claude_auth(self)
        self._original_which = cli_adapters_module.shutil.which
        self.addCleanup(self._restore_which)
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = True
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)

    def _restore_which(self) -> None:
        cli_adapters_module.shutil.which = self._original_which

    def _stub_which_claude_present(self) -> None:
        cli_adapters_module.shutil.which = lambda name: "/usr/local/bin/claude" if name == "claude" else None

    def test_claude_missing_binary_is_not_available(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        cli_adapters_module.shutil.which = lambda name: None
        self.assertFalse(adapters.command_exists("claude"))

    def test_claude_present_but_corrupt_catalog_is_not_available(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        self._stub_which_claude_present()
        self.assertFalse(adapters.command_exists("claude", catalog={}))

    def test_claude_present_but_no_selector_default_is_not_available(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        self._stub_which_claude_present()
        catalog = {"claude": {"models": [{"id": "sonnet", "billing_status": "subscription_safe"}]}}
        self.assertFalse(adapters.command_exists("claude", catalog=catalog))

    def test_claude_present_with_a_confirmed_safe_default_is_available(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        self._stub_which_claude_present()
        catalog = {
            "claude": {
                "models": [
                    {"id": "sonnet", "billing_status": "subscription_safe", "selector_default": True}
                ]
            }
        }
        self.assertTrue(adapters.command_exists("claude", catalog=catalog))

    def test_real_catalog_has_a_confirmed_claude_default(self) -> None:
        """Sanity check: the real catalog still ships sonnet as
        subscription_safe + selector_default, so re-enabling the slot won't
        immediately trip the catalog gate."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        self._stub_which_claude_present()
        self.assertTrue(adapters.command_exists("claude"))


class ModelAliasEnvVarBlockedTest(unittest.TestCase):
    """The catalog passes claude a model *alias* (e.g. "sonnet"), not a
    pinned full model ID — these env vars can redirect what that alias
    resolves to (per Claude Code's own docs), so an explicit --model sonnet
    is only actually safe if they're stripped from the child process env.
    The profile/federation vars are here for a related reason: Claude Code's
    authentication precedence ranks them *above* the /login subscription
    credential, so leaving them set could route a run through a billed path."""

    ALIAS_REDIRECT_VARS = (
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_CUSTOM_MODEL_OPTION",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
        "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
        "ANTHROPIC_PROFILE",
        "ANTHROPIC_FEDERATION_RULE_ID",
        "ANTHROPIC_ORGANIZATION_ID",
        "ANTHROPIC_IDENTITY_TOKEN_FILE",
        "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
        "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
        "CLAUDE_CODE_OAUTH_SCOPES",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_AWS_API_KEY",
        "ANTHROPIC_AWS_BASE_URL",
        "ANTHROPIC_AWS_WORKSPACE_ID",
        "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
        "ANTHROPIC_FOUNDRY_API_KEY",
        "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
        "ANTHROPIC_FOUNDRY_BASE_URL",
        "ANTHROPIC_FOUNDRY_RESOURCE",
        "ANTHROPIC_VERTEX_BASE_URL",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    )

    def test_alias_redirect_vars_are_in_the_blocklist(self) -> None:
        for name in self.ALIAS_REDIRECT_VARS:
            self.assertIn(name, config.BLOCKED_CHILD_ENV_VARS, f"{name} must be stripped from child CLI env")

    def test_child_env_actually_strips_them(self) -> None:
        """Only compares key sets, never asserts on the env dict itself —
        a failed assertNotIn(name, child_env, ...) would dump the entire
        child_env (i.e. the real process environment, which may contain
        unrelated secrets) into the test failure message."""
        runner = ProcessRunner(policy=_STRICT)
        original_environ = dict(os.environ)
        try:
            for name in self.ALIAS_REDIRECT_VARS:
                os.environ[name] = "claude-fable-5"
            child_env, isolated_dir = runner._child_env()
            child_env_keys = set(child_env.keys())
            runner._discard_isolated_config_dir(isolated_dir)
        finally:
            os.environ.clear()
            os.environ.update(original_environ)

        leaked = set(self.ALIAS_REDIRECT_VARS) & child_env_keys
        self.assertEqual(leaked, set(), f"these env var names leaked into the CLI child process env: {leaked}")


class ProjectRunRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ProjectRunRegistry()
        self.dir = Path(tempfile.mkdtemp())
        self.proj_a = self.dir / "a"
        self.proj_b = self.dir / "b"
        self.proj_a.mkdir()
        self.proj_b.mkdir()

    def test_second_start_on_the_same_path_is_rejected(self) -> None:
        first = self.registry.try_start(self.proj_a, "tab1")
        self.assertIsNotNone(first)
        second = self.registry.try_start(self.proj_a, "tab2")
        self.assertIsNone(second)

    def test_different_paths_never_block_each_other(self) -> None:
        first = self.registry.try_start(self.proj_a, "tab1")
        second = self.registry.try_start(self.proj_b, "tab2")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)

    def test_release_frees_the_slot_for_a_new_run(self) -> None:
        first = self.registry.try_start(self.proj_a, "tab1")
        self.registry.release(first)
        second = self.registry.try_start(self.proj_a, "tab2")
        self.assertIsNotNone(second)

    def test_release_with_a_stale_run_id_does_not_free_a_newer_run(self) -> None:
        """A slow-to-clean-up old run must not release a run that superseded it."""
        first = self.registry.try_start(self.proj_a, "tab1")
        self.registry.release(first)
        second = self.registry.try_start(self.proj_a, "tab2")
        self.registry.release(first)  # stale release from the old (already-released) run
        self.assertIs(self.registry.active_run_for(self.proj_a), second)

    def test_run_ids_are_unique(self) -> None:
        first = self.registry.try_start(self.proj_a, "tab1")
        self.registry.release(first)
        second = self.registry.try_start(self.proj_a, "tab1")
        self.assertNotEqual(first.run_id, second.run_id)

    def test_reservation_has_no_room_or_request_yet(self) -> None:
        reservation = self.registry.try_start(self.proj_a, "tab1")
        self.assertEqual(reservation.room_id, "")
        self.assertEqual(reservation.request, "")

    def test_finalize_fills_in_parameters_and_keeps_identity(self) -> None:
        reservation = self.registry.try_start(self.proj_a, "tab1")
        finalized = self.registry.finalize(reservation, room_id="room_1", request="hello", automation_level=2)
        self.assertEqual(finalized.run_id, reservation.run_id)
        self.assertIs(finalized.cancel_event, reservation.cancel_event)
        self.assertEqual(finalized.room_id, "room_1")
        self.assertEqual(finalized.request, "hello")
        self.assertEqual(finalized.automation_level, 2)
        # The registry itself must reflect the finalized version.
        self.assertIs(self.registry.active_run_for(self.proj_a), finalized)

    def test_release_before_finalize_frees_the_slot(self) -> None:
        """A room-creation failure between reserve and finalize must not leak the slot."""
        reservation = self.registry.try_start(self.proj_a, "tab1")
        self.registry.release(reservation)
        second = self.registry.try_start(self.proj_a, "tab2")
        self.assertIsNotNone(second)

    def test_active_run_for_reports_none_when_nothing_is_running(self) -> None:
        self.assertIsNone(self.registry.active_run_for(self.proj_a))

    def test_finalize_on_a_released_reservation_returns_none(self) -> None:
        """finalize() must never hand back a context the registry doesn't
        actually own — a caller treating a stale reservation as valid would
        start a run outside the one-per-path guarantee."""
        reservation = self.registry.try_start(self.proj_a, "tab1")
        self.registry.release(reservation)
        result = self.registry.finalize(reservation, room_id="r", request="req", automation_level=2)
        self.assertIsNone(result)

    def test_finalize_on_a_reservation_superseded_by_a_newer_run_returns_none(self) -> None:
        old = self.registry.try_start(self.proj_a, "tab1")
        self.registry.release(old)
        new = self.registry.try_start(self.proj_a, "tab2")
        result = self.registry.finalize(old, room_id="r", request="req", automation_level=2)
        self.assertIsNone(result)
        # The newer run's slot must be untouched by the stale finalize() call.
        self.assertIs(self.registry.active_run_for(self.proj_a), new)


class _StubWorkspace:
    def write_prompt(self, *args, **kwargs) -> None:
        pass

    def write_session_artifact(self, *args, **kwargs) -> None:
        pass


class _FakeCliRunnerCancelsDuringRound:
    """Simulates the user clicking Cancel while this round's CLIs are mid-flight."""

    async def run_all(self, prompts, cwd, automation_level, progress, cancel_event, agent_selection=None):
        cancel_event.set()
        results = {
            agent: CommandResult(agent=agent, command=[], ok=False, status="cancelled")
            for agent in prompts
        }
        return results, []


class CancelSkipsChairCallTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_a_round_skips_that_rounds_chair_summary(self) -> None:
        """A round cancelled mid-flight must not still pay for an LM Studio call."""
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = ChairAgent()
        loop.prompt_builder = PromptBuilder(loop.chair)
        loop.cli_runner = _FakeCliRunnerCancelsDuringRound()

        summarize_calls = []

        def fake_summarize(*args, **kwargs):
            summarize_calls.append(1)
            return "should not be reached"

        loop._summarize_round = fake_summarize

        cancel_event = threading.Event()
        role_rounds = RoleOrchestrator().build_plan({"claude"}, automation_level=2)

        prompts, results, summaries, warnings = await loop._run_role_rounds(
            project_root=Path("."),
            user_request="test request",
            context_pack="test context",
            session_id="20260101-000000",
            workspace=_StubWorkspace(),
            role_rounds=role_rounds,
            available_agents={"claude"},
            automation_level=2,
            progress=None,
            cancel_event=cancel_event,
        )

        self.assertEqual(summarize_calls, [], "chair must not be called for a round cancelled mid-flight")
        self.assertEqual(summaries, [])
        # Whatever CLI output did come back before cancellation is still recorded.
        self.assertTrue(any(result.status == "cancelled" for result in results.values()))


class RunSkipsChairWhenAlreadyCancelledTest(unittest.IsolatedAsyncioTestCase):
    async def test_run_returns_immediately_without_touching_chair(self) -> None:
        """If cancelled before the worker thread's run() call even starts, the
        chair-availability probe (and possible LM Studio auto-start) must not
        fire. loop.chair is deliberately left unset: touching it would raise
        AttributeError and fail the test, proving it was never accessed."""
        loop = RefinementLoop.__new__(RefinementLoop)
        cancel_event = threading.Event()
        cancel_event.set()

        result = await loop.run(Path("."), "test request", 2, None, cancel_event)

        self.assertIn("キャンセル", result.final_answer)
        self.assertEqual(result.command_results, {})


class _FakeChairAlwaysAvailable:
    def available(self) -> bool:
        return True

    def invalidate_cache(self) -> None:
        pass


class _FakeChairUnavailable:
    def available(self) -> bool:
        return False


class _CallCountedCancel:
    """A stand-in for RefinementLoop._is_cancelled that returns False for the
    first `false_calls` invocations and True afterward — lets a test target
    one *specific* cancellation checkpoint among several in run(), instead of
    accidentally satisfying an earlier one."""

    def __init__(self, false_calls: int):
        self.false_calls = false_calls
        self.calls = 0

    def __call__(self, _cancel_event: object | None) -> bool:
        self.calls += 1
        return self.calls > self.false_calls


class ContextPackGenerationCancelTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_right_before_context_pack_generation_is_caught_there(self) -> None:
        """Cancellation landing strictly between the post-preflight check and
        context-pack generation must be caught by THAT check specifically —
        not merely by an earlier one (which would make this pass vacuously).
        loop.prompt_builder is deliberately left unset: reaching
        build_context_documents() would raise AttributeError and fail the test."""
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = _FakeChairAlwaysAvailable()
        # run()-start check (call 1) and the post-preflight check (call 2)
        # must both see "not cancelled"; only the context-pack check (call 3)
        # sees "cancelled".
        loop._is_cancelled = _CallCountedCancel(false_calls=2)

        async def fake_preflight_all(self, cwd, automation_level, prefer_rtk, progress, cancel_event):
            return {"claude": CommandResult(agent="claude", command=[], ok=True, status="ok")}, []

        original_preflight = HealthChecker.preflight_all
        HealthChecker.preflight_all = fake_preflight_all
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            result = await loop.run(Path(tmp_dir.name), "test request", 2, None, threading.Event())
        finally:
            HealthChecker.preflight_all = original_preflight
            tmp_dir.cleanup()

        self.assertIn("キャンセル", result.final_answer)
        self.assertEqual(result.context_pack, "")

    async def test_cancellation_during_context_pack_chat_stops_subsequent_writes(self) -> None:
        """build_context_pack() can return a normal-looking fallback pack when
        cancellation lands mid-flight inside its own chair.chat() call —
        that's indistinguishable from a real success to the caller. run()
        must re-check cancellation right after and stop before writing any
        context files, building role rounds, etc."""
        loop = RefinementLoop.__new__(RefinementLoop)
        cancel_event = threading.Event()

        class _CancellingChair:
            def available(self) -> bool:
                return True

            def chat(self, *args, **kwargs):
                cancel_event.set()  # simulate cancellation while this chair call was in flight
                return None  # ChairAgent.chat() returns None on failure; triggers the fallback path

        loop.chair = _CancellingChair()
        loop.prompt_builder = PromptBuilder(loop.chair)

        async def fake_preflight_all(self, cwd, automation_level, prefer_rtk, progress, cancel_event):
            return {"claude": CommandResult(agent="claude", command=[], ok=True, status="ok")}, []

        original_preflight = HealthChecker.preflight_all
        HealthChecker.preflight_all = fake_preflight_all
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            project_root = Path(tmp_dir.name)
            result = await loop.run(project_root, "test request", 2, None, cancel_event)
            # workspace.initialize() writes an empty placeholder; only a real
            # write_context_files() call (which must not happen once
            # cancelled) would overwrite it with actual content.
            context_pack_path = project_root / ".ai-brainstorm" / "context_pack.md"
            self.assertEqual(context_pack_path.read_text(encoding="utf-8"), "")
        finally:
            HealthChecker.preflight_all = original_preflight
            tmp_dir.cleanup()

        self.assertIn("キャンセル", result.final_answer)


class AgentSelectionCancelInRunTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_right_after_agent_selection_stops_before_role_rounds(self) -> None:
        """Cancellation landing strictly between agent_model_selector.select()
        returning and the role rounds starting must be caught by THAT check
        specifically — not merely by an earlier one (which would make this
        pass vacuously). loop.cli_runner is deliberately left unset: reaching
        _run_role_rounds() would raise AttributeError and fail the test."""
        loop = RefinementLoop.__new__(RefinementLoop)

        class _FakeChairAvailableWithChat:
            def available(self) -> bool:
                return True

            def invalidate_cache(self) -> None:
                pass

            def chat(self, *args, **kwargs):
                return "OK"

        loop.chair = _FakeChairAvailableWithChat()
        loop.prompt_builder = PromptBuilder(loop.chair)
        loop.role_orchestrator = RoleOrchestrator()
        # chair.available() is True, so the pre-autostart/post-autostart
        # checks (which live inside `if not self.chair.available():`) are
        # skipped entirely. The 4 earlier checks that do run (run()-start,
        # post-preflight, pre-context-pack, post-context-pack) must all see
        # "not cancelled"; only the 5th (right after
        # agent_model_selector.select()) sees "cancelled".
        loop._is_cancelled = _CallCountedCancel(false_calls=4)

        async def fake_preflight_all(self, cwd, automation_level, prefer_rtk, progress, cancel_event):
            return {"claude": CommandResult(agent="claude", command=[], ok=True, status="ok")}, []

        original_preflight = HealthChecker.preflight_all
        HealthChecker.preflight_all = fake_preflight_all
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            result = await loop.run(Path(tmp_dir.name), "test request", 2, None, threading.Event())
        finally:
            HealthChecker.preflight_all = original_preflight
            tmp_dir.cleanup()

        self.assertIn("キャンセル", result.final_answer)


class LmStudioAutoStartCancelTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_before_autostart_skips_ensure_server_running(self) -> None:
        """A cancellation landing between the run()-start check and the LM
        Studio auto-start attempt must stop the (up to 8s) autostart call."""
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = _FakeChairUnavailable()
        loop._is_cancelled = _CallCountedCancel(false_calls=1)

        ensure_calls = []

        class _FakeLmManager:
            def ensure_server_running(self, timeout_seconds, cancel_event=None):
                ensure_calls.append(1)
                return False, "should not be called"

        loop.lm_manager = _FakeLmManager()

        result = await loop.run(Path("."), "test request", 2, None, threading.Event())

        self.assertEqual(ensure_calls, [], "ensure_server_running must not be called once cancelled")
        self.assertIn("キャンセル", result.final_answer)

    async def test_cancelled_during_autostart_skips_scan_and_preflight(self) -> None:
        """If ensure_server_running() returns because it was cancelled
        mid-poll, run() must stop right there — not proceed to scan the
        project, initialize .ai-brainstorm/, or run preflight."""
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = _FakeChairUnavailable()

        class _FakeLmManager:
            def ensure_server_running(self, timeout_seconds, cancel_event=None):
                cancel_event.set()  # simulate the poll loop detecting cancellation
                return False, "Cancelled while waiting for LM Studio to start."

        loop.lm_manager = _FakeLmManager()

        # loop.prompt_builder/role_orchestrator are deliberately left unset:
        # reaching the scan/preflight/context-pack stage would need them and
        # raise AttributeError, proving run() didn't proceed that far.
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            project_root = Path(tmp_dir.name)
            result = await loop.run(project_root, "test request", 2, None, threading.Event())
            self.assertFalse((project_root / ".ai-brainstorm").exists(), ".ai-brainstorm/ must not be created")
        finally:
            tmp_dir.cleanup()

        self.assertIn("キャンセル", result.final_answer)


class ChairCallCancelGuardTest(unittest.TestCase):
    """Unit-level checks that _integrate/_finalize/_summarize_round/
    PromptBuilder.build_context_pack each skip their own chair.chat() call
    when cancelled immediately beforehand — closing the gap where an outer
    caller's check is separated from the actual HTTP call by some
    (potentially non-trivial, e.g. preprocessing) synchronous work."""

    def _fake_chair(self, calls: list) -> object:
        class _FakeChair:
            def chat(self, *args, **kwargs):
                calls.append(1)
                return "should not be reached"

        return _FakeChair()

    def test_integrate_skips_chat_when_cancelled(self) -> None:
        loop = RefinementLoop.__new__(RefinementLoop)
        calls: list = []
        loop.chair = self._fake_chair(calls)
        cancel_event = threading.Event()
        cancel_event.set()

        loop._integrate("req", "ctx", "answers", cancel_event)

        self.assertEqual(calls, [])

    def test_finalize_skips_chat_when_cancelled(self) -> None:
        loop = RefinementLoop.__new__(RefinementLoop)
        calls: list = []
        loop.chair = self._fake_chair(calls)
        cancel_event = threading.Event()
        cancel_event.set()
        results = {"round1_claude_author": CommandResult(agent="claude", command=[], ok=True, status="ok")}

        result = loop._finalize("req", "integrated", "refine", results, "answers", cancel_event)

        self.assertEqual(calls, [])
        self.assertIn("キャンセル", result)

    def test_summarize_round_skips_chat_when_cancelled(self) -> None:
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.preprocessor = ResponsePreprocessor()
        calls: list = []
        loop.chair = self._fake_chair(calls)
        cancel_event = threading.Event()
        cancel_event.set()
        role_round = RoleOrchestrator().build_plan({"claude"}, automation_level=1)[0]
        results = {"round1_claude_author": CommandResult(agent="claude", command=[], ok=True, status="ok")}

        loop._summarize_round("req", "ctx", role_round, results, "", cancel_event)

        self.assertEqual(calls, [])

    def test_build_context_pack_skips_chat_when_cancelled(self) -> None:
        calls: list = []
        builder = PromptBuilder(self._fake_chair(calls))
        cancel_event = threading.Event()
        cancel_event.set()
        scan = ScanResult(project_root=Path("."), tree=[], important_files={}, vendor_paths=[])

        builder.build_context_pack(scan, "req", "raw context", cancel_event)

        self.assertEqual(calls, [])


class IntegrateSkipsBeforeChatCallTest(unittest.TestCase):
    def test_cancel_during_preprocessing_skips_integrate_call(self) -> None:
        """A cancellation landing while _combined_role_context() is compacting
        answers (before the actual _integrate() call) must stop it there —
        not only in the gap between _integrate() and _finalize()."""
        loop = RefinementLoop.__new__(RefinementLoop)
        cancel_event = threading.Event()

        class _CancellingPreprocessor:
            def summarize_results(self, results, *args, **kwargs):
                cancel_event.set()  # simulate cancellation while compacting answers
                return "compact answers"

        loop.preprocessor = _CancellingPreprocessor()

        integrate_calls: list = []
        finalize_calls: list = []
        loop._integrate = lambda *a, **k: integrate_calls.append(1) or "should not be reached"
        loop._finalize = lambda *a, **k: finalize_calls.append(1) or "should not be reached"

        results = {"round1_claude_author": CommandResult(agent="claude", command=[], ok=True, status="ok")}
        integrated, final_answer, refinement_summary = loop._integrate_and_finalize(
            user_request="req",
            context_pack="ctx",
            round_summaries=["## Round 1\nsummary"],
            results=results,
            cancel_event=cancel_event,
            progress=None,
        )

        self.assertEqual(integrate_calls, [], "_integrate must not be called once cancellation is detected")
        self.assertEqual(finalize_calls, [])
        self.assertIn("キャンセル", final_answer)

    def test_cancel_during_round_preprocessing_skips_summarize_chat_call(self) -> None:
        """A cancellation landing while preprocessor.summarize_results() is
        compacting a round's answers (before _summarize_round()'s own prompt
        is even built) must still stop it from calling chair.chat()."""
        loop = RefinementLoop.__new__(RefinementLoop)
        cancel_event = threading.Event()
        chat_calls: list = []

        class _CancellingPreprocessor:
            def summarize_results(self, results, *args, **kwargs):
                cancel_event.set()  # simulate cancellation while compacting answers
                return "compact answers"

        class _FakeChair:
            def chat(self, *args, **kwargs):
                chat_calls.append(1)
                return "should not be reached"

        loop.preprocessor = _CancellingPreprocessor()
        loop.chair = _FakeChair()
        role_round = RoleOrchestrator().build_plan({"claude"}, automation_level=1)[0]
        results = {"round1_claude_author": CommandResult(agent="claude", command=[], ok=True, status="ok")}

        loop._summarize_round("req", "ctx", role_round, results, "", cancel_event)

        self.assertEqual(chat_calls, [])


class IntegrateFinalizeCancelTest(unittest.TestCase):
    def test_cancel_during_integrate_skips_finalize(self) -> None:
        """A cancellation that lands while _integrate()'s HTTP call is in
        flight must stop _finalize() from firing a second, equally pointless
        chair call once _integrate() returns."""
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.preprocessor = ResponsePreprocessor()
        cancel_event = threading.Event()

        finalize_calls = []

        def fake_finalize(*args, **kwargs):
            finalize_calls.append(1)
            return "should not be reached"

        def fake_integrate(*args, **kwargs):
            cancel_event.set()  # simulate the user cancelling while this HTTP call was in flight
            return "integrated summary text"

        loop._finalize = fake_finalize
        loop._integrate = fake_integrate

        integrated, final_answer, refinement_summary = loop._integrate_and_finalize(
            user_request="req",
            context_pack="ctx",
            round_summaries=["## Round 1\nsummary"],
            results={"round1_claude_author": CommandResult(agent="claude", command=[], ok=True, status="ok")},
            cancel_event=cancel_event,
            progress=None,
        )

        self.assertEqual(finalize_calls, [], "finalize must not be called once cancellation is detected")
        self.assertEqual(integrated, "integrated summary text")
        self.assertIn("キャンセル", final_answer)


class _PollingFakeChair:
    """available() replays a fixed sequence of results, one per call — lets a
    test simulate LM Studio coming online partway through a poll loop, as
    long as the cache is invalidated before each re-check."""

    def __init__(self, sequence: list) -> None:
        self.sequence = list(sequence)
        self.calls = 0
        self.invalidate_calls = 0

    def available(self) -> bool:
        index = min(self.calls, len(self.sequence) - 1)
        self.calls += 1
        return self.sequence[index]

    def invalidate_cache(self) -> None:
        self.invalidate_calls += 1


class LMStudioManagerCancelAndCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_popen = subprocess.Popen
        self.addCleanup(setattr, subprocess, "Popen", self._orig_popen)

    def test_ensure_server_running_reprobes_after_invalidating_cache(self) -> None:
        """Without invalidating the cache before each poll, self.chair.available()
        would keep replaying its first (offline) result forever, and this loop
        could never detect a successful startup. _wait_or_cancelled is stubbed
        to skip the real ~1s poll delay, which isn't what this test targets."""
        manager = LMStudioManager.__new__(LMStudioManager)
        manager.chair = _PollingFakeChair([False, True])
        manager.lms_path = "lms"
        manager._wait_or_cancelled = lambda cancel_event, seconds: False
        subprocess.Popen = lambda *a, **k: None

        started, msg = manager.ensure_server_running(timeout_seconds=5)

        self.assertTrue(started)
        self.assertGreaterEqual(manager.chair.invalidate_calls, 1)

    def test_cancelled_before_popen_never_launches_lms(self) -> None:
        manager = LMStudioManager.__new__(LMStudioManager)
        manager.chair = _PollingFakeChair([False])
        manager.lms_path = "lms"
        cancel_event = threading.Event()
        cancel_event.set()

        popen_calls: list = []
        subprocess.Popen = lambda *a, **k: popen_calls.append(1)

        started, msg = manager.ensure_server_running(timeout_seconds=5, cancel_event=cancel_event)

        self.assertFalse(started)
        self.assertEqual(popen_calls, [])

    def test_cancelled_during_poll_stops_without_exhausting_the_timeout(self) -> None:
        """Isolates the poll loop's own cancellation handling from the
        earlier before-Popen check, by making _wait_or_cancelled itself
        report cancellation on its first call."""
        manager = LMStudioManager.__new__(LMStudioManager)
        manager.chair = _PollingFakeChair([False, False, False])
        manager.lms_path = "lms"
        subprocess.Popen = lambda *a, **k: None

        wait_calls: list = []

        def fake_wait_or_cancelled(cancel_event, seconds):
            wait_calls.append(1)
            return True

        manager._wait_or_cancelled = fake_wait_or_cancelled

        started, msg = manager.ensure_server_running(timeout_seconds=30, cancel_event=threading.Event())

        self.assertFalse(started)
        self.assertIn("Cancelled", msg)
        self.assertEqual(len(wait_calls), 1, "must stop after the first cancelled wait, not keep polling")


_TEST_MODEL_CATALOG = {
    "claude": {
        "supports_separate_effort": True,
        "models": [
            {
                "id": "sonnet",
                "label": "Sonnet",
                "billing_status": "subscription_safe",
                "selector_default": True,
                "effort_levels": ["low", "medium", "high"],
            },
            {
                "id": "haiku",
                "label": "Haiku",
                "billing_status": "subscription_safe",
                "effort_levels": [],
            },
            {
                "id": "expensive",
                "label": "Expensive",
                "billing_status": "credits_required",
                "effort_levels": ["low", "medium", "high"],
            },
            {"id": "missing-status", "label": "Missing", "effort_levels": ["low"]},
            {"id": "null-status", "label": "Null", "billing_status": None, "effort_levels": []},
            {"id": "unknown-status", "label": "Unknown", "billing_status": "unknown", "effort_levels": []},
            {"id": "typo-status", "label": "Typo", "billing_status": "subscrption_safe", "effort_levels": []},
        ],
    },
    "gemini": {
        "supports_separate_effort": False,
        "models": [{"id": "gemini-a", "label": "Gemini A", "billing_status": "subscription_safe"}],
    },
}


class _CallCountedCancelEvent:
    """A stand-in cancel_event whose is_set() returns False for the first
    `false_calls` invocations and True afterward — lets a test target one
    *specific* cancellation checkpoint among select()'s two _is_cancelled()
    calls, instead of accidentally satisfying an earlier one."""

    def __init__(self, false_calls: int):
        self.false_calls = false_calls
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > self.false_calls


class _FakeChairForSelector:
    def __init__(self, response: str | None):
        self.response = response
        self.calls: list = []

    def chat(self, system_prompt, user_prompt, max_tokens=1200, timeout_seconds=20):
        self.calls.append((system_prompt, user_prompt))
        return self.response


class AgentModelSelectorTest(unittest.TestCase):
    def test_safe_models_for_includes_only_subscription_safe_entries(self) -> None:
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", _TEST_MODEL_CATALOG)}
        self.assertEqual(safe_ids, {"sonnet", "haiku"})

    def test_safe_models_for_excludes_credits_required(self) -> None:
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", _TEST_MODEL_CATALOG)}
        self.assertNotIn("expensive", safe_ids)

    def test_safe_models_for_excludes_missing_billing_status(self) -> None:
        """Fail-closed: a key that's simply absent must not read as safe."""
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", _TEST_MODEL_CATALOG)}
        self.assertNotIn("missing-status", safe_ids)

    def test_safe_models_for_excludes_null_billing_status(self) -> None:
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", _TEST_MODEL_CATALOG)}
        self.assertNotIn("null-status", safe_ids)

    def test_safe_models_for_excludes_unknown_billing_status(self) -> None:
        """'unknown' means not yet human-reviewed, not 'assumed safe'."""
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", _TEST_MODEL_CATALOG)}
        self.assertNotIn("unknown-status", safe_ids)

    def test_safe_models_for_excludes_a_typo_d_billing_status_value(self) -> None:
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", _TEST_MODEL_CATALOG)}
        self.assertNotIn("typo-status", safe_ids)

    def test_real_catalog_excludes_fable(self) -> None:
        catalog = agent_model_selector.load_catalog()
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", catalog)}
        self.assertNotIn("fable", safe_ids, "credits_required models must never be offered")
        self.assertIn("sonnet", safe_ids)

    def test_real_catalog_excludes_all_unreviewed_gemini_models(self) -> None:
        """Every gemini entry is currently billing_status='unknown' (never
        per-model confirmed against agy models' output); none may be
        selectable until a human reviews it individually."""
        catalog = agent_model_selector.load_catalog()
        safe = agent_model_selector.safe_models_for("gemini", catalog)
        self.assertEqual(safe, [])

    def test_select_applies_a_valid_chair_response(self) -> None:
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\ngemini: model=gemini-a effort=none\n")
        selection = agent_model_selector.select(
            "test request", {"claude", "gemini"}, chair, catalog=_TEST_MODEL_CATALOG
        )
        self.assertEqual(selection.for_agent("claude"), ("sonnet", "high"))
        self.assertEqual(selection.for_agent("gemini"), ("gemini-a", None))

    def test_select_rejects_a_model_not_in_the_allowlist_and_falls_back_to_the_default(self) -> None:
        """An invalid pick is dropped, but the agent still isn't left running
        on 'no override' — it falls back to its curated selector_default."""
        chair = _FakeChairForSelector("claude: model=totally-made-up effort=high\n")
        selection = agent_model_selector.select("req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))

    def test_select_never_applies_a_credit_requiring_model_even_if_the_chair_proposes_it(self) -> None:
        """The chair never even sees 'expensive' as a candidate (filtered out
        of the prompt), but this also checks the parser refuses it outright
        if somehow proposed anyway — defense in depth. Rejecting it still
        falls back to the curated default, not to 'no override'."""
        chair = _FakeChairForSelector("claude: model=expensive effort=high\n")
        selection = agent_model_selector.select("req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))

    def test_select_ignores_an_effort_not_in_the_allowed_list(self) -> None:
        chair = _FakeChairForSelector("claude: model=sonnet effort=ultra\n")
        selection = agent_model_selector.select("req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))

    def test_select_falls_back_to_the_curated_default_when_chair_returns_nothing(self) -> None:
        """An unusable chair reply must never leave an agent on 'no
        override' — it falls back to the catalog's selector_default."""
        chair = _FakeChairForSelector(None)
        selection = agent_model_selector.select("req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))
        self.assertEqual(len(chair.calls), 1)

    def test_select_does_not_fall_back_an_agent_with_no_selector_default(self) -> None:
        """gemini's only safe candidate in the test catalog isn't flagged
        selector_default (mirroring the real catalog, where every gemini
        entry is still unreviewed) — it must stay unselected, not silently
        pick some arbitrary candidate."""
        chair = _FakeChairForSelector(None)
        selection = agent_model_selector.select("req", {"gemini"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("gemini"), (None, None))

    def test_select_skips_the_chair_entirely_when_already_cancelled(self) -> None:
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\n")
        cancel_event = threading.Event()
        cancel_event.set()
        selection = agent_model_selector.select(
            "req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG, cancel_event=cancel_event
        )
        self.assertEqual(selection.choices, {})
        self.assertEqual(chair.calls, [], "chair.chat() must not be called once cancelled")

    def test_select_returns_empty_when_no_agent_has_any_safe_candidate(self) -> None:
        chair = _FakeChairForSelector("codex: model=x effort=y\n")
        selection = agent_model_selector.select("req", {"codex"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.choices, {})
        self.assertEqual(chair.calls, [], "no point asking the chair with zero candidates")

    def test_select_never_applies_an_effort_to_a_model_with_no_effort_levels(self) -> None:
        """haiku's effort_levels is [] (it doesn't support effort at all) —
        even a chair reply proposing one must be dropped, not merely an
        agent-level 'no separate effort' check."""
        chair = _FakeChairForSelector("claude: model=haiku effort=high\n")
        selection = agent_model_selector.select("req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("claude"), ("haiku", None))

    def test_select_checks_cancellation_again_right_before_the_chair_call(self) -> None:
        """Cancellation landing strictly between candidate-gathering/prompt
        construction and the chair.chat() call must be caught by THAT check
        specifically — not merely by the earlier one (which would make this
        pass vacuously)."""
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\n")
        # First _is_cancelled() call (before candidate gathering) sees False;
        # second call (right before chair.chat()) sees True.
        cancel_event = _CallCountedCancelEvent(false_calls=1)
        selection = agent_model_selector.select(
            "req", {"claude"}, chair, catalog=_TEST_MODEL_CATALOG, cancel_event=cancel_event
        )
        self.assertEqual(selection.choices, {})
        self.assertEqual(chair.calls, [], "chair.chat() must not be called once cancelled")

    def test_agent_selection_for_agent_defaults_to_none_none(self) -> None:
        selection = agent_model_selector.AgentSelection.empty()
        self.assertEqual(selection.for_agent("claude"), (None, None))

    def test_default_selection_uses_the_catalog_default_with_no_chair_involved(self) -> None:
        """Mirrors what preflight needs: a safe --model with zero chair
        consultation (the user's request/LM Studio availability aren't even
        known yet at that point)."""
        selection = agent_model_selector.default_selection({"claude"}, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))

    def test_default_selection_omits_an_agent_with_no_selector_default(self) -> None:
        selection = agent_model_selector.default_selection({"gemini"}, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.for_agent("gemini"), (None, None))

    def test_default_selection_is_empty_for_an_agent_with_no_safe_candidates(self) -> None:
        selection = agent_model_selector.default_selection({"codex"}, catalog=_TEST_MODEL_CATALOG)
        self.assertEqual(selection.choices, {})


class _RunContextStub:
    """Duck-typed stand-in for RunContext: only the attributes
    resolve_chair_auto_agents()/determine_agent_selection() actually read."""

    def __init__(self, selected_models=None, selected_efforts=None, effort=None):
        self.selected_models = selected_models or {}
        self.selected_efforts = selected_efforts or {}
        self.effort = effort


class ChairAutoSelectSentinelTest(unittest.TestCase):
    """CHAIR_AUTO_SELECT ("議長AIにお任せ") must never be treated as a real
    catalog id anywhere in the pipeline it flows through."""

    def test_sentinel_matches_no_id_in_the_test_catalog(self) -> None:
        for agent in ("claude", "gemini"):
            ids = {m["id"] for m in agent_model_selector.all_models_for(agent, _TEST_MODEL_CATALOG)}
            self.assertNotIn(agent_model_selector.CHAIR_AUTO_SELECT, ids)

    def test_sentinel_matches_no_id_in_the_real_catalog(self) -> None:
        catalog = agent_model_selector.load_catalog()
        for agent in ("claude", "gemini", "codex"):
            ids = {m["id"] for m in agent_model_selector.all_models_for(agent, catalog)}
            self.assertNotIn(agent_model_selector.CHAIR_AUTO_SELECT, ids)

    def test_an_unresolved_sentinel_is_substituted_with_the_confirmed_default(self) -> None:
        """Defense in depth: validated_model_and_effort() is the last-line
        check build_commands() applies to an explicit pick. If the sentinel
        ever reached it unresolved, it must not flow through to --model —
        it must not match any safe id, so it falls through to the same
        confirmed-default substitution as any other invalid model_id."""
        model_id, _effort = agent_model_selector.validated_model_and_effort(
            "claude", agent_model_selector.CHAIR_AUTO_SELECT, None, catalog=_TEST_MODEL_CATALOG
        )
        self.assertEqual(model_id, "sonnet")
        self.assertNotEqual(model_id, agent_model_selector.CHAIR_AUTO_SELECT)


class ResolveChairAutoAgentsTest(unittest.TestCase):
    def test_run_context_none_means_the_whole_set_goes_to_the_chair(self) -> None:
        """Preserves select()'s original whole-set behavior for callers
        outside the GUI flow (e.g. direct test calls)."""
        result = agent_model_selector.resolve_chair_auto_agents(None, {"claude", "gemini"})
        self.assertEqual(result, {"claude", "gemini"})

    def test_no_agent_marked_auto_returns_empty(self) -> None:
        run_context = _RunContextStub(selected_models={"claude": "sonnet"})
        result = agent_model_selector.resolve_chair_auto_agents(run_context, {"claude", "gemini"})
        self.assertEqual(result, set())

    def test_only_the_marked_agents_are_returned(self) -> None:
        run_context = _RunContextStub(
            selected_models={
                "claude": "sonnet",
                "codex": agent_model_selector.CHAIR_AUTO_SELECT,
            }
        )
        result = agent_model_selector.resolve_chair_auto_agents(run_context, {"claude", "codex", "gemini"})
        self.assertEqual(result, {"codex"})

    def test_result_is_intersected_with_available_agents(self) -> None:
        """An agent marked auto-select that isn't actually available this run
        (e.g. failed preflight) must not trigger a wasted chair round-trip."""
        run_context = _RunContextStub(selected_models={"codex": agent_model_selector.CHAIR_AUTO_SELECT})
        result = agent_model_selector.resolve_chair_auto_agents(run_context, {"claude"})
        self.assertEqual(result, set())


class AgentSelectionRenderTest(unittest.TestCase):
    def test_construction_without_chair_auto_agents_still_works(self) -> None:
        """Protects every pre-existing AgentSelection(choices={...}) call
        site across both test files, which don't pass the new field."""
        selection = agent_model_selector.AgentSelection(choices={"claude": ("sonnet", "high")})
        self.assertEqual(selection.chair_auto_agents, frozenset())
        self.assertIn("- claude: model=sonnet / effort=high", selection.render())

    def test_a_chair_auto_agent_is_annotated_in_the_rendered_plan(self) -> None:
        selection = agent_model_selector.AgentSelection(
            choices={"claude": ("sonnet", "high"), "codex": ("gpt-6", None)},
            chair_auto_agents=frozenset({"codex"}),
        )
        rendered = selection.render()
        claude_line = next(line for line in rendered.splitlines() if line.startswith("- claude"))
        codex_line = next(line for line in rendered.splitlines() if line.startswith("- codex"))
        self.assertNotIn("議長AI自動選択", claude_line)
        self.assertIn("議長AI自動選択", codex_line)


class DetermineAgentSelectionTest(unittest.TestCase):
    """RefinementLoop.determine_agent_selection() previously had
    `has_user_selection = run_context is not None or bool(...)`: the `or`
    made this always True whenever any run_context was passed, which is every
    real GUI call — the chair-auto branch was unreachable dead code. These
    tests exercise the fixed, per-agent-mixed resolution directly."""

    def _loop(self, chair: "_FakeChairForSelector") -> RefinementLoop:
        loop = RefinementLoop.__new__(RefinementLoop)
        loop.chair = chair
        return loop

    def test_run_context_none_still_asks_the_chair_for_the_whole_set(self) -> None:
        """Regression-protects the one path that already worked before the
        fix, just unreachable from the GUI: a caller with no run_context at
        all still gets full chair-driven selection."""
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\n")
        loop = self._loop(chair)
        with patch.object(agent_model_selector, "load_catalog", return_value=_TEST_MODEL_CATALOG):
            selection = loop.determine_agent_selection("req", {"claude"}, run_context=None, cancel_event=None)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", "high"))
        self.assertEqual(len(chair.calls), 1)

    def test_pure_explicit_selection_never_calls_the_chair(self) -> None:
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\n")
        loop = self._loop(chair)
        run_context = _RunContextStub(selected_models={"claude": "opus"}, selected_efforts={"claude": "low"})
        selection = loop.determine_agent_selection("req", {"claude"}, run_context=run_context)
        self.assertEqual(selection.for_agent("claude"), ("opus", "low"))
        self.assertEqual(chair.calls, [])

    def test_pure_cli_default_never_calls_the_chair(self) -> None:
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\n")
        loop = self._loop(chair)
        run_context = _RunContextStub(selected_models={})
        selection = loop.determine_agent_selection("req", {"claude"}, run_context=run_context)
        self.assertEqual(selection.for_agent("claude"), (None, None))
        self.assertEqual(chair.calls, [])

    def test_a_three_agent_run_resolves_explicit_auto_and_default_independently(self) -> None:
        """The scenario the fix exists for: Claude explicitly picked, Codex
        left to the chair, Antigravity on CLI-default, all in one run."""
        chair = _FakeChairForSelector("gemini: model=gemini-a effort=none\n")
        loop = self._loop(chair)
        run_context = _RunContextStub(
            selected_models={
                "claude": "opus",
                "gemini": agent_model_selector.CHAIR_AUTO_SELECT,
                # codex absent entirely -> CLI-default
            },
            selected_efforts={"claude": "high"},
        )
        with patch.object(agent_model_selector, "load_catalog", return_value=_TEST_MODEL_CATALOG):
            selection = loop.determine_agent_selection(
                "req", {"claude", "gemini", "codex"}, run_context=run_context
            )
        self.assertEqual(selection.for_agent("claude"), ("opus", "high"))
        self.assertEqual(selection.for_agent("gemini"), ("gemini-a", None))
        self.assertEqual(selection.for_agent("codex"), (None, None))
        self.assertEqual(selection.chair_auto_agents, frozenset({"gemini"}))
        self.assertEqual(len(chair.calls), 1)

    def test_two_chair_auto_agents_trigger_exactly_one_chair_call(self) -> None:
        """Batched, not per-agent: chair calls in this app measure 30-90+
        seconds, so resolving N auto agents must cost one call, not N."""
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\ngemini: model=gemini-a effort=none\n")
        loop = self._loop(chair)
        run_context = _RunContextStub(
            selected_models={
                "claude": agent_model_selector.CHAIR_AUTO_SELECT,
                "gemini": agent_model_selector.CHAIR_AUTO_SELECT,
            }
        )
        with patch.object(agent_model_selector, "load_catalog", return_value=_TEST_MODEL_CATALOG):
            selection = loop.determine_agent_selection("req", {"claude", "gemini"}, run_context=run_context)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", "high"))
        self.assertEqual(selection.for_agent("gemini"), ("gemini-a", None))
        self.assertEqual(len(chair.calls), 1)

    def test_a_chair_auto_agent_never_resolves_to_an_unsafe_model(self) -> None:
        """Even if the chair proposes a credit-requiring model for the
        auto-select agent, the safe-by-construction candidate pool
        (safe_models_for()) means it was never offered as a candidate —
        this exercises that guarantee through determine_agent_selection(),
        not just select() directly."""
        chair = _FakeChairForSelector("claude: model=expensive effort=high\n")
        loop = self._loop(chair)
        run_context = _RunContextStub(selected_models={"claude": agent_model_selector.CHAIR_AUTO_SELECT})
        with patch.object(agent_model_selector, "load_catalog", return_value=_TEST_MODEL_CATALOG):
            selection = loop.determine_agent_selection("req", {"claude"}, run_context=run_context)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))

    def test_a_chair_auto_agent_falls_back_to_the_confirmed_default_when_the_chair_is_unavailable(self) -> None:
        chair = _FakeChairForSelector(None)
        loop = self._loop(chair)
        run_context = _RunContextStub(selected_models={"claude": agent_model_selector.CHAIR_AUTO_SELECT})
        with patch.object(agent_model_selector, "load_catalog", return_value=_TEST_MODEL_CATALOG):
            selection = loop.determine_agent_selection("req", {"claude"}, run_context=run_context)
        self.assertEqual(selection.for_agent("claude"), ("sonnet", None))

    def test_no_chair_auto_agents_means_zero_chair_calls_even_with_a_run_context(self) -> None:
        """The common case (all explicit or all CLI-default) must not pay any
        chair latency at all."""
        chair = _FakeChairForSelector("claude: model=sonnet effort=high\n")
        loop = self._loop(chair)
        run_context = _RunContextStub(selected_models={"claude": "sonnet"}, selected_efforts={"claude": "high"})
        selection = loop.determine_agent_selection("req", {"claude"}, run_context=run_context)
        self.assertEqual(chair.calls, [])
        self.assertEqual(selection.chair_auto_agents, frozenset())


class ConfirmedDefaultModelValidationTest(unittest.TestCase):
    """A catalog is a human-edited file — these guard against authoring
    mistakes that would otherwise silently produce an unsafe or broken
    --model rather than the intended safe default."""

    def test_string_false_is_not_treated_as_a_default(self) -> None:
        catalog = {
            "claude": {
                "models": [
                    {"id": "sonnet", "billing_status": "subscription_safe", "selector_default": "false"}
                ]
            }
        }
        self.assertIsNone(agent_model_selector.confirmed_default_for("claude", catalog))
        self.assertFalse(agent_model_selector.has_confirmed_default("claude", catalog))

    def test_empty_id_is_not_treated_as_a_default(self) -> None:
        catalog = {
            "claude": {
                "models": [{"id": "", "billing_status": "subscription_safe", "selector_default": True}]
            }
        }
        self.assertIsNone(agent_model_selector.confirmed_default_for("claude", catalog))

    def test_two_defaults_resolve_to_none_not_the_first_one(self) -> None:
        catalog = {
            "claude": {
                "models": [
                    {"id": "sonnet", "billing_status": "subscription_safe", "selector_default": True},
                    {"id": "opus", "billing_status": "subscription_safe", "selector_default": True},
                ]
            }
        }
        self.assertIsNone(agent_model_selector.confirmed_default_for("claude", catalog))
        self.assertFalse(agent_model_selector.has_confirmed_default("claude", catalog))

    def test_whitespace_only_id_is_not_treated_as_a_default(self) -> None:
        catalog = {
            "claude": {
                "models": [{"id": "   ", "billing_status": "subscription_safe", "selector_default": True}]
            }
        }
        self.assertIsNone(agent_model_selector.confirmed_default_for("claude", catalog))

    def test_non_dict_entries_in_models_are_skipped_not_raised_on(self) -> None:
        """A hand-edited catalog with junk in the list must degrade to
        'nothing selectable', never crash a run mid-flight."""
        catalog = {
            "claude": {
                "models": [
                    "not-a-dict",
                    None,
                    42,
                    {"id": "sonnet", "billing_status": "subscription_safe", "selector_default": True},
                ]
            }
        }
        safe_ids = {m["id"] for m in agent_model_selector.safe_models_for("claude", catalog)}
        self.assertEqual(safe_ids, {"sonnet"})
        default_model = agent_model_selector.confirmed_default_for("claude", catalog)
        self.assertIsNotNone(default_model)
        self.assertEqual(default_model["id"], "sonnet")

    def test_model_missing_its_id_is_excluded_at_the_candidate_stage(self) -> None:
        """Regression: a subscription_safe entry with no `id` key used to pass
        safe_models_for() and then raise KeyError downstream where callers
        index m["id"]. It must be excluded as a candidate instead, so a
        malformed catalog degrades safely rather than crashing a run."""
        catalog = {"claude": {"models": [{"billing_status": "subscription_safe", "selector_default": True}]}}
        self.assertEqual(agent_model_selector.safe_models_for("claude", catalog), [])
        self.assertIsNone(agent_model_selector.confirmed_default_for("claude", catalog))

        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = cli_execution_policy.active()
        self.assertEqual(
            adapters._validated_model_and_effort("claude", "sonnet", None, catalog),
            (None, None),
        )

    def test_malformed_agent_entry_degrades_to_empty(self) -> None:
        for broken in ({"claude": "not-a-dict"}, {"claude": {"models": "not-a-list"}}, {"claude": None}):
            with self.subTest(catalog=broken):
                self.assertEqual(agent_model_selector.safe_models_for("claude", broken), [])
                self.assertFalse(agent_model_selector.has_confirmed_default("claude", broken))

    def test_exactly_one_valid_default_is_confirmed(self) -> None:
        catalog = {
            "claude": {
                "models": [
                    {"id": "sonnet", "billing_status": "subscription_safe", "selector_default": True},
                    {"id": "opus", "billing_status": "subscription_safe"},
                ]
            }
        }
        default_model = agent_model_selector.confirmed_default_for("claude", catalog)
        self.assertIsNotNone(default_model)
        self.assertEqual(default_model["id"], "sonnet")
        self.assertTrue(agent_model_selector.has_confirmed_default("claude", catalog))


class RefreshGeminiCatalogTest(unittest.TestCase):
    def test_refresh_keeps_reviewed_billing_status_and_marks_new_ids_unknown(self) -> None:
        """A model already human-reviewed as subscription_safe must keep that
        status across a refresh (only its label may change); a model that
        `agy models` has never shown before must come in as 'unknown', never
        inheriting safety from an unrelated entry or an unearned default."""
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            catalog_path = Path(tmp_dir.name) / "agent_models.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "gemini": {
                            "supports_separate_effort": False,
                            "models": [
                                {
                                    "id": "gemini-a",
                                    "label": "Gemini A (old label)",
                                    "billing_status": "subscription_safe",
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            class _FakeCompleted:
                returncode = 0
                stdout = "gemini-a\tGemini A (new label)\ngemini-b\tGemini B\n"

            original_which = agent_model_selector.shutil.which
            original_run = agent_model_selector.subprocess.run
            agent_model_selector.shutil.which = lambda _name: "/usr/bin/agy"
            agent_model_selector.subprocess.run = lambda *_a, **_k: _FakeCompleted()
            try:
                ok = agent_model_selector.refresh_gemini_catalog_from_agy(path=catalog_path)
            finally:
                agent_model_selector.shutil.which = original_which
                agent_model_selector.subprocess.run = original_run

            self.assertTrue(ok)
            updated = json.loads(catalog_path.read_text(encoding="utf-8"))
            models_by_id = {m["id"]: m for m in updated["gemini"]["models"]}
            self.assertEqual(models_by_id["gemini-a"]["billing_status"], "subscription_safe")
            self.assertEqual(models_by_id["gemini-a"]["label"], "Gemini A (new label)")
            self.assertEqual(models_by_id["gemini-b"]["billing_status"], "unknown")
        finally:
            tmp_dir.cleanup()

    def test_refresh_refuses_to_touch_a_corrupt_catalog_file(self) -> None:
        """load_catalog() masks a corrupt file as an empty dict — refreshing
        from that would silently overwrite the file with a gemini-only
        catalog, destroying every other agent's (e.g. claude's) human-
        reviewed billing_status. A corrupt-but-present file must be left
        completely untouched instead."""
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            catalog_path = Path(tmp_dir.name) / "agent_models.json"
            corrupt_payload = "{not valid json"
            catalog_path.write_text(corrupt_payload, encoding="utf-8")

            class _FakeCompleted:
                returncode = 0
                stdout = "gemini-a\tGemini A\n"

            original_which = agent_model_selector.shutil.which
            original_run = agent_model_selector.subprocess.run
            agent_model_selector.shutil.which = lambda _name: "/usr/bin/agy"
            agent_model_selector.subprocess.run = lambda *_a, **_k: _FakeCompleted()
            try:
                ok = agent_model_selector.refresh_gemini_catalog_from_agy(path=catalog_path)
            finally:
                agent_model_selector.shutil.which = original_which
                agent_model_selector.subprocess.run = original_run

            self.assertFalse(ok)
            self.assertEqual(catalog_path.read_text(encoding="utf-8"), corrupt_payload)
        finally:
            tmp_dir.cleanup()

    def test_refresh_leaves_original_file_untouched_and_no_tmp_file_behind_on_replace_failure(self) -> None:
        """If the final os.replace() step fails (disk full, permissions,
        etc.), the original catalog must be unmodified and no stray .tmp
        file should be left in the directory."""
        tmp_dir = tempfile.TemporaryDirectory()
        try:
            catalog_path = Path(tmp_dir.name) / "agent_models.json"
            original_payload = json.dumps({"gemini": {"models": [{"id": "gemini-a", "label": "Old"}]}})
            catalog_path.write_text(original_payload, encoding="utf-8")

            class _FakeCompleted:
                returncode = 0
                stdout = "gemini-a\tGemini A (new label)\n"

            original_which = agent_model_selector.shutil.which
            original_run = agent_model_selector.subprocess.run
            original_replace = agent_model_selector.os.replace
            agent_model_selector.shutil.which = lambda _name: "/usr/bin/agy"
            agent_model_selector.subprocess.run = lambda *_a, **_k: _FakeCompleted()

            def _failing_replace(_src, _dst):
                raise OSError("simulated disk failure")

            agent_model_selector.os.replace = _failing_replace
            try:
                ok = agent_model_selector.refresh_gemini_catalog_from_agy(path=catalog_path)
            finally:
                agent_model_selector.shutil.which = original_which
                agent_model_selector.subprocess.run = original_run
                agent_model_selector.os.replace = original_replace

            self.assertFalse(ok)
            self.assertEqual(catalog_path.read_text(encoding="utf-8"), original_payload)
            leftover_tmp_files = list(Path(tmp_dir.name).glob("*.tmp"))
            self.assertEqual(leftover_tmp_files, [], f"stray tmp files left behind: {leftover_tmp_files}")
        finally:
            tmp_dir.cleanup()


class CliAdaptersAgentSelectionTest(unittest.TestCase):
    def test_claude_command_includes_model_and_effort_when_selected(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        selection = agent_model_selector.AgentSelection(choices={"claude": ("opus", "high")})
        command = adapters._base_command("claude", "hello", *selection.for_agent("claude"))
        self.assertIn("opus", command)
        self.assertIn("high", command)
        self.assertIn("--model", command)
        self.assertIn("--effort", command)

    def test_gemini_command_includes_model_but_never_an_effort_flag(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        selection = agent_model_selector.AgentSelection(choices={"gemini": ("gemini-3.6-flash-high", None)})
        command = adapters._base_command("gemini", "hello", *selection.for_agent("gemini"))
        self.assertIn("--model", command)
        self.assertIn("gemini-3.6-flash-high", command)
        self.assertNotIn("--effort", command)

    def test_codex_command_never_gets_model_or_effort_flags(self) -> None:
        """No verified catalog exists for codex yet; a selection for it must
        be silently ignored, not passed through to the CLI."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        selection = agent_model_selector.AgentSelection(choices={"codex": ("gpt-5", "high")})
        command = adapters._base_command("codex", "hello", *selection.for_agent("codex"))
        self.assertNotIn("--model", command)
        self.assertNotIn("high", command)

    def test_build_commands_wires_agent_selection_through(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        selection = agent_model_selector.AgentSelection(choices={"claude": ("opus", "high")})
        commands, _warnings = adapters.build_commands({"claude": "hi"}, automation_level=2, agent_selection=selection)
        self.assertIn("opus", commands["claude"])

    def test_no_selection_falls_back_to_the_catalog_confirmed_default(self) -> None:
        """build_commands() is the last line of defense: even with no
        agent_selection at all, claude must never run with no --model —
        it gets the catalog's confirmed-default model (sonnet today)."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        commands, _warnings = adapters.build_commands({"claude": "hi"}, automation_level=2)
        self.assertIn("--model", commands["claude"])
        self.assertIn("sonnet", commands["claude"])

    def test_no_selection_and_no_confirmed_default_means_no_model_override(self) -> None:
        """If the catalog genuinely has no confirmed default for an agent
        (e.g. codex, which has no catalog entry at all), build_commands()
        has nothing safe to fall back to and leaves --model unset — the
        agent is expected to be gated out via command_exists() elsewhere."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        commands, _warnings = adapters.build_commands({"codex": "hi"}, automation_level=2)
        self.assertNotIn("--model", commands["codex"])

    def test_build_commands_warns_when_agy_present_but_no_gemini_model_is_confirmed_safe(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = "/opt/homebrew/bin/agy"
        adapters.legacy_gemini_path = None
        _commands, warnings = adapters.build_commands({"gemini": "hi"}, automation_level=2)
        self.assertTrue(
            any("Use AI Credits" in w for w in warnings),
            f"expected a warning about the unverifiable billing setting, got: {warnings}",
        )

    def _bare_adapters(self) -> CliAdapters:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        adapters.prefer_rtk = False
        adapters.rtk_path = None
        adapters.agy_path = None
        adapters.legacy_gemini_path = None
        return adapters

    def test_build_commands_rejects_an_explicit_credit_requiring_model(self) -> None:
        """An AgentSelection built outside agent_model_selector's own
        chair-response validation must still not be able to produce
        `--model fable`; build_commands() re-checks it and substitutes the
        catalog's confirmed default."""
        selection = agent_model_selector.AgentSelection(choices={"claude": ("fable", None)})
        commands, _warnings = self._bare_adapters().build_commands(
            {"claude": "hi"}, automation_level=2, agent_selection=selection
        )
        self.assertNotIn("fable", commands["claude"])
        self.assertIn("sonnet", commands["claude"])

    def test_build_commands_rejects_an_unknown_explicit_model(self) -> None:
        selection = agent_model_selector.AgentSelection(choices={"claude": ("totally-made-up", None)})
        commands, _warnings = self._bare_adapters().build_commands(
            {"claude": "hi"}, automation_level=2, agent_selection=selection
        )
        self.assertNotIn("totally-made-up", commands["claude"])
        self.assertIn("sonnet", commands["claude"])

    def test_build_commands_drops_an_effort_the_chosen_model_does_not_support(self) -> None:
        """haiku has effort_levels: [] in the real catalog."""
        selection = agent_model_selector.AgentSelection(choices={"claude": ("haiku", "max")})
        commands, _warnings = self._bare_adapters().build_commands(
            {"claude": "hi"}, automation_level=2, agent_selection=selection
        )
        self.assertIn("haiku", commands["claude"])
        self.assertNotIn("--effort", commands["claude"])

    def test_build_commands_keeps_a_valid_explicit_model_and_effort(self) -> None:
        selection = agent_model_selector.AgentSelection(choices={"claude": ("opus", "high")})
        commands, _warnings = self._bare_adapters().build_commands(
            {"claude": "hi"}, automation_level=2, agent_selection=selection
        )
        self.assertIn("opus", commands["claude"])
        self.assertIn("--effort", commands["claude"])
        self.assertIn("high", commands["claude"])


class CodexAuthAndProviderEnforcementTest(unittest.TestCase):
    """`codex exec` otherwise inherits ~/.codex/config.toml (which can define
    a custom model_provider / bearer token) and any cached API-key login,
    neither of which stripping OPENAI_API_KEY from the child env prevents."""

    def test_codex_command_ignores_user_config(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("codex", "hello")
        self.assertIn("--ignore-user-config", command)

    def test_codex_command_forces_chatgpt_login_method(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("codex", "hello")
        self.assertIn("-c", command)
        self.assertIn('forced_login_method="chatgpt"', command)

    def test_codex_command_pins_the_model_provider(self) -> None:
        """--ignore-user-config only covers the *user* config; a system
        config could still point the provider elsewhere."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("codex", "hello")
        self.assertIn('model_provider="openai"', command)

    def test_codex_command_pins_the_inference_and_login_endpoints(self) -> None:
        """openai_base_url applies to the built-in `openai` provider too, so
        pinning the provider id alone still leaves the endpoint swappable
        from a system config."""
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("codex", "hello")
        self.assertIn('openai_base_url="https://chatgpt.com/backend-api/codex"', command)
        self.assertIn('chatgpt_base_url="https://chatgpt.com/backend-api/"', command)


class AnthropicProfileIsolationTest(unittest.TestCase):
    """Anthropic profile / federation credentials live under
    ANTHROPIC_CONFIG_DIR and outrank the /login subscription credential.
    Removing the variable is not enough — that falls back to the default
    ~/.config/anthropic — so it's pointed at a throwaway directory created
    per run. A shared fixed path wouldn't do: another process (or an earlier
    run) could have planted an active_config or a profile in it."""

    def _env_and_dir(self) -> tuple[dict, str]:
        runner = ProcessRunner(policy=_STRICT)
        child_env, isolated_dir = runner._child_env()
        self.addCleanup(runner._discard_isolated_config_dir, isolated_dir)
        return child_env, isolated_dir

    def test_child_env_points_at_the_throwaway_dir(self) -> None:
        """Compares only against the returned path; never asserts on the env
        dict itself, which would dump the real environment (possibly
        containing unrelated secrets) into a failure message."""
        child_env, isolated_dir = self._env_and_dir()
        self.assertEqual(child_env.get("ANTHROPIC_CONFIG_DIR"), isolated_dir)

    def test_isolated_dir_is_not_the_user_default(self) -> None:
        _child_env, isolated_dir = self._env_and_dir()
        self.assertNotEqual(Path(isolated_dir), Path.home() / ".config" / "anthropic")

    def test_isolated_dir_is_private_empty_and_not_a_symlink(self) -> None:
        """The four properties the review asked to be guaranteed. A fresh
        mkdtemp() gives all of them by construction."""
        _child_env, isolated_dir = self._env_and_dir()
        path = Path(isolated_dir)
        self.assertTrue(path.is_dir())
        self.assertFalse(path.is_symlink())
        self.assertEqual(list(path.iterdir()), [])
        self.assertEqual(oct(path.stat().st_mode)[-3:], "700")
        self.assertEqual(path.stat().st_uid, os.getuid())

    def test_each_call_gets_its_own_directory(self) -> None:
        _e1, first = self._env_and_dir()
        _e2, second = self._env_and_dir()
        self.assertNotEqual(first, second)

    def test_anthropic_config_dir_is_overridden_not_merely_stripped(self) -> None:
        """If it were only in BLOCKED_CHILD_ENV_VARS, the child would fall
        back to the user's default profile directory."""
        user_default = str(Path.home() / ".config" / "anthropic")
        original = os.environ.get("ANTHROPIC_CONFIG_DIR")
        os.environ["ANTHROPIC_CONFIG_DIR"] = user_default
        try:
            child_env, _isolated_dir = self._env_and_dir()
        finally:
            if original is None:
                os.environ.pop("ANTHROPIC_CONFIG_DIR", None)
            else:
                os.environ["ANTHROPIC_CONFIG_DIR"] = original
        self.assertNotEqual(child_env.get("ANTHROPIC_CONFIG_DIR"), user_default)


class IsolatedConfigDirCleanupTest(unittest.IsolatedAsyncioTestCase):
    """The finally block covers normal exit, exceptions, timeout and
    cancellation. It cannot cover SIGKILL or an app crash — those leave the
    directory behind, which is why it lives under the OS temp dir.

    These exercise the isolation machinery itself, so they run with the claude
    slot forced on; the slot being off today is covered elsewhere."""

    def setUp(self) -> None:
        enable_all_slots(self)
        stub_claude_auth(self)
        stub_claude_token(self)
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = True
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)

    def _leaked_dirs(self) -> set:
        return set(Path(tempfile.gettempdir()).glob("ai-brainstorm-anthropic-isolation-*"))

    async def test_directory_is_discarded_after_a_successful_run(self) -> None:
        before = self._leaked_dirs()
        await ProcessRunner().run(agent="claude", command=["/bin/echo", "OK"], cwd=Path("."), timeout_seconds=10)
        self.assertEqual(self._leaked_dirs() - before, set())

    async def test_directory_is_discarded_even_when_the_command_is_missing(self) -> None:
        before = self._leaked_dirs()
        result = await ProcessRunner().run(
            agent="gemini", command=["/nonexistent-command-for-tests"], cwd=Path("."), timeout_seconds=5
        )
        self.assertEqual(result.status, "command_missing")
        self.assertEqual(self._leaked_dirs() - before, set())

    async def test_directory_is_discarded_on_timeout(self) -> None:
        before = self._leaked_dirs()
        result = await ProcessRunner().run(
            agent="gemini", command=["/bin/sleep", "5"], cwd=Path("."), timeout_seconds=1
        )
        self.assertEqual(result.status, "timeout")
        self.assertEqual(self._leaked_dirs() - before, set())

    async def test_directory_is_discarded_on_cancellation(self) -> None:
        before = self._leaked_dirs()
        cancel_event = threading.Event()
        cancel_event.set()
        result = await ProcessRunner().run(
            agent="gemini",
            command=["/bin/sleep", "5"],
            cwd=Path("."),
            timeout_seconds=10,
            cancel_event=cancel_event,
        )
        self.assertEqual(result.status, "cancelled")
        self.assertEqual(self._leaked_dirs() - before, set())

    async def test_directory_is_discarded_when_launching_raises_unexpectedly(self) -> None:
        before = self._leaked_dirs()
        runner = ProcessRunner()
        original = asyncio.create_subprocess_exec

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("simulated launch failure")

        asyncio.create_subprocess_exec = _boom
        try:
            result = await runner.run(
                agent="gemini", command=["/bin/echo", "OK"], cwd=Path("."), timeout_seconds=5
            )
        finally:
            asyncio.create_subprocess_exec = original
        self.assertEqual(result.status, "error")
        self.assertEqual(self._leaked_dirs() - before, set())


class ProfileIsolationFailClosedTest(unittest.IsolatedAsyncioTestCase):
    """If the private isolation directory can't be created, an agent that
    needs it must not run at all — falling back to a predictable shared path
    would reintroduce exactly the planted-profile risk it exists to remove.

    Runs with the claude slot forced on so the isolation gate is what's under
    test here, not the slot gate that would otherwise short-circuit first."""

    def setUp(self) -> None:
        enable_all_slots(self)
        stub_claude_auth(self)
        stub_claude_token(self)
        original_flag = config.CLAUDE_SLOT_ENABLED
        config.CLAUDE_SLOT_ENABLED = True
        self.addCleanup(setattr, config, "CLAUDE_SLOT_ENABLED", original_flag)

    def _break_mkdtemp(self) -> None:
        original = process_runner_module.tempfile.mkdtemp

        def _fail(**_kwargs):
            raise OSError("simulated mkdtemp failure")

        process_runner_module.tempfile.mkdtemp = _fail
        self.addCleanup(setattr, process_runner_module.tempfile, "mkdtemp", original)

    async def test_claude_is_blocked_when_isolation_cannot_be_established(self) -> None:
        self._break_mkdtemp()
        result = await ProcessRunner(policy=_STRICT).run(
            agent="claude", command=["/bin/echo", "SHOULD-NOT-RUN"], cwd=Path("."), timeout_seconds=5
        )
        self.assertEqual(result.status, "safety_blocked")
        self.assertFalse(result.ok)
        self.assertEqual(result.stdout, "")

    async def test_an_agent_not_needing_isolation_is_unaffected(self) -> None:
        """Codex doesn't read ANTHROPIC_CONFIG_DIR, so a failure to create it
        must not take codex down with claude."""
        self._break_mkdtemp()
        result = await ProcessRunner(policy=_STRICT).run(
            agent="gemini", command=["/bin/echo", "OK"], cwd=Path("."), timeout_seconds=5
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.stdout.strip(), "OK")

    def test_no_fallback_path_is_ever_placed_in_the_child_env(self) -> None:
        self._break_mkdtemp()
        child_env, isolated_dir = ProcessRunner(policy=_STRICT)._child_env("claude")
        self.assertIsNone(isolated_dir)
        self.assertNotIn("ANTHROPIC_CONFIG_DIR", child_env)

    def test_agents_not_needing_isolation_get_no_isolation_dir(self) -> None:
        child_env, isolated_dir = ProcessRunner(policy=_STRICT)._child_env("codex")
        self.addCleanup(ProcessRunner(policy=_STRICT)._discard_isolated_config_dir, isolated_dir)
        self.assertIsNone(isolated_dir)
        self.assertNotIn("ANTHROPIC_CONFIG_DIR", child_env)


class ClaudeSettingSourceIsolationTest(unittest.TestCase):
    """Stripping the parent env can't reach an `apiKeyHelper` in the user's
    settings.json or an `env` block in the project's .claude/settings.json —
    both of which can reintroduce a billed credential. These flags stop
    claude from reading those files at all and pin the login method."""

    def test_claude_command_loads_no_setting_sources(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("claude", "hello")
        self.assertIn("--setting-sources", command)
        self.assertEqual(command[command.index("--setting-sources") + 1], "")

    def test_claude_command_forces_claudeai_login_method(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("claude", "hello")
        self.assertIn("--settings", command)
        settings_json = command[command.index("--settings") + 1]
        self.assertEqual(json.loads(settings_json), {"forceLoginMethod": "claudeai"})

    def test_isolation_flags_are_present_even_with_a_model_and_effort(self) -> None:
        adapters = CliAdapters.__new__(CliAdapters)
        adapters.policy = _STRICT
        command = adapters._base_command("claude", "hello", "sonnet", "high")
        self.assertIn("--setting-sources", command)
        self.assertIn("--settings", command)
        self.assertIn("--model", command)
        self.assertIn("--effort", command)


class PreflightUsesSafeDefaultSelectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_all_passes_a_default_selection_into_build_commands(self) -> None:
        """Mirrors the real bug: preflight must never call build_commands()
        without a safe agent_selection, or claude's preflight command would
        run with no --model at all (trusting the CLI's own unverified local
        default). command_exists() is stubbed to False for every agent so no
        real CLI process is ever launched by this test."""
        captured: dict = {}
        original_build_commands = CliAdapters.build_commands
        original_command_exists = CliAdapters.command_exists

        def fake_build_commands(self, prompts, automation_level=2, agent_selection=None):
            captured["agent_selection"] = agent_selection
            return original_build_commands(self, prompts, automation_level, agent_selection)

        CliAdapters.build_commands = fake_build_commands
        CliAdapters.command_exists = lambda self, agent, catalog=None: False
        try:
            checker = HealthChecker()
            await checker.preflight_all(cwd=Path("."), automation_level=1)
        finally:
            CliAdapters.build_commands = original_build_commands
            CliAdapters.command_exists = original_command_exists

        selection = captured.get("agent_selection")
        self.assertIsNotNone(selection, "preflight_all() must always pass an agent_selection")
        self.assertEqual(selection.for_agent("claude")[0], "sonnet")


class CheckAntigravityHealthDisplayTest(unittest.TestCase):
    def test_agy_present_but_no_confirmed_default_reports_unavailable(self) -> None:
        """The GUI health badge must agree with the actual execution gate:
        `agy` merely being on PATH is not 'available' if command_exists()
        would refuse to run it."""
        original_health_which = health_checker_module.shutil.which
        original_cli_which = cli_adapters_module.shutil.which
        original_load_catalog = agent_model_selector.load_catalog

        def fake_which(name):
            return "/opt/homebrew/bin/agy" if name == "agy" else None

        health_checker_module.shutil.which = fake_which
        cli_adapters_module.shutil.which = fake_which
        agent_model_selector.load_catalog = lambda path=agent_model_selector.CATALOG_PATH: {
            "gemini": {"models": [{"id": "gemini-a", "billing_status": "unknown"}]}
        }
        try:
            status = HealthChecker(policy=_STRICT)._check_antigravity()
        finally:
            health_checker_module.shutil.which = original_health_which
            cli_adapters_module.shutil.which = original_cli_which
            agent_model_selector.load_catalog = original_load_catalog

        self.assertFalse(status.available)
        self.assertIn("subscription_safe", status.detail)


if __name__ == "__main__":
    unittest.main()
