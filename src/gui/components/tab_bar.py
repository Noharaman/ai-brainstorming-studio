from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - local environment dependent
    ctk = None


STRIP_COLOR = "#141414"
ACTIVE_TAB_COLOR = "#2b2b2b"
INACTIVE_TAB_COLOR = "#1f1f1f"
HOVER_TAB_COLOR = "#282828"
ACTIVE_TEXT_COLOR = "#f2f2f2"
INACTIVE_TEXT_COLOR = "#9a9a9a"
RUNNING_DOT_COLOR = "#3b82f6"
CLOSE_HOVER_COLOR = "#7f1d1d"

TAB_WIDTH = 190
TAB_HEIGHT = 34
MAX_TITLE_CHARS = 16


@dataclass
class TabInfo:
    tab_id: str
    title: str
    running: bool = False


class BrowserTabBar(ctk.CTkFrame if ctk else object):
    """A Chrome-like horizontal tab strip with per-tab close buttons and a "+" button.

    The strip owns no application state: it renders the `TabInfo` list it is given
    and reports user intent through the callbacks.
    """

    def __init__(
        self,
        master: any,
        on_select: Callable[[str], None],
        on_close: Callable[[str], None],
        on_new: Callable[[], None],
        **kwargs,
    ) -> None:
        if ctk is None:
            raise RuntimeError("customtkinter is required for BrowserTabBar")

        super().__init__(master, fg_color=STRIP_COLOR, corner_radius=0, **kwargs)
        self.on_select = on_select
        self.on_close = on_close
        self.on_new = on_new

        self._tabs: list[TabInfo] = []
        self._active_id: str = ""
        self._tab_widgets: dict[str, dict[str, any]] = {}

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)

        self.strip = ctk.CTkScrollableFrame(
            self,
            orientation="horizontal",
            fg_color=STRIP_COLOR,
            height=TAB_HEIGHT + 8,
            corner_radius=0,
        )
        self.strip.grid(row=0, column=0, sticky="ew", padx=(6, 0), pady=(4, 0))

        self.new_tab_button = ctk.CTkButton(
            self,
            text="＋",
            width=34,
            height=TAB_HEIGHT,
            corner_radius=8,
            fg_color="transparent",
            hover_color=HOVER_TAB_COLOR,
            text_color=INACTIVE_TEXT_COLOR,
            font=ctk.CTkFont(size=16, weight="bold"),
            command=self.on_new,
        )
        self.new_tab_button.grid(row=0, column=1, sticky="e", padx=(4, 8), pady=(4, 0))

    def set_tabs(self, tabs: list[TabInfo], active_id: str) -> None:
        self._tabs = list(tabs)
        self._active_id = active_id
        self._rebuild()

    def set_active(self, active_id: str) -> None:
        if active_id == self._active_id:
            return
        self._active_id = active_id
        self._restyle()

    def set_running(self, tab_id: str, running: bool) -> None:
        for tab in self._tabs:
            if tab.tab_id == tab_id:
                if tab.running == running:
                    return
                tab.running = running
                break
        else:
            return
        widgets = self._tab_widgets.get(tab_id)
        if widgets:
            widgets["dot"].configure(text="●" if running else "")

    def set_run_state_marker(self, tab_id: str, glyph: str, colour: str) -> None:
        """Draw a precise run-state marker, replacing the coarse running dot.

        `set_running()` still drives the strip on the start/finish boundaries;
        this narrows the same dot to the phase the run is actually in
        (approval pending, failed, ...) while it is between those boundaries.
        """
        widgets = self._tab_widgets.get(tab_id)
        if not widgets:
            return
        widgets["dot"].configure(text=glyph, text_color=colour or RUNNING_DOT_COLOR)

    def set_title(self, tab_id: str, title: str) -> None:
        for tab in self._tabs:
            if tab.tab_id == tab_id:
                if tab.title == title:
                    return
                tab.title = title
                break
        else:
            return
        widgets = self._tab_widgets.get(tab_id)
        if widgets:
            widgets["label"].configure(text=self._display_title(title))

    def _rebuild(self) -> None:
        for child in self.strip.winfo_children():
            child.destroy()
        self._tab_widgets = {}

        for index, tab in enumerate(self._tabs):
            self._tab_widgets[tab.tab_id] = self._build_tab(index, tab)
        self._restyle()

    def _build_tab(self, index: int, tab: TabInfo) -> dict[str, any]:
        container = ctk.CTkFrame(
            self.strip,
            width=TAB_WIDTH,
            height=TAB_HEIGHT,
            corner_radius=8,
            fg_color=INACTIVE_TAB_COLOR,
        )
        container.grid(row=0, column=index, padx=(0, 2), pady=(4, 0), sticky="w")
        container.grid_propagate(False)
        container.grid_columnconfigure(0, weight=0)
        container.grid_columnconfigure(1, weight=1)
        container.grid_columnconfigure(2, weight=0)

        dot = ctk.CTkLabel(
            container,
            text="●" if tab.running else "",
            width=12,
            font=ctk.CTkFont(size=10),
            text_color=RUNNING_DOT_COLOR,
        )
        dot.grid(row=0, column=0, padx=(8, 2))

        label = ctk.CTkLabel(
            container,
            text=self._display_title(tab.title),
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=INACTIVE_TEXT_COLOR,
        )
        label.grid(row=0, column=1, sticky="ew", padx=(2, 2))

        close_button = ctk.CTkButton(
            container,
            text="✕",
            width=20,
            height=20,
            corner_radius=10,
            fg_color="transparent",
            hover_color=CLOSE_HOVER_COLOR,
            text_color=INACTIVE_TEXT_COLOR,
            font=ctk.CTkFont(size=11),
            command=lambda tab_id=tab.tab_id: self.on_close(tab_id),
        )
        close_button.grid(row=0, column=2, padx=(2, 6))

        select = lambda _event=None, tab_id=tab.tab_id: self.on_select(tab_id)
        container.bind("<Button-1>", select)
        dot.bind("<Button-1>", select)
        label.bind("<Button-1>", select)

        return {"container": container, "dot": dot, "label": label, "close": close_button}

    def _restyle(self) -> None:
        for tab in self._tabs:
            widgets = self._tab_widgets.get(tab.tab_id)
            if not widgets:
                continue
            is_active = tab.tab_id == self._active_id
            widgets["container"].configure(
                fg_color=ACTIVE_TAB_COLOR if is_active else INACTIVE_TAB_COLOR
            )
            widgets["label"].configure(
                text_color=ACTIVE_TEXT_COLOR if is_active else INACTIVE_TEXT_COLOR
            )
            widgets["close"].configure(
                text_color=ACTIVE_TEXT_COLOR if is_active else INACTIVE_TEXT_COLOR,
                hover_color=CLOSE_HOVER_COLOR,
            )

    def _display_title(self, title: str) -> str:
        title = title.strip() or "新しいタブ"
        if len(title) > MAX_TITLE_CHARS:
            return title[: MAX_TITLE_CHARS - 1] + "…"
        return title
