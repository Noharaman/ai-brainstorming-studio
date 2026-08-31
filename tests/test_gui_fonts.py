"""Guards the mitigation for the Tcl-font-finalizer hang.

Every `CTkFont` is a real Tk font that Tcl deletes on finalization. When that
finalizer runs on a worker thread it can hang the thread — observed twice in
this project, most recently when an approval dialog and a questions panel each
added per-widget fonts and a full test run began hanging reproducibly. Keeping
fonts shared bounds the number of live Font objects by style rather than by
widget. See `src/gui/components/fonts.py` for the full account.
"""

import unittest

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None

from src.gui.components import fonts


@unittest.skipIf(ctk is None, "customtkinter is required")
class SharedFontTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # A Tk root must exist before any font object can be created.
        cls.root = ctk.CTk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.root.destroy()

    def test_the_same_style_returns_the_same_object(self) -> None:
        self.assertIs(fonts.font(12), fonts.font(12))
        self.assertIs(fonts.bold(13), fonts.bold(13))

    def test_different_styles_are_different_objects(self) -> None:
        self.assertIsNot(fonts.font(12), fonts.font(11))
        self.assertIsNot(fonts.font(12), fonts.bold(12))

    def test_repeated_requests_do_not_grow_the_cache(self) -> None:
        # Warm both styles first, so the count is independent of whatever
        # earlier tests happened to cache.
        fonts.font(12)
        fonts.bold(12)
        before = fonts.cached_count()
        for _ in range(50):
            fonts.font(12)
            fonts.bold(12)
        self.assertEqual(fonts.cached_count(), before)


class NoDirectFontConstructionTest(unittest.TestCase):
    """The UI added in 2026-08-31 must go through the cache.

    A source check rather than a behavioural one: the hang it prevents is
    timing-dependent and does not reproduce reliably in a test.
    """

    FILES = (
        "src/gui/components/chat_message_card.py",
        "src/gui/components/approval_dialog.py",
        "src/gui/components/chat_timeline.py",
    )

    def test_new_components_do_not_construct_fonts_directly(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for relative in self.FILES:
            with self.subTest(file=relative):
                source = (root / relative).read_text(encoding="utf-8")
                self.assertNotIn(
                    "ctk.CTkFont(",
                    source,
                    f"{relative} must use src.gui.components.fonts, not CTkFont directly",
                )


if __name__ == "__main__":
    unittest.main()
