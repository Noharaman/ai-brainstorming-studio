"""What a tab in the strip tells you about its run.

Exercised through the marker resolution rather than through Tk, so the
assertions are about which glyph a tab should carry, not about rendering.
"""

import unittest

from src.gui.components.tab_bar import (
    UNREAD_MARKER,
    BrowserTabBar,
    TabInfo,
)
from src.services.run_state import STATE_MARKERS, RunState


class TabMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        # Widget-free: _marker_for() is pure, and _render_marker() no-ops when
        # there is no widget for the tab.
        self.bar = BrowserTabBar.__new__(BrowserTabBar)
        self.bar._tabs = [TabInfo("t1", "proj-a"), TabInfo("t2", "proj-b")]
        self.bar._tab_widgets = {}

    def _marker(self, tab_id: str) -> str:
        return self.bar._marker_for(self.bar._tab(tab_id))[0]

    def _enter(self, tab_id: str, state: RunState) -> None:
        self.bar.set_run_state_marker(tab_id, *STATE_MARKERS[state])

    def test_a_failed_run_still_shows_after_it_finishes(self) -> None:
        """The bug: finish_run sets the final marker and *then* reports
        running=False, and set_running() used to blank the same label — so a
        failure appeared and vanished in the same instant."""
        self.bar.set_running("t1", True)
        self._enter("t1", RunState.PLANNING)
        self._enter("t1", RunState.FAILED)
        self.bar.set_running("t1", False)

        self.assertEqual(self._marker("t1"), STATE_MARKERS[RunState.FAILED][0])
        self.assertNotEqual(self._marker("t1"), "")

    def test_the_phases_are_distinguishable(self) -> None:
        """running, waiting_approval, paused and failed must not look alike."""
        glyphs = {
            state: STATE_MARKERS[state][0]
            for state in (
                RunState.PLANNING,
                RunState.WAITING_APPROVAL,
                RunState.PAUSED,
                RunState.FAILED,
            )
        }
        self.assertEqual(
            len(set(glyphs.values())), len(glyphs), f"markers collide: {glyphs}"
        )
        for state, glyph in glyphs.items():
            with self.subTest(state=state):
                self._enter("t1", state)
                self.assertEqual(self._marker("t1"), glyph)

    def test_an_idle_tab_carries_nothing(self) -> None:
        self.assertEqual(self._marker("t1"), "")
        self._enter("t1", RunState.COMPLETED)
        self.assertEqual(self._marker("t1"), "")

    def test_a_finished_run_the_user_has_not_seen_is_marked(self) -> None:
        self._enter("t2", RunState.COMPLETED)
        self.bar.set_unread("t2", True)
        self.assertEqual(self._marker("t2"), UNREAD_MARKER)

    def test_looking_at_the_tab_clears_the_unread_mark(self) -> None:
        self.bar.set_unread("t2", True)
        self.bar.set_unread("t2", False)
        self.assertEqual(self._marker("t2"), "")

    def test_a_running_tab_outranks_an_unread_result(self) -> None:
        """A new run is the newer fact about the tab."""
        self.bar.set_unread("t1", True)
        self._enter("t1", RunState.PLANNING)
        self.assertEqual(self._marker("t1"), STATE_MARKERS[RunState.PLANNING][0])

    def test_markers_are_per_tab(self) -> None:
        self._enter("t1", RunState.FAILED)
        self.assertEqual(self._marker("t2"), "")

    def test_set_running_does_not_touch_the_marker(self) -> None:
        self._enter("t1", RunState.WAITING_APPROVAL)
        for running in (True, False, True, False):
            self.bar.set_running("t1", running)
        self.assertEqual(
            self._marker("t1"), STATE_MARKERS[RunState.WAITING_APPROVAL][0]
        )

    def test_the_marker_survives_a_rebuild(self) -> None:
        """Opening or closing one tab recreates every label; the others must
        not lose their state in the process."""
        self._enter("t1", RunState.FAILED)
        self.bar.set_unread("t2", True)

        # What _build_tab() would draw for each tab after a rebuild.
        self.assertEqual(self._marker("t1"), STATE_MARKERS[RunState.FAILED][0])
        self.assertEqual(self._marker("t2"), UNREAD_MARKER)

    def test_an_unknown_tab_id_is_ignored(self) -> None:
        self.bar.set_run_state_marker("nope", "✕", "#fff")
        self.bar.set_unread("nope", True)
        self.bar.set_running("nope", True)  # must not raise


class EveryRunStateHasAMarkerTest(unittest.TestCase):
    def test_no_state_is_missing_from_the_table(self) -> None:
        for state in RunState:
            with self.subTest(state=state):
                self.assertIn(state, STATE_MARKERS)

    def test_terminal_failure_is_visually_distinct_from_success(self) -> None:
        self.assertNotEqual(
            STATE_MARKERS[RunState.FAILED], STATE_MARKERS[RunState.COMPLETED]
        )



class UnreadWiringTest(unittest.TestCase):
    """Which tab gets the unread mark is decided by the app, not the strip."""

    def setUp(self) -> None:
        from src.gui.app import BrainstormApp

        self.app = BrainstormApp.__new__(BrainstormApp)
        self.app.tabs = []
        self.app.active_tab_id = "t1"
        self.calls: list[tuple] = []

        class _Bar:
            def __init__(self, calls):
                self.calls = calls

            def set_running(self, tab_id, running):
                self.calls.append(("running", tab_id, running))

            def set_unread(self, tab_id, unread):
                self.calls.append(("unread", tab_id, unread))

            def set_active(self, tab_id):
                self.calls.append(("active", tab_id))

        self.app.tab_bar = _Bar(self.calls)

        class _StatusBar:
            current_statuses: list = []

            def update_statuses(self, statuses, is_running=False):
                pass

        self.app.status_bar = _StatusBar()
        self.app._refresh_health = lambda: None

    def _unread_calls(self):
        return [c for c in self.calls if c[0] == "unread"]

    def test_a_background_tab_finishing_is_marked_unread(self) -> None:
        self.app.on_run_state_changed("t2", False)
        self.assertIn(("unread", "t2", True), self._unread_calls())

    def test_the_tab_you_are_watching_is_not_marked(self) -> None:
        """You just watched it finish; there is nothing unseen."""
        self.app.on_run_state_changed("t1", False)
        self.assertEqual(self._unread_calls(), [])

    def test_starting_a_run_never_marks_unread(self) -> None:
        self.app.on_run_state_changed("t2", True)
        self.assertEqual(self._unread_calls(), [])


if __name__ == "__main__":
    unittest.main()
