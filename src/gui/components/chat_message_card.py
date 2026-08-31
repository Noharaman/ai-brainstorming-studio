from __future__ import annotations

from typing import Any
import tkinter as tk

from src.gui.components import fonts

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None


AGENT_NAMES = {
    "claude": "🧠 Claude",
    "gemini": "⚡️ Antigravity",
    "codex": "💻 Codex",
}

#: Shown only for turns that actually reached the write phase.
DIFF_TAB = "📝 変更差分"


class UserMessageCard(ctk.CTkFrame if ctk else object):
    """User message bubble in the chat timeline."""

    def __init__(self, master: Any, content: str, created_at: str = "", **kwargs: Any) -> None:
        super().__init__(
            master,
            fg_color=("gray85", "gray22"),
            corner_radius=12,
            border_width=1,
            border_color=("gray75", "gray30"),
            **kwargs,
        )
        self.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="👤 あなた",
            font=fonts.bold(12),
            text_color=("gray10", "gray90"),
        ).grid(row=0, column=0, sticky="w")

        if created_at:
            # Short timestamp
            ts_short = created_at.replace("T", " ")[:19]
            ctk.CTkLabel(
                header,
                text=ts_short,
                font=fonts.font(10),
                text_color="gray50",
            ).grid(row=0, column=1, sticky="e")

        # Content Textbox (read-only, auto-height feel)
        lines = content.strip().count("\n") + 1
        calc_height = max(40, min(300, lines * 20 + 20))
        self.text_box = ctk.CTkTextbox(
            self,
            font=fonts.font(13),
            fg_color="transparent",
            wrap="word",
            height=calc_height,
            activate_scrollbars=True if calc_height >= 250 else False,
        )
        self.text_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        self.text_box.insert("1.0", content.strip())
        self.text_box.configure(state="disabled")


class AssistantTurnCard(ctk.CTkFrame if ctk else object):
    """AI turn card displaying Chair synthesis with tabs for multi-agent responses."""

    def __init__(
        self,
        master: Any,
        summary_content: str,
        agent_outputs: dict[str, str] | None = None,
        created_at: str = "",
        session_id: str = "",
        questions: list[str] | None = None,
        implementation: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            master,
            fg_color=("gray92", "gray17"),
            corner_radius=12,
            border_width=1,
            border_color=("gray80", "gray28"),
            **kwargs,
        )
        self.grid_columnconfigure(0, weight=1)
        self.summary_content = summary_content.strip()
        self.agent_outputs = agent_outputs or {}
        self.questions = questions or []
        # Only a run that actually reached the write phase has a diff to show.
        self.implementation = (
            implementation if getattr(implementation, "attempted", False) else None
        )

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_frame,
            text="👑 秘書兼議長AI",
            font=fonts.bold(13),
            text_color=("#1f538d", "#4da3ff"),
        ).pack(side="left")

        if session_id:
            ctk.CTkLabel(
                title_frame,
                text=f"  ({session_id})",
                font=fonts.font(10),
                text_color="gray50",
            ).pack(side="left")

        if created_at:
            ts_short = created_at.replace("T", " ")[:19]
            ctk.CTkLabel(
                header,
                text=ts_short,
                font=fonts.font(10),
                text_color="gray50",
            ).grid(row=0, column=1, sticky="e")

        # Tab Selector (Chair Summary + 3 AI Individual Outputs + the diff)
        self.tab_values = ["📊 議長まとめ"]
        for key in ("claude", "gemini", "codex"):
            if key in self.agent_outputs and self.agent_outputs[key]:
                self.tab_values.append(AGENT_NAMES.get(key, key))
        if self.implementation is not None:
            self.tab_values.append(DIFF_TAB)

        if len(self.tab_values) > 1:
            self.seg_btn = ctk.CTkSegmentedButton(
                self,
                values=self.tab_values,
                command=self._on_tab_change,
                height=26,
                font=fonts.font(11),
            )
            self.seg_btn.set("📊 議長まとめ")
            self.seg_btn.grid(row=1, column=0, sticky="w", padx=12, pady=(0, 6))

        # Content Text Box
        self.content_box = ctk.CTkTextbox(
            self,
            font=fonts.font(12),
            wrap="word",
            height=280,
            activate_scrollbars=True,
        )
        bottom_pad = 6 if self.questions else 10
        self.content_box.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, bottom_pad))
        self._render_text(self.summary_content)

        if self.questions:
            self._build_questions_panel()

    def _build_questions_panel(self) -> None:
        """Surface the chair's open questions so they are not buried in the summary."""
        panel = ctk.CTkFrame(
            self,
            fg_color=("#fff4e0", "#3a2f1c"),
            corner_radius=8,
            border_width=1,
            border_color=("#e0a750", "#7a5a20"),
        )
        panel.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 10))
        panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            panel,
            text=f"❓ 確認事項 ({len(self.questions)})",
            font=fonts.bold(12),
            text_color=("#8a5a00", "#f0c070"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))

        for index, question in enumerate(self.questions, start=1):
            ctk.CTkLabel(
                panel,
                text=f"{index}. {question}",
                font=fonts.font(12),
                text_color=("gray10", "gray90"),
                anchor="w",
                justify="left",
                wraplength=560,
            ).grid(row=index, column=0, sticky="ew", padx=10, pady=(0, 6))

    def _on_tab_change(self, selected_tab: str) -> None:
        if "議長まとめ" in selected_tab:
            self._render_text(self.summary_content)
            return
        if selected_tab == DIFF_TAB:
            self._render_text(self._diff_report())
            return

        for key, name in AGENT_NAMES.items():
            if name == selected_tab:
                raw_output = self.agent_outputs.get(key, "(このAIからの回答はありません)")
                self._render_text(raw_output)
                return

    def _diff_report(self) -> str:
        """The write phase as text: what changed, whether tests passed, how to
        undo it. Assembled from the outcome rather than from the chair's prose,
        so a model cannot reword or omit what happened on disk."""
        outcome = self.implementation
        if outcome is None:
            return "(実装は行われていません)"

        lines = [f"実装担当: {outcome.implementer or '(不明)'}", ""]

        lost = tuple(getattr(outcome, "lost_paths", ()) or ())
        if lost:
            lines += [
                "⚠️ Gitで復元できないファイルが削除されました:",
                *(f"  {path}" for path in lost),
                "",
            ]

        changed = tuple(outcome.changed_files or ())
        if changed:
            lines.append(f"■ 変更ファイル ({len(changed)}件)")
            lines.extend(f"  {path}" for path in changed)
        else:
            lines.append("■ 変更ファイル: なし（AIはファイルを変更しませんでした）")
        lines.append("")

        if outcome.diff_stat:
            lines += ["■ 統計", outcome.diff_stat, ""]

        if outcome.test_command:
            if outcome.test_passed is True:
                verdict = "成功"
            elif outcome.test_passed is False:
                verdict = "失敗"
            else:
                verdict = "未完了"
            lines += [f"■ テスト: {verdict}  (`{outcome.test_command}`)"]
            if outcome.test_output:
                lines += ["", outcome.test_output, ""]
            else:
                lines.append("")

        if outcome.revert_hint:
            lines += ["■ 取り消す場合", outcome.revert_hint, ""]

        if outcome.notes:
            lines += ["■ 注意"] + [f"  - {note}" for note in outcome.notes] + [""]

        if outcome.diff_text:
            lines += ["■ 差分", outcome.diff_text]
        elif changed:
            lines.append("（差分本文は取得できませんでした。セッション成果物を参照してください）")

        return "\n".join(lines)

    def _render_text(self, text: str) -> None:
        self.content_box.configure(state="normal")
        self.content_box.delete("1.0", "end")
        self.content_box.insert("1.0", text)
        self.content_box.configure(state="disabled")


class ThinkingCard(ctk.CTkFrame if ctk else object):
    """Temporary card shown while brainstorming is running."""

    def __init__(self, master: Any, **kwargs: Any) -> None:
        super().__init__(
            master,
            fg_color=("gray95", "gray15"),
            corner_radius=12,
            border_width=1,
            border_color=("#3b8ed0", "#1f538d"),
            **kwargs,
        )
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="⚡️ 3社に同時ブレスト中...",
            font=fonts.bold(13),
            text_color=("#1f538d", "#4da3ff"),
        ).grid(row=0, column=0, sticky="w")

        self.status_label = ctk.CTkLabel(
            self,
            text="Claude, Antigravity, Codex に並列問い合わせ中...",
            font=fonts.font(12),
            text_color="gray60",
            anchor="w",
        )
        self.status_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))

    def update_status(self, text: str) -> None:
        try:
            self.status_label.configure(text=text)
        except Exception:
            pass
