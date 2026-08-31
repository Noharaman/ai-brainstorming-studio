from __future__ import annotations

import threading
from tkinter import messagebox
from typing import Any, Callable

from src.services.agent_model_selector import AGENT_DISPLAY_NAMES
from src.services.model_catalog_updater import parse_and_update_models_from_text

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None


def _dialog_copy(focus_agents: list[str] | None) -> tuple[str, str, str]:
    """(window_title, header_text, description_text) for the dialog.

    Generic 3-agent copy when opened via the manual "取込" button
    (focus_agents=None); focused copy naming the specific agent(s) when
    opened proactively after a detected CLI version change. Either way the
    dialog still accepts pasted output for any agent — scoping is copy-text
    only, never a content restriction, since parse_and_update_models_from_text()
    itself has no per-agent concept to enforce that against.
    """
    if focus_agents:
        names = "、".join(AGENT_DISPLAY_NAMES.get(a, a) for a in focus_agents)
        title = f"📋 {names} のモデル一覧を更新"
        header = f"📋 {names} の /model 画面テキストから更新"
        desc = (
            f"{names} で「/model」を実行して表示された画面テキストをコピーして、\n"
            "下の枠にそのまま貼り付けてください。\n"
            "（他のAI CLIの出力が混ざっていても、議長AIが自動で判別・解析します）"
        )
        return title, header, desc
    title = "📋 議長AIでモデル一覧を更新"
    header = "📋 ターミナルの /model 画面テキストから更新"
    desc = (
        "各AI CLI (Claude, Codex, Antigravity) で「/model」を入力して表示された\n"
        "画面テキストをコピーして、下の枠にそのまま貼り付けてください。\n"
        "（1社だけでも、複数社混ざった状態でも、議長AIが自動で判別・解析します）"
    )
    return title, header, desc


class ModelImportDialog(ctk.CTkToplevel if ctk else object):
    """Modal dialog for pasting terminal /model output to update models using Chair AI."""

    def __init__(
        self,
        parent: Any,
        on_success_callback: Callable[[], None] | None = None,
        on_close_callback: Callable[[], None] | None = None,
        focus_agents: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(parent, **kwargs)
        self.on_success_callback = on_success_callback
        self.on_close_callback = on_close_callback

        title_text, header_text, desc_text = _dialog_copy(focus_agents)
        self.title(title_text)
        self.geometry("580x480")
        self.minsize(480, 360)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Make modal
        self.transient(parent)
        self.grab_set()

        # 1. Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text=header_text,
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        # 2. Description
        ctk.CTkLabel(
            self,
            text=desc_text,
            font=ctk.CTkFont(size=12),
            text_color="gray60",
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 8))

        # 3. Text Area
        self.text_box = ctk.CTkTextbox(
            self,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.text_box.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 10))

        # 4. Status & Action Buttons
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        footer.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            footer,
            text="",
            font=ctk.CTkFont(size=12),
            text_color="gray60",
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        self.cancel_btn = ctk.CTkButton(
            footer,
            text="キャンセル",
            width=80,
            command=self.destroy,
        )
        self.cancel_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))

        self.submit_btn = ctk.CTkButton(
            footer,
            text="✨ 議長AIで解析・更新",
            font=ctk.CTkFont(weight="bold"),
            command=self._on_submit,
        )
        self.submit_btn.grid(row=0, column=2, sticky="e")

    def _on_submit(self) -> None:
        raw_text = self.text_box.get("1.0", "end").strip()
        if not raw_text:
            messagebox.showerror("入力エラー", "テキストが空です。/model の出力テキストを貼り付けてください。")
            return

        self.submit_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.status_label.configure(text="👑 議長AI (LM Studio) がテキストを解析中...")

        def _worker() -> None:
            success, msg, _updated = parse_and_update_models_from_text(raw_text)
            self.after(0, self._on_complete, success, msg)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_complete(self, success: bool, msg: str) -> None:
        self.submit_btn.configure(state="normal")
        self.cancel_btn.configure(state="normal")
        self.status_label.configure(text="")

        if success:
            messagebox.showinfo("更新完了", msg)
            if self.on_success_callback:
                self.on_success_callback()
            self.destroy()
        else:
            messagebox.showerror("解析エラー", msg)

    def destroy(self) -> None:
        """Fire on_close_callback exactly once, regardless of which of the
        three close paths triggered it (Cancel button, the native window
        close box, or the success path above calling destroy() directly)."""
        if self.on_close_callback:
            callback = self.on_close_callback
            self.on_close_callback = None
            callback()
        super().destroy()
