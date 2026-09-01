"""Keyboard shortcuts, and where they are allowed to apply.

Focus is stubbed rather than driven through Tk: `focus_force()` does not
reliably move focus when the window is not the active OS window, so a test
that relied on it would pass or fail for reasons unrelated to the code.
"""

import unittest

from src.gui.app import BrainstormApp
from src.gui.project_tab import ProjectTab


def _app_with_focus_on(widget_class: str | None) -> BrainstormApp:
    app = BrainstormApp.__new__(BrainstormApp)

    class _Widget:
        def winfo_class(self):
            return widget_class

    class _Root:
        def focus_get(self):
            return None if widget_class is None else _Widget()

    app.root = _Root()
    return app


class TabCyclingStepsAsideWhileTypingTest(unittest.TestCase):
    """Shift+Tab moves focus backwards inside a text field. The app used to
    claim the key everywhere, so that never worked while typing."""

    def _cycle(self, widget_class):
        app = _app_with_focus_on(widget_class)
        calls = []
        app.cycle_tab = lambda step: calls.append(step) or "break"
        return app._cycle_tab_unless_editing(), calls

    def test_typing_in_a_text_box_is_left_alone(self) -> None:
        for widget_class in ("Text", "Entry", "TEntry"):
            with self.subTest(widget=widget_class):
                result, calls = self._cycle(widget_class)
                self.assertIsNone(result, "returning None hands the key back to Tk")
                self.assertEqual(calls, [], "tabs must not cycle while typing")

    def test_elsewhere_it_still_cycles(self) -> None:
        for widget_class in ("Button", "Frame", "CTkFrame", None):
            with self.subTest(widget=widget_class):
                result, calls = self._cycle(widget_class)
                self.assertEqual(result, "break")
                self.assertEqual(calls, [1])

    def test_a_broken_focus_query_does_not_raise(self) -> None:
        app = BrainstormApp.__new__(BrainstormApp)

        class _Root:
            def focus_get(self):
                raise RuntimeError("no display")

        app.root = _Root()
        self.assertFalse(app._focus_is_text_input())


class SubmitShortcutIsScopedToTheRequestBoxTest(unittest.TestCase):
    def test_it_is_bound_on_the_widget_not_globally(self) -> None:
        """bind_all fired from anywhere in the window, and fired twice when the
        focus was in the request box — once from the widget, once globally."""
        import inspect

        source = inspect.getsource(BrainstormApp._bind_shortcuts)
        self.assertNotIn("Command-Return", source)
        self.assertNotIn("Control-Return", source)

        bind_source = inspect.getsource(ProjectTab._bind_submit_shortcut)
        for sequence in (
            "<Command-Return>",
            "<Command-KP_Enter>",
            "<Control-Return>",
            "<Control-KP_Enter>",
        ):
            self.assertIn(sequence, bind_source)
        self.assertIn("self.request_text.bind", bind_source)


class SubmitSwallowsTheKeystrokeTest(unittest.TestCase):
    """Returning "break" is what stops Tk inserting a newline into the box
    being submitted. Measured: without it Tk appends "\\n"."""

    def _tab(self, running: bool):
        """A stand-in, not a real ProjectTab: `running` there is a read-only
        property derived from the run state machine. The method under test
        only reads `running` and calls `start_brainstorm`."""

        class _Tab:
            def __init__(self):
                self.running = running
                self.started = 0

            def start_brainstorm(self):
                self.started += 1

        return _Tab()

    def test_it_returns_break(self) -> None:
        tab = self._tab(running=False)
        self.assertEqual(ProjectTab.submit_from_shortcut(tab), "break")

    def test_an_idle_tab_starts_a_run(self) -> None:
        tab = self._tab(running=False)
        ProjectTab.submit_from_shortcut(tab)
        self.assertEqual(tab.started, 1)

    def test_a_running_tab_does_not_start_a_second(self) -> None:
        tab = self._tab(running=True)
        self.assertEqual(ProjectTab.submit_from_shortcut(tab), "break")
        self.assertEqual(tab.started, 0)

    def test_it_does_not_touch_the_request_text(self) -> None:
        """The old version deleted a trailing newline to clean up after the
        duplicate global binding; with only one binding left that would have
        eaten a newline the user typed deliberately."""
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(ProjectTab.submit_from_shortcut)))
        # Docstrings mention the old behaviour on purpose; look at the code.
        names = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        }
        self.assertNotIn("request_text", names)
        self.assertNotIn("delete", names)


if __name__ == "__main__":
    unittest.main()
