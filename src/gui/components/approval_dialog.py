"""The gate a human passes before an AI is allowed to edit their files.

Deliberately modal and deliberately negative by default: closing the window,
pressing Escape, or quitting the app all resolve to "not approved". The only
path to approval is the explicit button.
"""

from __future__ import annotations

from typing import Any

from src.gui.components import fonts

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover
    ctk = None


class ApprovalDialog(ctk.CTkToplevel if ctk else object):
    """Shows the plan and returns the user's decision through `on_decision`.

    `on_decision(approved: bool, feedback: str)` is called exactly once, no
    matter how the window goes away.
    """

    def __init__(self, master: Any, request: Any, on_decision: Any, **kwargs: Any) -> None:
        super().__init__(master, **kwargs)
        self.request = request
        self._on_decision = on_decision
        self._answered = False

        self.title("実装の承認")
        self.geometry("760x680")
        self.transient(master)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        self._build_header()
        self._build_warnings()
        self._build_plan()
        self._build_feedback()
        self._build_buttons()

        # Every exit route is a rejection unless the approve button ran first.
        self.protocol("WM_DELETE_WINDOW", self._reject)
        self.bind("<Escape>", lambda _event: self._reject())

        self.after(50, self._grab)

    # ------------------------------------------------------------------ build

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text="⚠️ AIにファイルの編集を許可しますか？",
            font=fonts.bold(16),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew")

        ctk.CTkLabel(
            header,
            text=(
                f"実装担当: {self.request.implementer}\n"
                f"対象フォルダ: {self.request.project_root}"
            ),
            font=fonts.font(12),
            text_color="gray60",
            anchor="w",
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 0))

    def _build_warnings(self) -> None:
        warnings = list(self.request.warnings())
        checkpoint = self.request.checkpoint
        if checkpoint.can_diff:
            warnings.append(
                f"承認時点のコミット: {checkpoint.commit[:12]} "
                f"({checkpoint.branch or 'detached'})"
            )
        if not warnings:
            return

        box = ctk.CTkFrame(self, fg_color=("#fff4e0", "#3a2f1c"), corner_radius=8)
        box.grid(row=1, column=0, sticky="ew", padx=16, pady=(8, 4))
        box.grid_columnconfigure(0, weight=1)
        for index, warning in enumerate(warnings):
            ctk.CTkLabel(
                box,
                text=f"• {warning}",
                font=fonts.font(12),
                anchor="w",
                justify="left",
                wraplength=680,
            ).grid(row=index, column=0, sticky="ew", padx=10, pady=(8 if index == 0 else 2, 6))

    def _build_plan(self) -> None:
        ctk.CTkLabel(
            self,
            text="実装プラン",
            font=fonts.bold(13),
            anchor="w",
        ).grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 2))

        plan_box = ctk.CTkTextbox(self, wrap="word", font=fonts.font(12))
        plan_box.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 8))
        plan = self.request.plan
        # Show the raw text whenever the tidy rendering would hide anything:
        # an unparsed plan, or one whose leading section landed under a
        # heading this app does not recognise. The user must be able to read
        # everything they are approving, even when it is less pretty.
        readable = plan.is_parsed and plan.is_complete
        plan_box.insert("1.0", plan.render() if readable else plan.raw_text)
        plan_box.configure(state="disabled")

    def _build_feedback(self) -> None:
        ctk.CTkLabel(
            self,
            text="補足指示（任意・却下する場合の理由にもなります）",
            font=fonts.font(12),
            text_color="gray60",
            anchor="w",
        ).grid(row=4, column=0, sticky="ew", padx=16)

        self.feedback_box = ctk.CTkTextbox(self, height=70, wrap="word")
        self.feedback_box.grid(row=5, column=0, sticky="ew", padx=16, pady=(2, 8))

    def _build_buttons(self) -> None:
        buttons = ctk.CTkFrame(self, fg_color="transparent")
        buttons.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 14))
        buttons.grid_columnconfigure(0, weight=1)

        ctk.CTkButton(
            buttons,
            text="承認しない",
            width=140,
            fg_color="gray40",
            hover_color="gray30",
            command=self._reject,
        ).grid(row=0, column=1, padx=(0, 8))

        ctk.CTkButton(
            buttons,
            text="承認して実装させる",
            width=200,
            fg_color="#c9761a",
            hover_color="#a85f10",
            command=self._approve,
        ).grid(row=0, column=2)

    # ----------------------------------------------------------------- answer

    def _grab(self) -> None:
        try:
            self.grab_set()
            self.lift()
            self.focus_force()
        except Exception:
            # A window manager that refuses the grab must not strand the run:
            # the dialog still works, it just isn't modal.
            pass

    def _feedback(self) -> str:
        try:
            return self.feedback_box.get("1.0", "end").strip()
        except Exception:
            return ""

    def _approve(self) -> None:
        self._answer(True)

    def _reject(self) -> None:
        self._answer(False)

    def _answer(self, approved: bool) -> None:
        if self._answered:
            return
        self._answered = True
        feedback = self._feedback()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self._on_decision(approved, feedback)
