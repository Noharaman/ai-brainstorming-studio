from __future__ import annotations

import webbrowser
from tkinter import messagebox
from typing import Callable

from src.services import cli_status
from src.services.cli_setup_guidance import (
    SetupGuidance,
    for_status as setup_guidance_for_status,
)

try:
    import customtkinter as ctk
except ImportError:
    ctk = None


_SUCCESS_STATUSES = {"ok", "ready"}
_NEUTRAL_STATUSES = {"installed", "installed_unverified", "checking"}
_WARNING_STATUSES = {
    "rate_limited",
    "timeout",
    "no_clean_exit",
    "empty_response",
    "no_expected_reply",
}
INITIAL_GLOBAL_STATUS_TEXT = "🔵 システム状態を確認中..."


def status_code(status: any) -> str:
    code = getattr(status, "status", "")
    if code:
        return code
    return "ok" if getattr(status, "available", False) else "unknown"


def status_visual(status: any) -> tuple[str, str, str, str]:
    """Return icon, background, hover, and text colors for one status."""
    code = status_code(status)
    if code in _SUCCESS_STATUSES:
        return "🟢", "#064E3B", "#065F46", "#A7F3D0"
    if code == "checking":
        return "🔵", "#1E3A8A", "#1E40AF", "#93C5FD"
    if code == "slot_disabled":
        # Grey, not red. A closed slot is a decision this build made, not a
        # fault on the user's machine — showing it as an error sends people
        # off re-authenticating a CLI that was never going to run.
        return "⏸", "#374151", "#4B5563", "#D1D5DB"
    if code in _NEUTRAL_STATUSES or code in _WARNING_STATUSES:
        return "🟡", "#78350F", "#92400E", "#FDE68A"
    return "🔴", "#7F1D1D", "#991B1B", "#FCA5A5"


def setup_guidance_items(statuses: list[any]) -> list[SetupGuidance]:
    """Return actionable setup rows in the same order as the header lamps."""
    items: list[SetupGuidance] = []
    status_by_name = {status.name: status for status in statuses}
    for tool_name in ("claude", "Antigravity(agy)", "codex"):
        tool_status = status_by_name.get(tool_name)
        if tool_status is None:
            continue
        setup = setup_guidance_for_status(tool_name, status_code(tool_status))
        if setup:
            items.append(setup)
    return items


class HeaderStatusBar(ctk.CTkFrame if ctk else object):
    def __init__(
        self,
        master: any,
        on_refresh_callback: Callable[[], None] | None = None,
        on_connection_test_callback: Callable[[], None] | None = None,
        **kwargs,
    ) -> None:
        if ctk is None:
            raise RuntimeError("customtkinter is required for HeaderStatusBar")

        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_refresh_callback = on_refresh_callback
        self.on_connection_test_callback = on_connection_test_callback
        self.current_statuses: list[any] = []
        self.is_running = False
        self.last_health_error = ""

        self._build_ui()

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)

        # 1. Global System Status Badge (Left) - Clickable for details
        self.global_badge = ctk.CTkButton(
            self,
            text=INITIAL_GLOBAL_STATUS_TEXT,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#1E3A8A",
            hover_color="#1E40AF",
            text_color="#93C5FD",
            corner_radius=16,
            height=32,
            command=self._show_details_dialog,
        )
        self.global_badge.grid(row=0, column=0, padx=(0, 12), sticky="w")

        # 2. Individual Agent Badges Container (Right)
        self.badges_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.badges_frame.grid(row=0, column=1, sticky="e")

        self.agent_buttons: dict[str, ctk.CTkButton] = {}
        agents_info = [
            ("LM Studio", "🤖 LM Studio"),
            ("claude", "🧠 Claude"),
            ("Antigravity(agy)", "⚡️ Antigravity"),
            ("codex", "💻 Codex"),
        ]

        for idx, (key, label) in enumerate(agents_info):
            btn = ctk.CTkButton(
                self.badges_frame,
                text=f"{label}: ⚪️",
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color=("gray80", "gray20"),
                hover_color=("gray70", "gray30"),
                text_color=("gray10", "gray90"),
                corner_radius=12,
                height=26,
                width=110,
                command=self._show_details_dialog,
            )
            btn.grid(row=0, column=idx, padx=3)
            self.agent_buttons[key] = btn

    def update_statuses(
        self,
        statuses: list[any],
        is_running: bool = False,
    ) -> None:
        self.current_statuses = statuses
        self.is_running = is_running

        status_dict = {s.name: s for s in statuses}
        lm_status = status_dict.get("LM Studio")
        claude_status = status_dict.get("claude")
        agy_status = status_dict.get("Antigravity(agy)")
        codex_status = status_dict.get("codex")

        # Keep the individual lamps current even while the global badge shows
        # that a brainstorm is running.
        self._update_agent_btn("LM Studio", "🤖 LM Studio", lm_status)
        self._update_agent_btn("claude", "🧠 Claude", claude_status)
        self._update_agent_btn("Antigravity(agy)", "⚡️ Antigravity", agy_status)
        self._update_agent_btn("codex", "💻 Codex", codex_status)

        if is_running:
            self.global_badge.configure(
                text="🔵 3社に同時ブレスト中...",
                fg_color="#1E3A8A",
                text_color="#93C5FD",
            )
            return

        # Evaluate Overall Health
        has_critical = lm_status is None or status_code(lm_status) not in _SUCCESS_STATUSES
        agents = (claude_status, agy_status, codex_status)
        has_warning = any(s is None or status_code(s) not in _SUCCESS_STATUSES for s in agents)
        all_ok = not has_critical and not has_warning

        if has_critical:
            self.global_badge.configure(
                text="🔴 障害・LM Studio停止",
                fg_color="#7F1D1D",
                hover_color="#991B1B",
                text_color="#FCA5A5",
            )
        elif has_warning:
            self.global_badge.configure(
                text="🟡 警告・一部機能制限あり",
                fg_color="#78350F",
                hover_color="#92400E",
                text_color="#FDE68A",
            )
        elif all_ok:
            self.global_badge.configure(
                text="🟢 システム正常・準備完了",
                fg_color="#065F46",
                hover_color="#047857",
                text_color="#A7F3D0",
            )

    def _update_agent_btn(self, key: str, label: str, status: any) -> None:
        btn = self.agent_buttons.get(key)
        if not btn:
            return
        if not status:
            btn.configure(text=f"{label}: ⚪️", fg_color=("gray80", "gray20"))
            return

        icon, fg_color, hover_color, text_color = status_visual(status)
        btn.configure(
            text=f"{label}: {icon}",
            fg_color=fg_color,
            hover_color=hover_color,
            text_color=text_color,
        )

    def show_health_error(self, message: str) -> None:
        self.last_health_error = message
        self.global_badge.configure(
            text="🔴 ヘルスチェック失敗",
            fg_color="#7F1D1D",
            hover_color="#991B1B",
            text_color="#FCA5A5",
        )

    def _show_details_dialog(self) -> None:
        setup_items = setup_guidance_items(self.current_statuses)

        dialog = ctk.CTkToplevel(self)
        dialog.title("🔍 AIエージェント＆システム詳細診断")
        dialog.geometry("760x700" if len(setup_items) > 1 else "720x540")
        dialog.transient(self.winfo_toplevel())
        dialog.grab_set()

        ctk.CTkLabel(
            dialog,
            text="🔍 各AIエージェント & システム詳細ステータス",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(padx=16, pady=(16, 8), anchor="w")

        textbox = ctk.CTkTextbox(dialog, font=ctk.CTkFont(family="Consolas", size=12))
        textbox.pack(fill="both", expand=True, padx=16, pady=8)

        # Format Detail Status Report
        report_lines = []
        report_lines.append("==========================================")
        report_lines.append(" 🤖 AI Brainstorming Studio - システム診断")
        report_lines.append("==========================================")
        report_lines.append("")

        report_lines.append("【各コンポーネント詳細】")
        for s in self.current_statuses:
            code = status_code(s)
            icon, _fg, _hover, _text = status_visual(s)
            report_lines.append(f"• {s.name:<18}: {icon} {cli_status.label_for(code)}")
            report_lines.append(f"  判定元            : {getattr(s, 'source', 'unknown')}")
            report_lines.append(f"  最終確認          : {getattr(s, 'checked_at', '-')}")
            if getattr(s, "executable_path", ""):
                report_lines.append(f"  実行ファイル      : {s.executable_path}")
            report_lines.append(f"  詳細              : {s.detail}")
            guidance = cli_status.guidance_for(code)
            if code not in _SUCCESS_STATUSES and guidance and guidance != s.detail:
                report_lines.append(f"  対応              : {guidance}")
            report_lines.append("")

        if self.last_health_error:
            report_lines.append("------------------------------------------")
            report_lines.append(f"ヘルスチェックエラー: {self.last_health_error}")

        report_lines.append("------------------------------------------")
        report_lines.append("通常の再チェックはインストール状態だけを確認します。")
        report_lines.append("AI接続テストは各CLIへ短いプロンプトを送り、契約枠を少量使用する可能性があります。")

        textbox.insert("1.0", "\n".join(report_lines))
        textbox.configure(state="disabled")

        for setup in setup_items:
            setup_section = ctk.CTkFrame(dialog, fg_color="transparent")
            setup_section.pack(fill="x", padx=16, pady=(0, 8))
            ctk.CTkLabel(
                setup_section,
                text=f"{setup.title}: {setup.note}",
                wraplength=720,
                justify="left",
                text_color="gray70",
            ).pack(fill="x", pady=(0, 4), anchor="w")

            setup_buttons = ctk.CTkFrame(setup_section, fg_color="transparent")
            setup_buttons.pack(fill="x")
            if setup.command:
                ctk.CTkButton(
                    setup_buttons,
                    text=setup.command_button_label,
                    command=lambda command=setup.command: self._copy_command(command),
                ).pack(side="left", padx=(0, 6))
            ctk.CTkButton(
                setup_buttons,
                text=setup.url_button_label,
                command=lambda url=setup.official_url: webbrowser.open(url),
            ).pack(side="left", padx=6)

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(fill="x", padx=16, pady=(0, 16))

        if self.on_refresh_callback:
            ctk.CTkButton(
                btn_frame,
                text="🔄 ステータス再チェック",
                command=lambda: [dialog.destroy(), self.on_refresh_callback()],
            ).pack(side="left", padx=(0, 6))

        if self.on_connection_test_callback:
            ctk.CTkButton(
                btn_frame,
                text="🧪 AI接続テスト",
                command=lambda: [dialog.destroy(), self.on_connection_test_callback()],
            ).pack(side="left", padx=6)

        ctk.CTkButton(
            btn_frame,
            text="閉じる",
            command=dialog.destroy,
            fg_color="gray30",
        ).pack(side="right")

    def _copy_command(self, command: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(command)
        self.update()
        messagebox.showinfo(
            "コマンドをコピーしました",
            "ターミナルへ貼り付ける前に内容を確認してください。\n\n" + command,
        )
