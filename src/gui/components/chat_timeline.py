from __future__ import annotations

from typing import Any
from pathlib import Path

from src.gui.components.chat_message_card import (
    AssistantTurnCard,
    ThinkingCard,
    UserMessageCard,
)
from src.gui.components import fonts
from src.services.implementation_plan import outcome_from_dict
from src.services.question_manager import QuestionManager

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None


class ChatTimeline(ctk.CTkScrollableFrame if ctk else object):
    """Scrollable chat timeline displaying the message history as cards."""

    def __init__(self, master: Any, **kwargs: Any) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.grid_columnconfigure(0, weight=1)
        self.cards: list[Any] = []
        self.thinking_card: ThinkingCard | None = None
        self.empty_label: Any = None
        self._show_empty_placeholder()

    def _show_empty_placeholder(self) -> None:
        if self.empty_label is None:
            self.empty_label = ctk.CTkLabel(
                self,
                text="💬 チャット履歴はまだありません。\n下の入力欄からブレスト依頼を送信してください。",
                font=fonts.font(14),
                text_color="gray50",
                justify="center",
            )
            self.empty_label.grid(row=0, column=0, pady=80, sticky="nsew")

    def _hide_empty_placeholder(self) -> None:
        if self.empty_label is not None:
            try:
                self.empty_label.destroy()
            except Exception:
                pass
            self.empty_label = None

    def clear(self) -> None:
        self.hide_thinking()
        for card in self.cards:
            try:
                card.destroy()
            except Exception:
                pass
        self.cards.clear()
        self._show_empty_placeholder()

    def add_user_message(self, content: str, created_at: str = "") -> UserMessageCard:
        self._hide_empty_placeholder()
        row_idx = len(self.cards)
        card = UserMessageCard(self, content=content, created_at=created_at)
        card.grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(6, 6))
        self.cards.append(card)
        self.scroll_to_bottom()
        return card

    def add_assistant_turn(
        self,
        summary_content: str,
        agent_outputs: dict[str, str] | None = None,
        created_at: str = "",
        session_id: str = "",
        questions: list[str] | None = None,
        implementation: object | None = None,
    ) -> AssistantTurnCard:
        self.hide_thinking()
        self._hide_empty_placeholder()
        row_idx = len(self.cards)
        card = AssistantTurnCard(
            self,
            summary_content=summary_content,
            agent_outputs=agent_outputs,
            created_at=created_at,
            session_id=session_id,
            questions=questions,
            implementation=implementation,
        )
        card.grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(6, 12))
        self.cards.append(card)
        self.scroll_to_bottom()
        return card

    def show_thinking(self, text: str = "") -> None:
        self._hide_empty_placeholder()
        if self.thinking_card is None:
            row_idx = len(self.cards)
            self.thinking_card = ThinkingCard(self)
            self.thinking_card.grid(row=row_idx, column=0, sticky="ew", padx=8, pady=(6, 12))
        if text:
            self.thinking_card.update_status(text)
        self.scroll_to_bottom()

    def update_thinking(self, text: str) -> None:
        if self.thinking_card is not None:
            self.thinking_card.update_status(text)

    def hide_thinking(self) -> None:
        if self.thinking_card is not None:
            try:
                self.thinking_card.destroy()
            except Exception:
                pass
            self.thinking_card = None

    def load_messages(self, messages: list[dict]) -> None:
        self.clear()
        if not messages:
            return

        self._hide_empty_placeholder()
        for msg in messages:
            role = msg.get("role", "")
            content = str(msg.get("content", "")).strip()
            created_at = str(msg.get("created_at", ""))
            session_id = str(msg.get("session_id", ""))

            if role == "user":
                self.add_user_message(content=content, created_at=created_at)
            elif role == "assistant":
                agent_outputs = msg.get("agent_outputs")
                # Questions are not persisted with the turn, so recover them
                # from the saved answer; otherwise a reopened room would drop
                # the 確認事項 panel that was there before the restart.
                self.add_assistant_turn(
                    summary_content=content,
                    agent_outputs=agent_outputs,
                    created_at=created_at,
                    session_id=session_id,
                    questions=QuestionManager().extract_questions(content),
                    implementation=outcome_from_dict(msg.get("implementation")),
                )

        self.scroll_to_bottom()

    def scroll_to_bottom(self) -> None:
        self.after(50, self._do_scroll)

    def _do_scroll(self) -> None:
        try:
            if hasattr(self, "_parent_canvas"):
                self._parent_canvas.yview_moveto(1.0)
        except Exception:
            pass
