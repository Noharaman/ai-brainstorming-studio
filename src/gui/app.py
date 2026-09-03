from __future__ import annotations

import queue
import threading
import time
import uuid
from pathlib import Path
from tkinter import messagebox

from src import config
from src.gui.components.header_status_bar import HeaderStatusBar
from src.gui.components.model_import_dialog import ModelImportDialog
from src.gui.components.tab_bar import BrowserTabBar, TabInfo
from src.gui.project_tab import ProjectTab
from src.services.agent_model_detector import AgentModelDetector
from src.services.agent_model_selector import AGENT_DISPLAY_NAMES
from src.services.health_checker import (
    HealthChecker,
    ToolStatus,
    merge_health_statuses,
    tool_status_from_result,
)
from src.services.recent_projects_manager import RecentProjectsManager
from src.services.run_registry import ProjectRunRegistry
from src.services.run_state import STATE_MARKERS, RunState
from src.services.tab_session_manager import TabSessionManager

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - local environment dependent
    ctk = None


class BrainstormApp:
    """Shell window: global health header, browser-style tab strip, and tab content.

    Per-project state lives in `ProjectTab`. Tabs run independently, so several
    folders can be brainstormed in parallel; this class only routes worker
    messages back to the right tab on the Tk main thread.
    """

    def __init__(self) -> None:
        if ctk is None:
            raise RuntimeError("customtkinter is not installed. Run: pip install -r requirements.txt")

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title("AI Brainstorming Studio")
        self.root.geometry("1280x820")

        self.recent_manager = RecentProjectsManager()
        self.tab_sessions = TabSessionManager()
        self.run_registry = ProjectRunRegistry()
        # (kind, tab_id, run_id, payload). tab_id/run_id are "" for app-global
        # events (health checks); run_id lets a tab ignore a stale event from
        # a run it has already moved on from (cancelled + restarted, etc.).
        self.queue: queue.Queue[tuple[str, str, str, object]] = queue.Queue()
        self.health_running = False
        self.connection_test_running = False
        self.connection_test_cancel_event: threading.Event | None = None
        self.connection_test_thread: threading.Thread | None = None
        self.connection_test_previous_statuses: dict[str, ToolStatus] = {}
        self.discovery_statuses: list[ToolStatus] = []
        self.runtime_statuses: dict[str, ToolStatus] = {}

        self.tabs: list[ProjectTab] = []
        self.active_tab_id: str = ""

        # Session-only (never persisted) state for the proactive "a CLI
        # version changed, would you like to import its models?" flow.
        self._pending_model_import_agents: set[str] = set()
        self._model_import_prompt_open = False
        self._model_check_after_id: str | None = None

        self._build_ui()
        self._restore_tabs()
        self._bind_shortcuts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_queue()
        self._refresh_health()
        self.refresh_models_async()
        self._schedule_periodic_model_check()

    def run(self) -> None:
        self.root.mainloop()

    # ------------------------------------------------------------------- ui

    def _build_ui(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(self.root, corner_radius=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            header, text="AI Brainstorming Studio", font=ctk.CTkFont(size=18, weight="bold")
        ).grid(row=0, column=0, padx=12, pady=8, sticky="w")
        self.status_bar = HeaderStatusBar(
            header,
            on_refresh_callback=self._refresh_health,
            on_connection_test_callback=self._test_cli_connections,
        )
        self.status_bar.grid(row=0, column=1, padx=12, pady=8, sticky="e")

        self.tab_bar = BrowserTabBar(
            self.root,
            on_select=self.activate_tab,
            on_close=self.close_tab,
            on_new=self.new_tab,
        )
        self.tab_bar.grid(row=1, column=0, sticky="ew")

        self.content = ctk.CTkFrame(self.root, fg_color="transparent")
        self.content.grid(row=2, column=0, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

    # ----------------------------------------------------------------- tabs

    def _restore_tabs(self) -> None:
        saved = self.tab_sessions.load()
        seen_paths: set[str] = set()
        for entry in saved:
            resolved = str(Path(entry["project_path"]).resolve()) if entry["project_path"] else ""
            if resolved and resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            self.new_tab(
                project_path=entry["project_path"],
                room_id=entry["room_id"],
                automation_label=entry["automation_level"],
                activate=False,
                persist=False,
            )
        if not self.tabs:
            recents = self.recent_manager.get_recent_projects()
            self.new_tab(project_path=recents[0] if recents else "", activate=False, persist=False)

        index = min(self.tab_sessions.active_index(), len(self.tabs) - 1)
        self.activate_tab(self.tabs[index].tab_id)
        self._sync_tab_bar()

    def new_tab(
        self,
        project_path: str = "",
        room_id: str = "",
        automation_label: str = "",
        activate: bool = True,
        persist: bool = True,
    ) -> ProjectTab:
        if project_path:
            existing = self.find_tab_by_path(project_path)
            if existing:
                if activate:
                    self.activate_tab(existing.tab_id)
                return existing
        tab = ProjectTab(
            self.content,
            app=self,
            tab_id="tab_" + uuid.uuid4().hex[:8],
            project_path=project_path,
            automation_label=automation_label,
            room_id=room_id,
        )
        tab.frame.grid(row=0, column=0, sticky="nsew")
        self.tabs.append(tab)
        if activate:
            self._sync_tab_bar()
            self.activate_tab(tab.tab_id)
        if persist:
            self._save_tabs()
        return tab

    def close_tab(self, tab_id: str) -> None:
        tab = self._tab(tab_id)
        if not tab:
            return
        if tab.running and not messagebox.askyesno(
            "実行中", "このタブはブレスト実行中です。中断してタブを閉じますか？"
        ):
            return

        index = self.tabs.index(tab)
        self.tabs.remove(tab)
        tab.destroy()

        if not self.tabs:
            self.new_tab(activate=False, persist=False)
            index = 0
        self._sync_tab_bar()
        self.activate_tab(self.tabs[min(index, len(self.tabs) - 1)].tab_id)
        self._save_tabs()

    def activate_tab(self, tab_id: str) -> None:
        tab = self._tab(tab_id)
        if not tab:
            return
        self.active_tab_id = tab_id
        tab.frame.tkraise()
        self.tab_bar.set_active(tab_id)
        # Looking at the tab is what makes its result read.
        self.tab_bar.set_unread(tab_id, False)
        self._save_tabs()

    def cycle_tab(self, step: int) -> str:
        if len(self.tabs) < 2:
            return "break"
        index = next((i for i, tab in enumerate(self.tabs) if tab.tab_id == self.active_tab_id), 0)
        self.activate_tab(self.tabs[(index + step) % len(self.tabs)].tab_id)
        return "break"

    def on_tab_changed(self, tab_id: str) -> None:
        """A tab opened or switched its project folder."""
        self._sync_tab_bar()
        for tab in self.tabs:
            if not tab.project_path:
                tab.refresh_recent_buttons()
        self._save_tabs()

    def on_run_state_display_changed(self, tab_id: str, state: RunState) -> None:
        """Reflect one tab's run phase in the tab strip. Main thread only."""
        glyph, colour = STATE_MARKERS[state]
        self.tab_bar.set_run_state_marker(tab_id, glyph, colour)

    def on_run_state_changed(self, tab_id: str, running: bool) -> None:
        self.tab_bar.set_running(tab_id, running)
        if not running and tab_id != self.active_tab_id:
            # Finished while the user was on another tab. A failed run already
            # carries its own persistent marker, so this only has to cover the
            # case where nothing else would show: a result waiting unseen.
            self.tab_bar.set_unread(tab_id, True)
        self.status_bar.update_statuses(
            self.status_bar.current_statuses,
            is_running=any(tab.running for tab in self.tabs),
        )
        if not running:
            self._refresh_health()

    def refresh_models_async(self) -> None:
        if getattr(self, "_refreshing_models", False):
            return
        self._refreshing_models = True

        def _worker():
            try:
                results = AgentModelDetector().refresh_all()
                self.root.after(0, self._on_models_refreshed, results)
            except Exception:
                self._refreshing_models = False

        threading.Thread(target=_worker, daemon=True).start()

    def _schedule_periodic_model_check(self) -> None:
        """Self-rescheduling: exactly one place starts this chain (__init__)
        and exactly one place continues it (here, in the finally below) — the
        chain must never be started from anywhere else, or timer
        registrations would accumulate."""
        self._model_check_after_id = self.root.after(
            config.MODEL_CATALOG_CHECK_INTERVAL_SECONDS * 1000, self._periodic_model_check
        )

    def _periodic_model_check(self) -> None:
        self._model_check_after_id = None
        try:
            self.refresh_models_async()
        finally:
            self._schedule_periodic_model_check()

    def _on_models_refreshed(self, results: dict) -> None:
        self._refreshing_models = False
        for tab in self.tabs:
            tab.refresh_model_options(notify_disappeared=True)

        # Antigravity has a real listing command (agy models) — a change
        # needs no human step, so it stays a plain FYI notice.
        gemini_result = results.get("gemini")
        if getattr(gemini_result, "changed", False):
            messagebox.showinfo(
                "モデル情報更新",
                "Antigravity (Gemini) のモデル情報が更新されました。\n\n"
                "料金区分やモデル選択をご確認ください。",
            )

        # Claude/Codex have no listing command at all — "changed" here only
        # ever means their --version string moved, a signal that a human
        # should re-paste /model output. Offer to open that flow rather than
        # just noting it and hoping the user remembers to click "取込" later.
        version_changed_agents = [
            agent for agent in ("claude", "codex")
            if getattr(results.get(agent), "changed", False)
        ]
        if version_changed_agents:
            self._pending_model_import_agents.update(version_changed_agents)
            self._maybe_show_model_import_prompt()

    def _maybe_show_model_import_prompt(self) -> None:
        if self._model_import_prompt_open or not self._pending_model_import_agents:
            return
        agents = sorted(self._pending_model_import_agents)
        self._pending_model_import_agents.clear()
        self._model_import_prompt_open = True

        names = "、".join(AGENT_DISPLAY_NAMES.get(a, a) for a in agents)
        proceed = messagebox.askyesno(
            "モデル一覧の取り込み",
            f"{names} の CLI バージョンが変わりました。\n"
            "最新のモデル一覧を取り込みますか？\n\n"
            "「はい」を選ぶと、ターミナルで /model を実行して\n"
            "貼り付ける画面を開きます。",
        )
        if not proceed:
            self._model_import_prompt_open = False
            return
        self._open_model_import_dialog(agents)

    def _open_model_import_dialog(self, focus_agents: list[str]) -> None:
        def _on_success() -> None:
            for tab in self.tabs:
                tab.refresh_model_options(notify_disappeared=False)

        def _on_dialog_closed() -> None:
            self._model_import_prompt_open = False
            # Picks up anything that arrived while this dialog was open,
            # rather than silently dropping a second agent's offer.
            self._maybe_show_model_import_prompt()

        ModelImportDialog(
            self.root,
            on_success_callback=_on_success,
            on_close_callback=_on_dialog_closed,
            focus_agents=focus_agents,
        )

    def _sync_tab_bar(self) -> None:
        titles = self._unique_titles()
        self.tab_bar.set_tabs(
            [
                TabInfo(tab_id=tab.tab_id, title=titles[tab.tab_id], running=tab.running)
                for tab in self.tabs
            ],
            active_id=self.active_tab_id or (self.tabs[0].tab_id if self.tabs else ""),
        )

    def _unique_titles(self) -> dict[str, str]:
        """Disambiguate same-named folders with their parent directory, like a browser."""
        counts: dict[str, int] = {}
        for tab in self.tabs:
            counts[tab.title()] = counts.get(tab.title(), 0) + 1
        titles: dict[str, str] = {}
        for tab in self.tabs:
            base = tab.title()
            if counts[base] > 1 and tab.project_path:
                parent = Path(tab.project_path).parent.name
                titles[tab.tab_id] = f"{parent}/{base}" if parent else base
            else:
                titles[tab.tab_id] = base
        return titles

    def find_tab_by_path(self, path: str) -> ProjectTab | None:
        resolved = str(Path(path).resolve())
        return next((tab for tab in self.tabs if tab.project_path == resolved), None)

    def open_path(self, path: str) -> None:
        """Open `path` in its own tab: focus a tab already on that real folder,
        or create a fresh one. Never mutates another tab's project in place."""
        project = Path(path)
        if not project.is_dir():
            messagebox.showerror("Missing folder", f"フォルダが見つかりません:\n{path}")
            return
        existing = self.find_tab_by_path(str(project.resolve()))
        if existing:
            self.activate_tab(existing.tab_id)
            return
        self.new_tab(project_path=path)

    def _tab(self, tab_id: str) -> ProjectTab | None:
        return next((tab for tab in self.tabs if tab.tab_id == tab_id), None)

    def _active_tab(self) -> ProjectTab | None:
        return self._tab(self.active_tab_id)

    def _save_tabs(self) -> None:
        index = next((i for i, tab in enumerate(self.tabs) if tab.tab_id == self.active_tab_id), 0)
        self.tab_sessions.save([tab.session_state() for tab in self.tabs], active_index=index)

    # ------------------------------------------------------------ shortcuts

    def _bind_shortcuts(self) -> None:
        # Shift+Tab always cycles to the next tab, by explicit request: the
        # user wants Chrome's feel, where Ctrl+Tab works everywhere including
        # mid-edit in a text field. Shift+Tab is also the standard way to move
        # focus backwards inside a field, and claiming it here means that
        # reverse-focus navigation is unavailable while editing a request —
        # a deliberate trade the user chose over having the shortcut only work
        # some of the time.
        for sequence in ("<Shift-Tab>", "<ISO_Left_Tab>", "<Control-Tab>"):
            self.root.bind_all(sequence, lambda _event=None: self.cycle_tab(1))
        for sequence in ("<Control-Shift-Tab>", "<Command-Shift-braceleft>"):
            self.root.bind_all(sequence, lambda _event=None: self.cycle_tab(-1))
        self.root.bind_all("<Command-Shift-braceright>", lambda _event=None: self.cycle_tab(1))

        for sequence in ("<Command-t>", "<Control-t>"):
            self.root.bind_all(sequence, self._new_tab_shortcut)
        for sequence in ("<Command-w>", "<Control-w>"):
            self.root.bind_all(sequence, self._close_tab_shortcut)
        for number in range(1, 10):
            for prefix in ("Command", "Control"):
                self.root.bind_all(
                    f"<{prefix}-Key-{number}>",
                    lambda _event=None, n=number: self._jump_to_tab(n),
                )

        # Cmd/Ctrl+Enter is bound by each tab on its own request box
        # (ProjectTab._bind_submit_shortcut). It used to be bind_all, which
        # meant it fired from anywhere in the window — including while the
        # focus was in an unrelated field — and fired *twice* when the focus
        # was in the request box, once from the widget and once from here.

    def _new_tab_shortcut(self, _event: object | None = None) -> str:
        self.new_tab()
        return "break"

    def _close_tab_shortcut(self, _event: object | None = None) -> str:
        if self.active_tab_id:
            self.close_tab(self.active_tab_id)
        return "break"

    def _jump_to_tab(self, number: int) -> str:
        if 1 <= number <= len(self.tabs):
            self.activate_tab(self.tabs[number - 1].tab_id)
        return "break"


    # ----------------------------------------------------------- background

    def _refresh_health(self) -> None:
        if self.health_running:
            return
        self.health_running = True
        threading.Thread(target=self._refresh_health_worker, daemon=True).start()

    def _refresh_health_worker(self) -> None:
        try:
            statuses = HealthChecker().check_all()
            self.queue.put(("health", "", "", statuses))
        except Exception as exc:
            self.queue.put(("health_error", "", "", str(exc)))

    def _test_cli_connections(self) -> None:
        if self.connection_test_running:
            return
        if any(tab.running for tab in self.tabs):
            messagebox.showwarning(
                "実行中",
                "ブレスト実行中は別のAI接続テストを開始できません。実行終了後にお試しください。",
            )
            return
        if not messagebox.askyesno(
            "AI接続テスト",
            "各CLIへ短い確認プロンプトを送信します。契約枠を少量使用する可能性があります。\n\n"
            "ログイン・設定・認証情報は変更しません。続行しますか？",
        ):
            return

        tab = self._active_tab()
        project_root = Path(tab.project_path) if tab and tab.project_path else Path.cwd()
        self.connection_test_running = True
        self.connection_test_previous_statuses = dict(self.runtime_statuses)
        for status in self.status_bar.current_statuses:
            if status.name not in {"claude", "Antigravity(agy)", "codex"}:
                continue
            if status.status in {"command_missing", "slot_disabled", "unsupported_client"}:
                continue
            self._record_runtime_status(
                ToolStatus(
                    name=status.name,
                    available=False,
                    detail="AI接続を確認しています。",
                    status="checking",
                    source="preflight",
                    executable_path=status.executable_path,
                )
            )
        cancel_event = threading.Event()
        self.connection_test_cancel_event = cancel_event
        self.connection_test_thread = threading.Thread(
            target=self._test_cli_connections_worker,
            args=(project_root, cancel_event),
            daemon=True,
        )
        try:
            self.connection_test_thread.start()
        except Exception as exc:
            # The worker never started, so no queue completion event will reset
            # these fields or restore the temporary blue statuses.
            cancel_event.set()
            self.connection_test_running = False
            self.connection_test_cancel_event = None
            self.connection_test_thread = None
            self._clear_connection_test_placeholders()
            messagebox.showerror("起動エラー", f"AI接続テストを開始できませんでした:\n{exc}")
            self._broadcast_log(f"AI connection test start error: {exc}\n")

    def _test_cli_connections_worker(
        self, project_root: Path, cancel_event: threading.Event
    ) -> None:
        try:
            HealthChecker().preflight_all_sync(
                cwd=project_root,
                automation_level=1,
                prefer_rtk=False,
                cancel_event=cancel_event,
                status_callback=lambda status: self.queue.put(("agent_health", "", "", status)),
            )
            self.queue.put(("connection_test_done", "", "", None))
        except Exception as exc:
            self.queue.put(("connection_test_error", "", "", str(exc)))

    def _apply_discovery_statuses(self, statuses: list[ToolStatus]) -> None:
        self.discovery_statuses = statuses
        blockers = {"command_missing", "slot_disabled", "unsupported_client"}
        for status in statuses:
            if status.status in blockers:
                self.runtime_statuses.pop(status.name, None)
        self._render_health_statuses()

    def _record_runtime_status(self, status: ToolStatus) -> None:
        self.runtime_statuses[status.name] = status
        self._render_health_statuses()

    def _clear_connection_test_placeholders(self) -> None:
        """Remove only unfinished blue states, preserving completed results."""
        for name, status in list(self.runtime_statuses.items()):
            if status.status != "checking":
                continue
            previous = self.connection_test_previous_statuses.get(name)
            if previous is None:
                self.runtime_statuses.pop(name, None)
            else:
                self.runtime_statuses[name] = previous
        self.connection_test_previous_statuses = {}
        self._render_health_statuses()

    def _render_health_statuses(self) -> None:
        merged = merge_health_statuses(self.discovery_statuses, self.runtime_statuses)
        self.status_bar.update_statuses(
            merged,
            is_running=any(tab.running for tab in self.tabs),
        )

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, tab_id, run_id, payload = self.queue.get_nowait()
                if kind == "health":
                    statuses = payload
                    self.health_running = False
                    self.status_bar.last_health_error = ""
                    self._apply_discovery_statuses(statuses)
                    continue
                if kind == "health_error":
                    self.health_running = False
                    self.status_bar.show_health_error(str(payload))
                    self._broadcast_log(f"Health check error: {payload}\n")
                    continue
                if kind == "agent_health":
                    if tab_id:
                        source_tab = self._tab(tab_id)
                        if source_tab is None or (run_id and not source_tab.owns_run(run_id)):
                            continue
                    self._record_runtime_status(payload)
                    continue
                if kind == "connection_test_done":
                    self.connection_test_running = False
                    self.connection_test_cancel_event = None
                    self.connection_test_thread = None
                    self._clear_connection_test_placeholders()
                    self._broadcast_log("AI connection test finished.\n")
                    continue
                if kind == "connection_test_error":
                    self.connection_test_running = False
                    self.connection_test_cancel_event = None
                    self.connection_test_thread = None
                    self._clear_connection_test_placeholders()
                    self.status_bar.show_health_error(str(payload))
                    self._broadcast_log(f"AI connection test error: {payload}\n")
                    continue

                tab = self._tab(tab_id)
                if tab is None:
                    continue
                if run_id and not tab.owns_run(run_id):
                    # Stale event from a run this tab has already moved past
                    # (cancelled-then-restarted, etc.) — drop it.
                    continue
                if kind == "log":
                    tab.append_log(str(payload))
                elif kind == "run_state":
                    tab.apply_run_state(payload)
                elif kind == "result":
                    result, project, request, room_id = payload
                    for command_result in result.command_results.values():
                        if command_result.status not in {"cancelled", "skipped"}:
                            self._record_runtime_status(
                                tool_status_from_result(command_result, source="run")
                            )
                    tab.display_result(result, project, request, room_id)
                elif kind == "approval_request":
                    tab.show_approval_dialog(payload)
                elif kind == "error":
                    tab.append_log(f"ERROR: {payload}\n")
                    tab.finish_run(failed=True)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _broadcast_log(self, text: str) -> None:
        tab = self._active_tab()
        if tab:
            tab.append_log(text)

    def _on_close(self) -> None:
        running = [tab for tab in self.tabs if tab.running]
        connection_test_running = self.connection_test_running
        if running or connection_test_running:
            active_work = []
            if running:
                active_work.append(f"ブレスト {len(running)} 件")
            if connection_test_running:
                active_work.append("AI接続テスト")
            if not messagebox.askyesno(
                "実行中",
                f"{'、'.join(active_work)}が実行中です。中断して終了しますか？",
            ):
                return
        # Past this point the app is committed to closing. Stop the periodic
        # model check now, before anything else — it must not fire during
        # the shutdown grace period below, and this must stay after the
        # "interrupt running work?" gate above: a user who declines there
        # keeps the app open, and the periodic chain must keep running for
        # the rest of that session, not die here just because closing was
        # attempted once.
        if self._model_check_after_id is not None:
            self.root.after_cancel(self._model_check_after_id)
            self._model_check_after_id = None
        for tab in running:
            tab.request_cancel()
        if connection_test_running and self.connection_test_cancel_event is not None:
            self.connection_test_cancel_event.set()
        self._save_tabs()
        if running or connection_test_running:
            # Worker threads are daemon threads: if the interpreter exits before
            # they notice cancel_event and kill their subprocess (up to ~2.2s),
            # the CLI process would be orphaned. Give them a bounded grace period,
            # via after() so the Tk event loop stays responsive, before quitting.
            self.status_bar.global_badge.configure(text="🔵 終了処理中...")
            self._wait_for_shutdown(running, deadline=time.monotonic() + 3.0)
        else:
            self.root.destroy()

    def _wait_for_shutdown(self, running: list[ProjectTab], deadline: float) -> None:
        still_running = [tab for tab in running if tab.worker_thread and tab.worker_thread.is_alive()]
        connection_test_alive = bool(
            self.connection_test_thread and self.connection_test_thread.is_alive()
        )
        if (not still_running and not connection_test_alive) or time.monotonic() >= deadline:
            self.root.destroy()
            return
        self.root.after(100, lambda: self._wait_for_shutdown(still_running, deadline))


def run_app() -> None:
    if ctk is None:
        raise SystemExit("customtkinter is not installed. Run: pip install -r requirements.txt")
    BrainstormApp().run()
