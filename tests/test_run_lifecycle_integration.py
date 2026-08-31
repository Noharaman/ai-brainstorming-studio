"""Integration tests for the tab/worker/ProjectRunRegistry lifecycle.

These build a real BrainstormApp/ProjectTab widget tree (window withdrawn,
never shown) and drive it with `root.update()`, so they require a Tk-capable
environment (a real WindowServer/display session) — the same requirement as
running the app itself. There is no headless/Xvfb fallback; that's out of
scope for what is fundamentally a macOS desktop GUI app.

Run: python3 -m unittest tests.test_run_lifecycle_integration
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
import tkinter
import tkinter.messagebox as messagebox
import unittest
from pathlib import Path

import src.services.recent_projects_manager as rpm
import src.services.tab_session_manager as tsm
from src.gui.app import BrainstormApp
from src.models import BrainstormResult
from src.services.refinement_loop import RefinementLoop


def _wait_until(app: BrainstormApp, predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Pumps the Tk event loop until predicate() is true or timeout elapses.
    A deadline-based wait is far less flaky across machine speeds than a
    fixed iteration count."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if predicate():
            return True
        time.sleep(interval)
    app.root.update()
    return predicate()


def _pump_for(app: BrainstormApp, duration: float = 0.3, interval: float = 0.02) -> None:
    """Pumps the Tk event loop for a fixed short duration, with no predicate —
    used only to let _poll_queue drain events already on the queue (e.g. to
    prove a dropped stale event never arrives, where there's nothing to wait
    for a predicate to become true)."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        app.root.update()
        time.sleep(interval)
    app.root.update()


class RunLifecycleIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import sys
        import subprocess

        cmd = [
            sys.executable,
            "-c",
            "import tkinter; root=tkinter.Tk(); root.withdraw(); root.update(); root.destroy()",
        ]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
            if res.returncode != 0:
                raise unittest.SkipTest("Tk/WindowServer GUI session is not available (subprocess probe failed)")
        except Exception as exc:
            raise unittest.SkipTest(f"Tk/WindowServer GUI session is not available: {exc}")

    def setUp(self) -> None:

        # Every patch below registers its own restoration via addCleanup
        # *immediately*, rather than relying on tearDown(): tearDown() is
        # skipped entirely if setUp() raises partway through (e.g. if
        # BrainstormApp() construction itself fails), but addCleanup callbacks
        # still run — so a later failure here can't leave a class-level
        # monkeypatch (e.g. BrainstormApp._refresh_health) leaking into
        # every other test in the process.
        self.sandbox = Path(tempfile.mkdtemp(prefix="run-lifecycle-"))
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)

        # Isolate persisted app config from the developer's real ~/.ai-brainstorm-studio/.
        orig_recent_dir = rpm.CONFIG_DIR
        orig_recent_file = rpm.RECENT_PROJECTS_FILE
        orig_recent_defaults = rpm.RecentProjectsManager.__init__.__defaults__
        orig_tabs_dir = tsm.CONFIG_DIR
        orig_tabs_file = tsm.OPEN_TABS_FILE
        orig_tabs_defaults = tsm.TabSessionManager.__init__.__defaults__

        def _restore_config() -> None:
            rpm.CONFIG_DIR = orig_recent_dir
            rpm.RECENT_PROJECTS_FILE = orig_recent_file
            rpm.RecentProjectsManager.__init__.__defaults__ = orig_recent_defaults
            tsm.CONFIG_DIR = orig_tabs_dir
            tsm.OPEN_TABS_FILE = orig_tabs_file
            tsm.TabSessionManager.__init__.__defaults__ = orig_tabs_defaults

        self.addCleanup(_restore_config)

        rpm.CONFIG_DIR = self.sandbox / "cfg"
        rpm.RECENT_PROJECTS_FILE = rpm.CONFIG_DIR / "recent_projects.json"
        rpm.RecentProjectsManager.__init__.__defaults__ = (rpm.RECENT_PROJECTS_FILE,)
        tsm.CONFIG_DIR = self.sandbox / "cfg"
        tsm.OPEN_TABS_FILE = tsm.CONFIG_DIR / "open_tabs.json"
        tsm.TabSessionManager.__init__.__defaults__ = (tsm.OPEN_TABS_FILE,)

        self.proj = self.sandbox / "proj-x"
        self.proj.mkdir(parents=True)

        # BrainstormApp() normally spawns a background health-check thread that
        # calls the real HealthChecker/LMStudioManager, which can attempt to
        # launch LM Studio for real (`lms server start`) or shell out to `rtk`
        # if LM Studio isn't already running. Tests must not depend on, or
        # cause side effects in, real external applications.
        orig_refresh_health = BrainstormApp._refresh_health
        orig_refresh_models = BrainstormApp.refresh_models_async
        BrainstormApp._refresh_health = lambda self: None
        BrainstormApp.refresh_models_async = lambda self: None
        self.addCleanup(setattr, BrainstormApp, "_refresh_health", orig_refresh_health)
        self.addCleanup(setattr, BrainstormApp, "refresh_models_async", orig_refresh_models)

        orig_run_sync = RefinementLoop.run_sync
        self.addCleanup(setattr, RefinementLoop, "run_sync", orig_run_sync)

        orig_showinfo = messagebox.showinfo
        orig_showerror = messagebox.showerror
        orig_askyesno = messagebox.askyesno

        messagebox.showinfo = lambda *a, **k: None
        messagebox.showerror = lambda *a, **k: None
        messagebox.askyesno = lambda *a, **k: True

        def _restore_messagebox() -> None:
            messagebox.showinfo = orig_showinfo
            messagebox.showerror = orig_showerror
            messagebox.askyesno = orig_askyesno

        self.addCleanup(_restore_messagebox)

        # Every gate ever handed out and every tab a test touches (including
        # ones later removed from self.app.tabs via close_tab) are tracked
        # here, so cleanup can unblock and join them all even if the test
        # failed an assertion partway through and never got to do so itself.
        self._gates: list[threading.Event] = []
        self._tracked_tabs: list = []

        self.app = BrainstormApp()
        self.app.root.withdraw()
        self.app.root.update()
        # Registered in this order so LIFO cleanup runs _release_and_join_all
        # (pure Python threading, no Tk needed) before _destroy_root.
        self.addCleanup(self._destroy_root)
        self.addCleanup(self._release_and_join_all)

    def _release_and_join_all(self) -> None:
        for gate in self._gates:
            gate.set()
        for tab in self._tracked_tabs:
            if tab.worker_thread and tab.worker_thread.is_alive():
                tab.cancel_event.set()
                tab.worker_thread.join(timeout=2)
                if tab.worker_thread.is_alive():
                    raise AssertionError(
                        f"worker thread for tab {getattr(tab, 'tab_id', '?')} did not "
                        "finish within the 2s join timeout in cleanup — a fake "
                        "run_sync gate was probably left unreleased by the test."
                    )

    def _destroy_root(self) -> None:
        try:
            for after_id in self.app.root.tk.call('after', 'info'):
                try:
                    self.app.root.after_cancel(after_id)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.app.root.quit()
        except Exception:
            pass
        try:
            self.app.root.destroy()
        except (tkinter.TclError, Exception):
            pass

    def _install_gated_run_sync(self, gate_count: int) -> list[threading.Event]:
        """Each call to RefinementLoop.run_sync blocks on its own gate, so
        timing between concurrent/sequential runs is fully controllable."""
        gates = [threading.Event() for _ in range(gate_count)]
        self._gates.extend(gates)
        call_count = {"n": 0}

        def fake_run_sync(_self, project_root, request, automation_level, progress, cancel_event, *args, **kwargs):
            gate = gates[call_count["n"]]
            call_count["n"] += 1
            progress("fake run started")
            gate.wait(timeout=5)
            return BrainstormResult(
                session_id="fake-session",
                context_pack="",
                prompts={},
                command_results={},
                integrated_summary="",
                final_answer="結論:\nテスト\n次にやること:\nテスト\n",
            )

        RefinementLoop.run_sync = fake_run_sync
        return gates

    def _track(self, tab):
        """Registers `tab` so tearDown can join its worker even if the test
        later closes the tab (removing it from self.app.tabs) or fails an
        assertion before doing its own cleanup."""
        self._tracked_tabs.append(tab)
        return tab

    def test_close_tab_race_then_stale_event_exclusion(self) -> None:
        """Reproduces the full scenario from the Unit 1 review:
        1. start a run on folder X in tab1
        2/3. close tab1 (via the real app.close_tab()) while its worker is
             still alive
        4/5. a new tab on the same folder is rejected while the old worker lives
        6. the rejected attempt never touches chat_rooms.json (fix for bug #1)
        7/8. once the old worker truly finishes, the registry frees up and a
             new run on the same folder is accepted
        9/10. a stale run_id event is dropped; a current run_id event is delivered
        """
        gates = self._install_gated_run_sync(gate_count=2)

        tab1 = self._track(self.app.tabs[0])
        tab1.open_project(str(self.proj))
        self.app.root.update()
        tab1.request_text.insert("1.0", "最初の依頼")

        tab1.start_brainstorm()
        self.assertTrue(_wait_until(self.app, lambda: self.app.run_registry.active_run_for(self.proj) is not None))
        first_run_id = tab1.active_run.run_id
        rooms_path = self.proj / ".ai-brainstorm" / "chat_rooms.json"
        content_while_running = rooms_path.read_text(encoding="utf-8")

        # Close tab1 through the real, user-facing code path (not a manual
        # tab.destroy() + tabs.remove() shortcut), so this also exercises
        # request_cancel() the same way a real close-with-confirm click does.
        messagebox.askyesno = lambda *a, **k: True
        worker1 = tab1.worker_thread
        self.app.close_tab(tab1.tab_id)
        self.assertTrue(tab1.cancelling)
        self.assertTrue(worker1.is_alive())
        self.assertIsNotNone(self.app.run_registry.active_run_for(self.proj))

        tab2 = self._track(self.app.new_tab(project_path=str(self.proj)))
        self.app.root.update()
        tab2.request_text.insert("1.0", "二番目の依頼")
        blocked = {"flag": False}
        messagebox.showinfo = lambda *a, **k: blocked.__setitem__("flag", True)
        tab2.start_brainstorm()
        self.app.root.update()
        self.assertTrue(blocked["flag"])
        self.assertFalse(tab2.running)

        self.assertEqual(
            rooms_path.read_text(encoding="utf-8"),
            content_while_running,
            "a rejected start must not touch chat_rooms.json at all",
        )

        gates[0].set()
        # Join the actual worker thread object directly rather than polling
        # registry state through the Tk event loop. This assertion measured
        # as genuinely flaky under the full suite (worker1 was still alive
        # after 6s of _wait_until polling, no trace of it ever reaching
        # fake_run_sync) but reliable when instrumented with extra print()
        # calls — a signature of GIL scheduling starvation, not a logic bug:
        # print() releases the GIL for its write syscall, incidentally
        # giving the newly-started worker thread a scheduling opportunity
        # that a tight, print-free window did not. A direct Thread.join()
        # is the correct fix because it is a blocking C call that itself
        # releases the GIL for its whole duration, so the worker gets
        # scheduled promptly regardless of what the main thread was doing —
        # unlike _wait_until's poll loop, which only yields the GIL in short
        # time.sleep(interval) bursts between predicate checks.
        worker1.join(timeout=6.0)
        self.assertFalse(worker1.is_alive(), "tab1's worker never finished after its gate was released")
        self.assertTrue(_wait_until(self.app, lambda: self.app.run_registry.active_run_for(self.proj) is None))

        tab2.start_brainstorm()
        self.assertTrue(_wait_until(self.app, lambda: tab2.running))
        self.assertIsNotNone(tab2.active_run)
        second_run_id = tab2.active_run.run_id
        self.assertNotEqual(first_run_id, second_run_id)

        self.app.queue.put(("log", tab2.tab_id, first_run_id, "STALE MESSAGE\n"))
        _pump_for(self.app)
        self.assertNotIn("STALE MESSAGE", tab2.log_text.get("1.0", "end"))

        self.app.queue.put(("log", tab2.tab_id, second_run_id, "CURRENT MESSAGE\n"))
        self.assertTrue(_wait_until(self.app, lambda: "CURRENT MESSAGE" in tab2.log_text.get("1.0", "end")))

        gates[1].set()  # let the still-blocked second worker finish so it doesn't linger
        self.assertTrue(_wait_until(self.app, lambda: not tab2.worker_thread.is_alive()))

    def test_thread_start_failure_releases_the_registry_slot(self) -> None:
        """If Thread.start() itself raises, _run_worker's own finally-release
        never runs. This must not permanently block the folder from running."""
        tab = self._track(self.app.tabs[0])
        tab.open_project(str(self.proj))
        self.app.root.update()
        tab.request_text.insert("1.0", "依頼")

        original_thread_start = threading.Thread.start

        def failing_start(self):
            raise RuntimeError("simulated thread start failure")

        errored = {"flag": False}
        messagebox.showerror = lambda *a, **k: errored.__setitem__("flag", True)
        threading.Thread.start = failing_start
        try:
            tab.start_brainstorm()
        finally:
            threading.Thread.start = original_thread_start

        self.assertTrue(errored["flag"])
        self.assertFalse(tab.running)
        self.assertIsNone(tab.worker_thread)
        self.assertIsNone(self.app.run_registry.active_run_for(self.proj))

        gates = self._install_gated_run_sync(gate_count=1)
        tab.start_brainstorm()
        self.assertTrue(_wait_until(self.app, lambda: tab.running), "the same folder must be immediately retryable")
        gates[0].set()
        self.assertTrue(_wait_until(self.app, lambda: not tab.worker_thread.is_alive()))

    def test_app_close_routes_through_request_cancel(self) -> None:
        """_on_close() must set `cancelling` the same way the cancel button
        and tab-close do, by going through request_cancel() rather than
        touching cancel_event directly."""
        gates = self._install_gated_run_sync(gate_count=1)
        tab = self._track(self.app.tabs[0])
        tab.open_project(str(self.proj))
        self.app.root.update()
        tab.request_text.insert("1.0", "依頼")
        tab.start_brainstorm()
        self.assertTrue(_wait_until(self.app, lambda: tab.running))
        self.assertFalse(tab.cancelling)

        messagebox.askyesno = lambda *a, **k: True
        self.app._on_close()

        self.assertTrue(tab.cancelling, "_on_close must route through request_cancel()")
        self.assertTrue(tab.cancel_event.is_set())

        # Let the real shutdown-wait proceed to completion: once the worker
        # finishes, _wait_for_shutdown() will call the real root.destroy() on
        # its own (same as production behavior) — tearDown() tolerates that.
        gates[0].set()
        self.assertTrue(_wait_until(self.app, lambda: not tab.worker_thread.is_alive(), timeout=4.0))


if __name__ == "__main__":
    unittest.main()
