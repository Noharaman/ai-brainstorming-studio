from __future__ import annotations

import inspect
import queue
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from src import config
from src.gui.app import BrainstormApp
from src.gui.components.header_status_bar import (
    HeaderStatusBar,
    INITIAL_GLOBAL_STATUS_TEXT,
    setup_guidance_items,
    status_visual,
)
from src.gui.project_tab import ProjectTab
from src.models import CommandResult
from src.services import cli_setup_guidance
from src.services.cli_adapters import CliAdapters
from src.services.cli_setup_guidance import (
    ANTIGRAVITY_AUTH_DOCS_URL,
    ANTIGRAVITY_CLI_DOCS_URL,
    ANTIGRAVITY_INSTALL_COMMAND,
    ANTIGRAVITY_LOGIN_COMMAND,
    CLAUDE_AUTH_DOCS_URL,
    CLAUDE_CLI_DOCS_URL,
    CLAUDE_INSTALL_COMMAND,
    CLAUDE_LOGIN_COMMAND,
    CODEX_AUTH_DOCS_URL,
    CODEX_CLI_DOCS_URL,
    CODEX_CONFIG_DOCS_URL,
    CODEX_INSTALL_COMMAND,
    CODEX_LOGIN_COMMAND,
    for_status as setup_guidance_for_status,
)
from tests.support import enable_all_slots
from src.services.health_checker import (
    HealthChecker,
    ToolStatus,
    merge_health_statuses,
    tool_status_from_result,
)


class PreflightStatusCallbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_every_agent_publishes_a_status_when_commands_are_missing(self) -> None:
        published: list[ToolStatus] = []

        with patch.object(CliAdapters, "command_exists", return_value=False):
            results, _warnings = await HealthChecker().preflight_all(
                Path.cwd(),
                status_callback=published.append,
            )

        self.assertEqual(set(results), {"claude", "gemini", "codex"})
        self.assertEqual(
            {status.name for status in published},
            {"claude", "Antigravity(agy)", "codex"},
        )


class HealthStatusMergeTest(unittest.TestCase):
    def test_codex_discovery_does_not_claim_login_is_verified(self) -> None:
        """Finding the binary says nothing about whether the user is logged in."""
        enable_all_slots(self)
        with patch("src.services.health_checker.shutil.which", return_value="/usr/local/bin/codex"):
            status = HealthChecker()._check_command(
                "codex", installed_status="installed_unverified"
            )

        self.assertEqual(status.status, "installed_unverified")

    def test_a_closed_slot_outranks_discovery(self) -> None:
        """An installed CLI whose slot is closed is paused, not unverified.

        _check_claude and _check_antigravity already did this; the generic
        _check_command did not, so codex alone showed an ordinary lamp while
        the other two showed as paused.
        """
        with patch("src.services.health_checker.shutil.which", return_value="/usr/local/bin/codex"):
            status = HealthChecker()._check_command(
                "codex", installed_status="installed_unverified"
            )

        self.assertEqual(status.status, "slot_disabled")
        self.assertFalse(status.available)

    def test_runtime_auth_failure_outranks_binary_discovery(self) -> None:
        discovery = [
            ToolStatus(
                "codex",
                True,
                "/usr/local/bin/codex",
                status="installed_unverified",
                executable_path="/usr/local/bin/codex",
            )
        ]
        runtime = {
            "codex": ToolStatus(
                "codex",
                False,
                "ログインが必要です。",
                status="auth_required",
                source="preflight",
            )
        }

        merged = merge_health_statuses(discovery, runtime)

        self.assertEqual(merged[0].status, "auth_required")
        self.assertEqual(merged[0].source, "preflight")
        self.assertEqual(merged[0].executable_path, "/usr/local/bin/codex")

    def test_fresh_missing_discovery_outranks_stale_runtime_success(self) -> None:
        discovery = [ToolStatus("codex", False, "not found", status="command_missing")]
        runtime = {"codex": ToolStatus("codex", True, "worked", status="ok", source="run")}

        merged = merge_health_statuses(discovery, runtime)

        self.assertEqual(merged[0].status, "command_missing")
        self.assertFalse(merged[0].available)

    def test_command_result_uses_the_header_tool_name(self) -> None:
        result = CommandResult(agent="gemini", command=[], ok=False, status="rate_limited")

        status = tool_status_from_result(result)

        self.assertEqual(status.name, "Antigravity(agy)")
        self.assertEqual(status.status, "rate_limited")
        self.assertEqual(status.source, "preflight")


class ConnectionTestCleanupTest(unittest.TestCase):
    def test_unfinished_checking_state_is_restored_without_losing_completed_results(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        previous_codex = ToolStatus(
            "codex", False, "login needed", status="auth_required", source="run"
        )
        completed_claude = ToolStatus("claude", True, "worked", status="ok", source="preflight")
        app.runtime_statuses = {
            "codex": ToolStatus("codex", False, status="checking", source="preflight"),
            "claude": completed_claude,
        }
        app.connection_test_previous_statuses = {
            "codex": previous_codex,
            "claude": ToolStatus("claude", False, status="auth_required", source="run"),
        }
        app.discovery_statuses = []
        app.tabs = []

        class StatusBarStub:
            def update_statuses(self, statuses, is_running=False) -> None:
                self.statuses = statuses

        app.status_bar = StatusBarStub()

        app._clear_connection_test_placeholders()

        self.assertIs(app.runtime_statuses["codex"], previous_codex)
        self.assertIs(app.runtime_statuses["claude"], completed_claude)
        self.assertEqual(app.connection_test_previous_statuses, {})

    def test_connection_worker_passes_its_cancel_event_to_preflight(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        app.queue = queue.Queue()
        cancel_event = threading.Event()

        with patch("src.gui.app.HealthChecker") as checker_class:
            app._test_cli_connections_worker(Path.cwd(), cancel_event)

        checker_class.return_value.preflight_all_sync.assert_called_once()
        self.assertIs(
            checker_class.return_value.preflight_all_sync.call_args.kwargs["cancel_event"],
            cancel_event,
        )
        self.assertEqual(app.queue.get_nowait()[0], "connection_test_done")

    def test_connection_test_thread_start_failure_restores_state(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        app.connection_test_running = False
        app.connection_test_cancel_event = None
        app.connection_test_thread = None
        app.connection_test_previous_statuses = {}
        app.runtime_statuses = {}
        app.discovery_statuses = []
        app.tabs = []
        app.queue = queue.Queue()
        app._active_tab = lambda: None
        app._broadcast_log = lambda _text: None

        class StatusBarStub:
            current_statuses = []

            def update_statuses(self, statuses, is_running=False) -> None:
                self.statuses = statuses

        app.status_bar = StatusBarStub()

        with (
            patch("src.gui.app.messagebox.askyesno", return_value=True),
            patch("src.gui.app.messagebox.showerror") as showerror,
            patch("src.gui.app.threading.Thread.start", side_effect=RuntimeError("no threads")),
        ):
            app._test_cli_connections()

        self.assertFalse(app.connection_test_running)
        self.assertIsNone(app.connection_test_cancel_event)
        self.assertIsNone(app.connection_test_thread)
        self.assertEqual(app.connection_test_previous_statuses, {})
        showerror.assert_called_once()

    def test_app_close_cancels_and_waits_for_connection_test(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        app.tabs = []
        app.connection_test_running = True
        app.connection_test_cancel_event = threading.Event()
        app.connection_test_thread = object()
        app._model_check_after_id = None
        app._save_tabs = lambda: None
        wait_args = {}
        app._wait_for_shutdown = lambda running, deadline: wait_args.update(
            running=running, deadline=deadline
        )

        class BadgeStub:
            def configure(self, **kwargs) -> None:
                self.kwargs = kwargs

        class StatusBarStub:
            global_badge = BadgeStub()

        app.status_bar = StatusBarStub()

        with patch("src.gui.app.messagebox.askyesno", return_value=True):
            app._on_close()

        self.assertTrue(app.connection_test_cancel_event.is_set())
        self.assertEqual(wait_args["running"], [])
        self.assertIn("deadline", wait_args)

    def test_shutdown_wait_includes_the_connection_test_thread(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)

        class ThreadStub:
            alive = True

            def is_alive(self) -> bool:
                return self.alive

        class RootStub:
            destroyed = False
            callback = None

            def destroy(self) -> None:
                self.destroyed = True

            def after(self, _delay, callback) -> None:
                self.callback = callback

        thread = ThreadStub()
        root = RootStub()
        app.connection_test_thread = thread
        app.root = root

        app._wait_for_shutdown([], deadline=float("inf"))

        self.assertFalse(root.destroyed)
        self.assertIsNotNone(root.callback)

        thread.alive = False
        root.callback()
        self.assertTrue(root.destroyed)

    def test_brainstorm_is_blocked_while_connection_test_runs(self) -> None:
        tab = ProjectTab.__new__(ProjectTab)
        # `running` is derived from the state machine; no machine means idle.
        tab.run_state_machine = None
        tab.app = type("AppStub", (), {"connection_test_running": True})()

        with patch("src.gui.project_tab.messagebox.showinfo") as showinfo:
            tab.start_brainstorm()

        showinfo.assert_called_once()


class _AfterCancelRootStub:
    """RootStub extended with after_cancel(), for the periodic model-check
    scheduling tests below. Records what after()/after_cancel() were called
    with instead of driving a real Tk event loop."""

    def __init__(self) -> None:
        self.after_calls: list[tuple[int, object]] = []
        self.after_cancel_calls: list[object] = []
        self._next_id = 0

    def after(self, delay, callback):
        self._next_id += 1
        after_id = f"after#{self._next_id}"
        self.after_calls.append((delay, callback))
        return after_id

    def after_cancel(self, after_id) -> None:
        self.after_cancel_calls.append(after_id)

    def destroy(self) -> None:
        pass


class ModelRefreshSchedulingTest(unittest.TestCase):
    """The periodic 30-minute CLI-version/Antigravity-catalog check added
    alongside the manual '更新' button and the app-startup refresh. Exactly
    one place must start the self-rescheduling chain (__init__) and exactly
    one place continue it (_periodic_model_check's own finally) — these tests
    guard that invariant and the app-close cancellation ordering."""

    def test_schedule_periodic_model_check_registers_one_after_call(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        app.root = _AfterCancelRootStub()

        app._schedule_periodic_model_check()

        self.assertEqual(len(app.root.after_calls), 1)
        delay, callback = app.root.after_calls[0]
        self.assertEqual(delay, config.MODEL_CATALOG_CHECK_INTERVAL_SECONDS * 1000)
        self.assertEqual(callback, app._periodic_model_check)
        self.assertEqual(app._model_check_after_id, "after#1")

    def test_periodic_check_calls_refresh_and_reschedules(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        app.root = _AfterCancelRootStub()
        app.refresh_models_async = lambda: calls.append(1)
        calls: list[int] = []

        app._periodic_model_check()

        self.assertEqual(calls, [1])
        self.assertEqual(len(app.root.after_calls), 1, "must reschedule itself exactly once")
        self.assertIsNotNone(app._model_check_after_id)

    def test_periodic_check_reschedules_even_if_refresh_raises(self) -> None:
        """The finally is load-bearing: one bad tick must not silently end
        the recurring chain for the rest of the session."""
        app = BrainstormApp.__new__(BrainstormApp)
        app.root = _AfterCancelRootStub()

        def boom() -> None:
            raise RuntimeError("boom")

        app.refresh_models_async = boom

        with self.assertRaises(RuntimeError):
            app._periodic_model_check()

        self.assertEqual(len(app.root.after_calls), 1, "must still reschedule after the exception")

    def test_on_close_cancels_the_periodic_timer_when_closing(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)
        app.tabs = []
        app.connection_test_running = False
        app.root = _AfterCancelRootStub()
        app._model_check_after_id = "after#1"
        app._save_tabs = lambda: None

        with patch("src.gui.app.messagebox.askyesno") as askyesno:
            app._on_close()

        askyesno.assert_not_called()  # nothing running -> no confirmation gate to cross
        self.assertEqual(app.root.after_cancel_calls, ["after#1"])
        self.assertIsNone(app._model_check_after_id)

    def test_on_close_preserves_the_periodic_timer_when_the_user_declines(self) -> None:
        """Regression: the cancellation must sit AFTER the 'interrupt
        running work?' gate. A user who declines keeps the app open, so the
        periodic chain must keep running for the rest of that session."""
        app = BrainstormApp.__new__(BrainstormApp)
        running_tab = type("TabStub", (), {"running": True})()
        app.tabs = [running_tab]
        app.connection_test_running = False
        app.root = _AfterCancelRootStub()
        app._model_check_after_id = "after#1"

        with patch("src.gui.app.messagebox.askyesno", return_value=False):
            app._on_close()

        self.assertEqual(app.root.after_cancel_calls, [])
        self.assertEqual(app._model_check_after_id, "after#1")


class ProactiveModelImportPromptTest(unittest.TestCase):
    """_on_models_refreshed() previously showed one combined messagebox for
    every changed agent regardless of whether a human step was actually
    needed. Antigravity has a real listing command (agy models) and needs
    none; Claude/Codex have none at all, so 'changed' only ever means their
    --version string moved — a signal to re-paste /model output, which these
    tests verify gets offered instead of merely noted."""

    def _app(self) -> BrainstormApp:
        app = BrainstormApp.__new__(BrainstormApp)
        app.tabs = []
        app.root = object()  # only passed through to ModelImportDialog, which is patched below
        app._pending_model_import_agents = set()
        app._model_import_prompt_open = False
        return app

    def test_a_changed_gemini_result_is_a_plain_notice_not_an_import_offer(self) -> None:
        app = self._app()
        results = {"gemini": type("R", (), {"changed": True})()}

        with (
            patch("src.gui.app.messagebox.showinfo") as showinfo,
            patch("src.gui.app.messagebox.askyesno") as askyesno,
        ):
            app._on_models_refreshed(results)

        showinfo.assert_called_once()
        askyesno.assert_not_called()
        self.assertEqual(app._pending_model_import_agents, set())

    def test_a_changed_claude_result_offers_the_import_dialog_scoped_to_claude(self) -> None:
        app = self._app()
        results = {
            "claude": type("R", (), {"changed": True})(),
            "codex": type("R", (), {"changed": False})(),
        }

        with (
            patch("src.gui.app.messagebox.askyesno", return_value=True) as askyesno,
            patch("src.gui.app.ModelImportDialog") as dialog_cls,
        ):
            app._on_models_refreshed(results)

        askyesno.assert_called_once()
        dialog_cls.assert_called_once()
        self.assertEqual(dialog_cls.call_args.kwargs["focus_agents"], ["claude"])
        self.assertTrue(app._model_import_prompt_open)

    def test_declining_the_confirmation_releases_the_gate_without_opening_the_dialog(self) -> None:
        app = self._app()
        results = {"codex": type("R", (), {"changed": True})()}

        with (
            patch("src.gui.app.messagebox.askyesno", return_value=False),
            patch("src.gui.app.ModelImportDialog") as dialog_cls,
        ):
            app._on_models_refreshed(results)

        dialog_cls.assert_not_called()
        self.assertFalse(app._model_import_prompt_open)

    def test_a_second_changed_agent_does_not_stack_a_second_dialog(self) -> None:
        app = self._app()
        app._model_import_prompt_open = True  # a prompt is already up

        with patch("src.gui.app.messagebox.askyesno") as askyesno:
            app._on_models_refreshed({"claude": type("R", (), {"changed": True})()})

        askyesno.assert_not_called()
        self.assertEqual(app._pending_model_import_agents, {"claude"}, "must queue, not drop")

    def test_the_queued_agent_is_offered_once_the_open_dialog_closes(self) -> None:
        app = self._app()
        app._pending_model_import_agents = {"codex"}
        app._model_import_prompt_open = True

        with patch("src.gui.app.messagebox.askyesno") as askyesno:
            app._maybe_show_model_import_prompt()  # still gated, nothing happens yet
        askyesno.assert_not_called()

        app._model_import_prompt_open = False  # simulates the dialog's on_close_callback firing
        with (
            patch("src.gui.app.messagebox.askyesno", return_value=True) as askyesno,
            patch("src.gui.app.ModelImportDialog") as dialog_cls,
        ):
            app._maybe_show_model_import_prompt()

        askyesno.assert_called_once()
        self.assertEqual(dialog_cls.call_args.kwargs["focus_agents"], ["codex"])


class HeaderStatusVisualTest(unittest.TestCase):
    def test_initial_global_status_is_neutral_until_health_check_finishes(self) -> None:
        self.assertEqual(INITIAL_GLOBAL_STATUS_TEXT, "🔵 システム状態を確認中...")
        self.assertIn(
            "text=INITIAL_GLOBAL_STATUS_TEXT",
            inspect.getsource(HeaderStatusBar._build_ui),
        )

    def test_detail_dialog_collects_all_three_setup_rows_in_lamp_order(self) -> None:
        items = setup_guidance_items(
            [
                ToolStatus("codex", False, status="command_missing"),
                ToolStatus("Antigravity(agy)", False, status="auth_required"),
                ToolStatus("claude", False, status="command_missing"),
            ]
        )

        self.assertEqual(
            [item.title for item in items],
            [
                "Claude Code のインストール",
                "Antigravity CLI のログイン",
                "Codex CLI のインストール",
            ],
        )

    def test_installed_but_unverified_is_yellow(self) -> None:
        status = ToolStatus("codex", True, status="installed_unverified")
        self.assertEqual(status_visual(status)[0], "🟡")

    def test_preflight_success_is_green(self) -> None:
        status = ToolStatus("codex", True, status="ok", source="preflight")
        self.assertEqual(status_visual(status)[0], "🟢")

    def test_auth_required_is_red(self) -> None:
        status = ToolStatus("codex", False, status="auth_required", source="preflight")
        self.assertEqual(status_visual(status)[0], "🔴")


class CliSetupGuidanceTest(unittest.TestCase):
    def test_missing_codex_offers_reviewed_official_install_guidance(self) -> None:
        guidance = setup_guidance_for_status("codex", "command_missing")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, CODEX_INSTALL_COMMAND)
        self.assertEqual(guidance.official_url, CODEX_CLI_DOCS_URL)

    def test_logged_out_codex_offers_login_guidance_without_credentials(self) -> None:
        guidance = setup_guidance_for_status("codex", "auth_required")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, CODEX_LOGIN_COMMAND)
        self.assertEqual(guidance.official_url, CODEX_AUTH_DOCS_URL)
        self.assertNotIn("API_KEY", guidance.command)

    def test_missing_claude_offers_official_install_guidance(self) -> None:
        guidance = setup_guidance_for_status("claude", "command_missing")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, CLAUDE_INSTALL_COMMAND)
        self.assertEqual(guidance.official_url, CLAUDE_CLI_DOCS_URL)

    def test_claude_auth_failure_offers_interactive_login(self) -> None:
        guidance = setup_guidance_for_status("claude", "auth_required")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, CLAUDE_LOGIN_COMMAND)
        self.assertEqual(guidance.official_url, CLAUDE_AUTH_DOCS_URL)
        self.assertNotIn("--console", guidance.command)

    def test_missing_antigravity_offers_official_install_guidance(self) -> None:
        guidance = setup_guidance_for_status("Antigravity(agy)", "command_missing")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, ANTIGRAVITY_INSTALL_COMMAND)
        self.assertEqual(guidance.official_url, ANTIGRAVITY_CLI_DOCS_URL)

    def test_legacy_gemini_detection_still_offers_antigravity_installation(self) -> None:
        guidance = setup_guidance_for_status("Antigravity(agy)", "unsupported_client")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, ANTIGRAVITY_INSTALL_COMMAND)

    def test_antigravity_auth_failure_offers_interactive_login(self) -> None:
        guidance = setup_guidance_for_status("Antigravity(agy)", "auth_required")

        self.assertIsNotNone(guidance)
        self.assertEqual(guidance.command, ANTIGRAVITY_LOGIN_COMMAND)
        self.assertEqual(guidance.official_url, ANTIGRAVITY_AUTH_DOCS_URL)
        self.assertNotIn("API_KEY", guidance.command)

    def test_api_key_blocked_offers_settings_help_without_a_login_command(self) -> None:
        cases = (
            ("claude", CLAUDE_AUTH_DOCS_URL),
            ("Antigravity(agy)", ANTIGRAVITY_AUTH_DOCS_URL),
            ("codex", CODEX_CONFIG_DOCS_URL),
        )
        for tool_name, expected_url in cases:
            with self.subTest(tool_name=tool_name):
                guidance = setup_guidance_for_status(tool_name, "api_key_blocked")
                self.assertIsNotNone(guidance)
                self.assertIn("設定確認", guidance.title)
                self.assertEqual(guidance.command, "")
                self.assertEqual(guidance.command_button_label, "")
                self.assertEqual(guidance.official_url, expected_url)
                self.assertIn("変更しません", guidance.note)

    def test_unrelated_status_has_no_setup_action(self) -> None:
        self.assertIsNone(setup_guidance_for_status("claude", "rate_limited"))
        self.assertIsNone(setup_guidance_for_status("Antigravity(agy)", "timeout"))

    def test_guidance_module_cannot_execute_shell_or_login_commands(self) -> None:
        source = inspect.getsource(cli_setup_guidance)
        self.assertNotIn("subprocess", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("Popen", source)


if __name__ == "__main__":
    unittest.main()
