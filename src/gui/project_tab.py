from __future__ import annotations

import threading
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog

from src.gui.components import fonts
from src.gui.components.approval_dialog import ApprovalDialog
from src.gui.components.chat_timeline import ChatTimeline
from src.gui.components.markdown_editor import MarkdownEditor
from src.gui.components.model_import_dialog import ModelImportDialog
from src.models import BrainstormResult, CommandResult
from src import config
from src.services.autonomy_controller import (
    AUTOMATION_LEVELS,
    DEFAULT_LEVEL,
    capabilities_for,
    label_to_level,
    level_to_label,
)
from src.services.chat_room_manager import ChatRoomManager
from src.services.implementation_plan import (
    ApprovalDecision,
    ApprovalRequest,
    outcome_to_dict,
)
from src.services.refinement_loop import RefinementLoop
from src.services.health_checker import ToolStatus
from src.services.run_registry import RunContext
from src.services.run_state import (
    STATE_LABELS,
    InvalidRunStateTransition,
    RunState,
    RunStateMachine,
)
from src.services.safety_guard import SafetyGuard
from src.services import agent_model_selector
from src.services.agent_model_selector import all_models_for, load_catalog

try:
    import customtkinter as ctk
except ImportError:  # pragma: no cover - local environment dependent
    ctk = None


AGENT_VIEWS = (
    ("claude", "🧠 Claude"),
    ("gemini", "⚡️ Antigravity"),
    ("codex", "💻 Codex"),
)
DEFAULT_AUTOMATION_LABEL = level_to_label(DEFAULT_LEVEL)


@dataclass(frozen=True, eq=False)
class ApprovalGate:
    """One run's wait at the approval gate.

    `eq=False` keeps identity comparison: two gates for the same run must
    still be distinguishable, and `_resolve_approval` compares with `is`.
    """

    run_id: str
    event: threading.Event = field(default_factory=threading.Event)
    answer: dict = field(default_factory=dict)


MAX_LOG_CHARS = 400_000


#: Display names for the AI CLI slots, in header order.
_SLOT_LABELS = (("claude", "Claude"), ("gemini", "Antigravity"), ("codex", "Codex"))


def enabled_cli_slots() -> list[str]:
    """Display names of the AI CLI slots this build will actually launch."""
    return [
        label for agent, label in _SLOT_LABELS if config.is_agent_slot_enabled(agent)
    ]


def run_button_label() -> str:
    """What the run button says.

    Derived from the slots rather than hardcoded: the button read
    "3社に同時ブレスト依頼" for a build that launches no external CLI at all,
    which is the same kind of claim the automation-level menu used to make.
    """
    slots = enabled_cli_slots()
    if not slots:
        return "✨ 議長AI (LM Studio) に依頼"
    if len(slots) == 1:
        return f"✨ {slots[0]} に依頼"
    return f"✨ {len(slots)}社に同時ブレスト依頼"


def consultation_hint() -> str:
    slots = enabled_cli_slots()
    send_key = "(Cmd+Enter で送信)"
    if not slots:
        return f"💡 ローカルの議長AI (LM Studio) が回答します {send_key}"
    joined = ", ".join(slots)
    return f"💡 {joined} の意見を議長AIが統合します {send_key}"


def integrating_message() -> str:
    slots = enabled_cli_slots()
    if not slots:
        return "👑 議長AI (LM Studio) が回答を作成中..."
    return f"👑 議長AI (LM Studio) が{len(slots)}社の意見を比較・統合中..."

# Display-side sentinels. agent_model_selector.CHAIR_AUTO_SELECT is the value
# that actually flows through RunContext/AgentSelection; this label is the
# GUI-only text shown in the dropdown, translated at the collect_user_selections()
# boundary and never seen below the GUI layer.
CHAIR_AUTO_LABEL = "👑 議長AIにお任せ"
EFFORT_UNSET_LABEL = "既定（指定なし）"


class ProjectTab:
    """One project folder's workspace: its own state, widgets, and worker thread.

    Tabs are fully independent, so several folders can run at the same time.
    All cross-thread updates go through the shared app queue tagged with `tab_id`.
    """

    def __init__(
        self,
        master: any,
        app: any = None,
        tab_id: str = "",
        project_path: str = "",
        automation_label: str = "",
        room_id: str = "",
        app_queue: queue.Queue | None = None,
    ) -> None:
        if ctk is None:
            raise RuntimeError("customtkinter is required for ProjectTab")

        self.master = master
        self.app = app
        self.tab_id = tab_id
        self.app_queue = app_queue or getattr(app, "queue", None)
        self.project_path = ""
        self.current_room_id = room_id
        self.room_label_to_id: dict[str, str] = {}
        self.recent_label_to_path: dict[str, str] = {}
        self.run_state_machine: RunStateMachine | None = None
        self.cancel_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.active_run: RunContext | None = None
        #: The gate a worker is currently waiting on, or None. Answered by
        #: identity (see _resolve_approval), never by "whatever is installed".
        self._approval_gate: ApprovalGate | None = None
        self._approval_dialog = None
        # Round-trip through the level so a tab saved by the five-level build
        # restores to the level it meant, rather than silently resetting to the
        # default. label_to_level() knows the retired labels.
        self.automation_level = ctk.StringVar(
            value=level_to_label(label_to_level(automation_label))
            if automation_label
            else DEFAULT_AUTOMATION_LABEL
        )
        self.room_selection = ctk.StringVar(value="履歴なし")
        self.model_selections: dict[str, ctk.StringVar] = {
            "claude": ctk.StringVar(value="CLI既定モデル"),
            "gemini": ctk.StringVar(value="CLI既定モデル"),
            "codex": ctk.StringVar(value="CLI既定モデル"),
        }
        self.effort_selections: dict[str, ctk.StringVar] = {
            "claude": ctk.StringVar(value=EFFORT_UNSET_LABEL),
            "gemini": ctk.StringVar(value=EFFORT_UNSET_LABEL),
            "codex": ctk.StringVar(value=EFFORT_UNSET_LABEL),
        }
        self.effort_menus: dict[str, ctk.CTkOptionMenu] = {}
        self.effort_rows: dict[str, ctk.CTkFrame] = {}
        self.agent_outputs: dict[str, ctk.CTkTextbox] = {}

        self.frame = ctk.CTkFrame(master, fg_color="transparent")
        self.frame.grid_rowconfigure(0, weight=1)
        self.frame.grid_columnconfigure(0, weight=1)

        self._build_start_view()
        self._build_work_view()
        self.start_view.tkraise()

        if project_path:
            self.open_project(project_path, remember_room=bool(room_id))

    # ------------------------------------------------------------------ views

    def _build_start_view(self) -> None:
        self.start_view = ctk.CTkFrame(self.frame)
        self.start_view.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)
        self.start_view.grid_columnconfigure(0, weight=1)
        self.start_view.grid_rowconfigure(0, weight=1)
        self.start_view.grid_rowconfigure(4, weight=2)

        ctk.CTkLabel(
            self.start_view,
            text="🗂  作業するプロジェクトフォルダを選んでください",
            font=fonts.bold(18),
        ).grid(row=1, column=0, pady=(0, 4))
        ctk.CTkLabel(
            self.start_view,
            text="選択した時点ではフォルダに書き込みません。",
            text_color="gray60",
        ).grid(row=2, column=0, pady=(0, 16))

        ctk.CTkButton(
            self.start_view,
            text="📁 フォルダダイアログから選択",
            width=300,
            height=40,
            command=self._pick_project_dialog,
        ).grid(row=3, column=0)

        recent_box = ctk.CTkFrame(self.start_view, fg_color="transparent")
        recent_box.grid(row=4, column=0, sticky="n", pady=(20, 0))
        ctk.CTkLabel(recent_box, text="最近使ったプロジェクト", text_color="gray60").pack(pady=(0, 6))
        self.recent_buttons_frame = ctk.CTkFrame(recent_box, fg_color="transparent")
        self.recent_buttons_frame.pack()
        self.refresh_recent_buttons()

    def refresh_recent_buttons(self) -> None:
        for child in self.recent_buttons_frame.winfo_children():
            child.destroy()
        recents = self.app.recent_manager.get_recent_projects()
        if not recents:
            ctk.CTkLabel(self.recent_buttons_frame, text="(履歴なし)", text_color="gray50").pack()
            return
        for path in recents[:6]:
            ctk.CTkButton(
                self.recent_buttons_frame,
                text=f"📁 {Path(path).name}",
                width=300,
                height=30,
                anchor="w",
                fg_color=("gray80", "gray22"),
                hover_color=("gray70", "gray30"),
                command=lambda p=path: self.open_project(p),
            ).pack(pady=2)

    def _build_work_view(self) -> None:
        self.work_view = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.work_view.grid(row=0, column=0, sticky="nsew")
        self.work_view.grid_columnconfigure(0, weight=0, minsize=290)
        self.work_view.grid_columnconfigure(1, weight=1)
        self.work_view.grid_rowconfigure(0, weight=1)

        self._build_left_panel()
        self._build_right_panel()

    def _build_left_panel(self) -> None:
        left = ctk.CTkFrame(self.work_view, width=290)
        left.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=(6, 12))
        left.grid_columnconfigure(0, weight=1)
        left.grid_rowconfigure(7, weight=1)

        # 1. Project Path Header
        header = ctk.CTkFrame(left, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 2))
        header.grid_columnconfigure(0, weight=1)
        self.path_label = ctk.CTkLabel(
            header, text="", wraplength=210, anchor="w", justify="left", font=fonts.font(12)
        )
        self.path_label.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            header, text="変更", width=48, height=24, command=self._pick_project_dialog
        ).grid(row=0, column=1, padx=(6, 0))

        # 2. Chat Room selector & buttons
        ctk.CTkLabel(left, text="チャットルーム", font=fonts.bold(12)).grid(
            row=1, column=0, sticky="w", padx=12, pady=(8, 0)
        )
        self.room_menu = ctk.CTkOptionMenu(
            left, values=["履歴なし"], variable=self.room_selection, command=self._select_room
        )
        self.room_menu.grid(row=2, column=0, sticky="ew", padx=12, pady=(4, 6))

        room_buttons = ctk.CTkFrame(left, fg_color="transparent")
        room_buttons.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 8))
        for column in range(3):
            room_buttons.grid_columnconfigure(column, weight=1)
        ctk.CTkButton(room_buttons, text="新規", height=24, command=self._new_room).grid(
            row=0, column=0, sticky="ew", padx=(0, 3)
        )
        ctk.CTkButton(room_buttons, text="名前変更", height=24, command=self._rename_room).grid(
            row=0, column=1, sticky="ew", padx=3
        )
        ctk.CTkButton(room_buttons, text="削除", height=24, command=self._delete_room).grid(
            row=0, column=2, sticky="ew", padx=(3, 0)
        )

        # 3. Automation Level
        ctk.CTkLabel(left, text="自動化レベル", font=fonts.bold(12)).grid(
            row=4, column=0, sticky="w", padx=12
        )
        # Menu and description share one cell, so adding the description does
        # not renumber every row below it.
        automation_frame = ctk.CTkFrame(left, fg_color="transparent")
        automation_frame.grid(row=5, column=0, sticky="ew", padx=12, pady=(4, 8))
        automation_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkOptionMenu(
            automation_frame,
            values=list(AUTOMATION_LEVELS),
            variable=self.automation_level,
            command=self._on_automation_level_changed,
        ).grid(row=0, column=0, sticky="ew")

        # Spells out what the selected level actually does. The five-level menu
        # this replaced offered "実装・テストまで" while running read-only, so
        # the description is generated from the same capabilities the run reads.
        self.automation_hint = ctk.CTkLabel(
            automation_frame,
            text="",
            font=fonts.font(11),
            text_color="gray60",
            anchor="w",
            justify="left",
            wraplength=230,
        )
        self.automation_hint.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._on_automation_level_changed(self.automation_level.get())

        # 4. Model Selection
        catalog = load_catalog()
        model_frame = ctk.CTkFrame(left, fg_color="transparent")
        model_frame.grid(row=6, column=0, sticky="ew", padx=12, pady=(0, 8))
        model_frame.grid_columnconfigure(1, weight=1)

        header_sub = ctk.CTkFrame(model_frame, fg_color="transparent")
        header_sub.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        header_sub.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header_sub, text="モデル選択", font=fonts.bold(12)).grid(
            row=0, column=0, sticky="w"
        )
        btn_box = ctk.CTkFrame(header_sub, fg_color="transparent")
        btn_box.grid(row=0, column=1, sticky="e")
        ctk.CTkButton(
            btn_box,
            text="📋 取込",
            width=52,
            height=20,
            command=self._on_click_import_models,
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            btn_box,
            text="更新",
            width=42,
            height=20,
            command=self._on_click_refresh_models,
        ).pack(side="left")

        # Shared across all three agents' effort rows rather than one
        # ctk.CTkFont(...) per widget: each CTkFont wraps a real Tk font
        # object, and unnecessary churn of those increases the odds of a
        # GC-triggered Font.__del__() (which calls into Tcl) landing on a
        # background worker thread mid-call — Tcl calls from a non-main
        # thread can hang. Six fewer Font objects per tab is a real, if
        # partial, mitigation for a hazard that predates this feature.
        effort_font = fonts.font(10)

        self.model_menus: dict[str, ctk.CTkOptionMenu] = {}
        if not enabled_cli_slots():
            # Every slot is closed, so a model picker would be three dropdowns
            # that change nothing. Say why instead.
            ctk.CTkLabel(
                model_frame,
                text=(
                    "外部AI CLIは現在すべて停止しています（安全上の判断）。\n"
                    "相談はローカルの LM Studio のみで行われます。"
                ),
                font=fonts.font(11),
                text_color="gray60",
                anchor="w",
                justify="left",
                wraplength=250,
            ).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(2, 4))

        for idx, (agent_key, label_text) in enumerate(AGENT_VIEWS):
            slot_open = config.is_agent_slot_enabled(agent_key)
            if not slot_open:
                # Keep the StringVar so collect_user_selections() and tab
                # restore stay unchanged; just do not offer a control.
                continue
            model_row = 1 + 2 * idx
            effort_row = model_row + 1
            ctk.CTkLabel(model_frame, text=label_text, font=fonts.font(11)).grid(
                row=model_row, column=0, sticky="w", padx=(0, 4)
            )
            choices = ["CLI既定モデル", CHAIR_AUTO_LABEL]
            for m in all_models_for(agent_key, catalog):
                m_id = m.get("id", "")
                b_status = m.get("billing_status", "")
                badge = ""
                if b_status in ("unknown", "usage_credits", "pay_as_you_go"):
                    badge = " [要確認]"
                choices.append(f"{m_id}{badge}")
            menu = ctk.CTkOptionMenu(
                model_frame,
                values=choices,
                variable=self.model_selections[agent_key],
                height=24,
                command=lambda selected, agent_key=agent_key: self._on_model_changed(agent_key, selected),
            )
            menu.grid(row=model_row, column=1, sticky="ew", pady=2)
            self.model_menus[agent_key] = menu

            effort_row_frame = ctk.CTkFrame(model_frame, fg_color="transparent")
            effort_row_frame.grid(row=effort_row, column=0, columnspan=2, sticky="ew")
            effort_row_frame.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(
                effort_row_frame, text="   effort", font=effort_font, text_color="gray60"
            ).grid(row=0, column=0, sticky="w", padx=(0, 4))
            effort_menu = ctk.CTkOptionMenu(
                effort_row_frame,
                values=[EFFORT_UNSET_LABEL],
                variable=self.effort_selections[agent_key],
                height=20,
                font=effort_font,
            )
            effort_menu.grid(row=0, column=1, sticky="ew", pady=(0, 2))
            self.effort_menus[agent_key] = effort_menu
            self.effort_rows[agent_key] = effort_row_frame
            effort_row_frame.grid_remove()

        catalog_for_init = catalog
        for agent_key, _label_text in AGENT_VIEWS:
            self._on_model_changed(agent_key, self.model_selections[agent_key].get(), catalog=catalog_for_init)

        # 5. Spacer & Status
        spacer = ctk.CTkFrame(left, fg_color="transparent")
        spacer.grid(row=7, column=0, sticky="nsew")

        self.status_label = ctk.CTkLabel(left, text="Ready", anchor="w", text_color="gray60")
        self.status_label.grid(row=8, column=0, sticky="ew", padx=12, pady=(0, 12))

    def _effort_levels_for(self, agent_key: str, selected_value: str, catalog: dict) -> list[str]:
        """The effort levels selectable for whichever model `selected_value`
        names, or [] if the agent doesn't support separate effort, the model
        has none, or `selected_value` isn't a real model (CLI既定モデル /
        議長AIにお任せ — neither has one fixed model to read levels from)."""
        if selected_value in ("CLI既定モデル", CHAIR_AUTO_LABEL):
            return []
        agent_catalog = catalog.get(agent_key) or {}
        if not agent_catalog.get("supports_separate_effort"):
            return []
        model_id = selected_value.split(" ")[0].strip()
        model = next((m for m in all_models_for(agent_key, catalog) if m.get("id") == model_id), None)
        return list((model or {}).get("effort_levels") or [])

    def _on_model_changed(self, agent_key: str, selected_value: str, catalog: dict | None = None) -> None:
        """Show/hide and repopulate `agent_key`'s effort dropdown to match
        whichever model is now selected, resetting the effort choice. Called
        both as the model dropdown's own `command=` callback (a genuine model
        change, where carrying over the old effort value wouldn't make sense
        even if it happens to share a name with a level of the new model) and
        once per agent at panel-build time, so the initial state doesn't rely
        on a hardcoded assumption about which agents start out effort-capable.

        Never silently defaults to a maximum/implicit effort: the row always
        resets to EFFORT_UNSET_LABEL, whether shown or hidden.
        """
        effort_row = self.effort_rows.get(agent_key)
        effort_menu = self.effort_menus.get(agent_key)
        if effort_row is None or effort_menu is None:
            return

        active_catalog = catalog if catalog is not None else load_catalog()
        effort_levels = self._effort_levels_for(agent_key, selected_value, active_catalog)
        self.effort_selections[agent_key].set(EFFORT_UNSET_LABEL)
        if not effort_levels:
            effort_row.grid_remove()
            return
        effort_menu.configure(values=[EFFORT_UNSET_LABEL] + effort_levels)
        effort_row.grid()

    def refresh_model_options(self, notify_disappeared: bool = True) -> None:
        if not hasattr(self, "model_menus"):
            return
        catalog = load_catalog()
        disappeared_agents = []
        disappeared_efforts = []
        for agent_key, menu in self.model_menus.items():
            if menu is None:
                continue
            choices = ["CLI既定モデル", CHAIR_AUTO_LABEL]
            valid_ids = set()
            for m in all_models_for(agent_key, catalog):
                m_id = m.get("id", "")
                b_status = m.get("billing_status", "")
                badge = ""
                if b_status in ("unknown", "usage_credits", "pay_as_you_go"):
                    badge = " [要確認]"
                choice_str = f"{m_id}{badge}"
                choices.append(choice_str)
                valid_ids.add(m_id)

            menu.configure(values=choices)

            curr_val = self.model_selections[agent_key].get()
            # CHAIR_AUTO_LABEL isn't a catalog id and never will be — leaving
            # it out of this check treated it as an unknown model on every
            # refresh (any startup, the manual button, or a paste-import
            # success), silently downgrading a user's 議長AIにお任せ choice
            # back to CLI既定モデル.
            if curr_val and curr_val not in ("CLI既定モデル", CHAIR_AUTO_LABEL):
                selected_id = curr_val.split(" ")[0].strip()
                if selected_id not in valid_ids:
                    self.model_selections[agent_key].set("CLI既定モデル")
                    disappeared_agents.append(f"{agent_key}: {selected_id}")
                    curr_val = "CLI既定モデル"

            # Re-derive the effort row from the (possibly refreshed) catalog
            # for a model selection that survived above. Unlike
            # _on_model_changed() (a genuine model change, always resets),
            # this preserves the current effort choice when it's still valid
            # — the model didn't change, so there's nothing to reset for.
            effort_row = self.effort_rows.get(agent_key)
            effort_menu = self.effort_menus.get(agent_key)
            if effort_row is not None and effort_menu is not None:
                effort_levels = self._effort_levels_for(agent_key, curr_val, catalog)
                if not effort_levels:
                    effort_row.grid_remove()
                    self.effort_selections[agent_key].set(EFFORT_UNSET_LABEL)
                else:
                    effort_menu.configure(values=[EFFORT_UNSET_LABEL] + effort_levels)
                    prior_effort = self.effort_selections[agent_key].get()
                    if prior_effort != EFFORT_UNSET_LABEL and prior_effort not in effort_levels:
                        self.effort_selections[agent_key].set(EFFORT_UNSET_LABEL)
                        disappeared_efforts.append(f"{agent_key}: {prior_effort}")
                    effort_row.grid()

        if notify_disappeared and (disappeared_agents or disappeared_efforts):
            lines = list(disappeared_agents) + [f"{line}（effort）" for line in disappeared_efforts]
            messagebox.showinfo(
                "モデル非表示",
                "選択されていた以下のモデル・effortが検出対象から消去されたため、既定へリセットされました:\n\n"
                + "\n".join(lines),
            )

    def _on_click_refresh_models(self) -> None:
        if self.app:
            self.app.refresh_models_async()

    def _on_click_import_models(self) -> None:
        try:
            top_level = self.frame.winfo_toplevel()
        except Exception:
            top_level = self.master
        ModelImportDialog(
            top_level,
            on_success_callback=lambda: self.refresh_model_options(notify_disappeared=False),
        )

    def _build_right_panel(self) -> None:
        right = ctk.CTkFrame(self.work_view)
        right.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=(6, 12))
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)
        right.grid_rowconfigure(2, weight=0)

        # Header: Room title + View Toggle
        header = ctk.CTkFrame(right, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        header.grid_columnconfigure(0, weight=1)

        self.chat_title_label = ctk.CTkLabel(
            header,
            text="💬 チャット",
            font=fonts.bold(14),
            anchor="w",
        )
        self.chat_title_label.grid(row=0, column=0, sticky="w")

        self.view_toggle_btn = ctk.CTkSegmentedButton(
            header,
            values=["💬 チャット", "📜 ログ"],
            command=self._on_view_toggle,
            height=26,
            font=fonts.font(11),
        )
        self.view_toggle_btn.set("💬 チャット")
        self.view_toggle_btn.grid(row=0, column=1, sticky="e")

        # Center Container (Timeline / Log View)
        self.center_container = ctk.CTkFrame(right, fg_color="transparent")
        self.center_container.grid(row=1, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.center_container.grid_columnconfigure(0, weight=1)
        self.center_container.grid_rowconfigure(0, weight=1)

        # 1. Timeline
        self.timeline = ChatTimeline(self.center_container)
        self.timeline.grid(row=0, column=0, sticky="nsew")

        # 2. Log View (not gridded initially)
        self.log_view = ctk.CTkFrame(self.center_container, fg_color="transparent")
        self.log_view.grid_columnconfigure(0, weight=1)
        self.log_view.grid_rowconfigure(0, weight=1)
        self.log_text = ctk.CTkTextbox(self.log_view, font=ctk.CTkFont(family="Consolas", size=12))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")

        # Bottom Input Area
        input_bar = ctk.CTkFrame(right, fg_color=("gray88", "gray20"), corner_radius=12)
        input_bar.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))
        input_bar.grid_columnconfigure(0, weight=1)

        self.request_text = ctk.CTkTextbox(
            input_bar,
            height=70,
            font=fonts.font(13),
            fg_color="transparent",
            wrap="word",
        )
        self.request_text.grid(row=0, column=0, sticky="nsew", padx=10, pady=(8, 4))
        self.request_text.bind("<Command-Return>", lambda e: self.submit_from_shortcut())

        btn_row = ctk.CTkFrame(input_bar, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 8))
        btn_row.grid_columnconfigure(0, weight=1)

        hint_label = ctk.CTkLabel(
            btn_row,
            text=consultation_hint(),
            font=fonts.font(11),
            text_color="gray50",
            anchor="w",
        )
        hint_label.grid(row=0, column=0, sticky="w")

        self.run_button = ctk.CTkButton(
            btn_row,
            text=run_button_label(),
            command=self.start_brainstorm,
            height=30,
            font=fonts.bold(12),
        )
        self.run_button.grid(row=0, column=1, sticky="e", padx=(0, 6))

        self.cancel_button = ctk.CTkButton(
            btn_row,
            text="キャンセル",
            command=self.cancel_brainstorm,
            state="disabled",
            width=80,
            height=30,
        )
        self.cancel_button.grid(row=0, column=2, sticky="e")

    def _on_view_toggle(self, value: str) -> None:
        if "ログ" in value:
            self.timeline.grid_remove()
            self.log_view.grid(row=0, column=0, sticky="nsew")
        else:
            self.log_view.grid_remove()
            self.timeline.grid(row=0, column=0, sticky="nsew")

    # ------------------------------------------------------------- project

    def _pick_project_dialog(self) -> None:
        path = filedialog.askdirectory()
        if not path:
            return
        self.open_project(path)

    def open_project(self, path: str, remember_room: bool = False) -> None:
        if self.project_path and str(Path(self.project_path).resolve()) != str(Path(path).resolve()):
            # Tabs are immutable once bound to a folder: a running worker thread
            # has already captured this tab's project_path by value, so mutating
            # it here would desync the tab's room/UI state from the run in flight.
            # Route the request through the app instead of changing this tab.
            self.app.open_path(path)
            return
        project = Path(path)
        if not project.is_dir():
            messagebox.showerror("Missing folder", f"フォルダが見つかりません:\n{path}")
            return
        resolved = str(project.resolve())
        existing = self.app.find_tab_by_path(resolved)
        if existing and existing is not self:
            self.app.activate_tab(existing.tab_id)
            return
        self.project_path = resolved
        self.app.recent_manager.add_project(self.project_path)
        self.path_label.configure(text=self.project_path)
        if not remember_room:
            self.current_room_id = ""
        self.append_log(f"Selected project: {self.project_path}\n")
        self._load_rooms(selected_room_id=self.current_room_id)
        for warning in SafetyGuard().project_warnings(project):
            self.append_log(f"Warning: {warning}\n")
        self.work_view.tkraise()
        self.app.on_tab_changed(self.tab_id)

    def title(self) -> str:
        if not self.project_path:
            return "新しいタブ"
        return Path(self.project_path).name or self.project_path

    def selected_project(self) -> Path | None:
        return Path(self.project_path) if self.project_path else None

    # ---------------------------------------------------------------- rooms

    def _load_rooms(self, selected_room_id: str = "", persist_active: bool = False) -> None:
        project = self.selected_project()
        if not project:
            return
        manager = ChatRoomManager(project)
        rooms = manager.list_rooms()
        if manager.load_error:
            self.append_log(f"Warning: {manager.load_error}\n")
        self.room_label_to_id = {}
        if not rooms:
            self.current_room_id = ""
            self.room_selection.set("履歴なし")
            self.room_menu.configure(values=["履歴なし"])
            self.timeline.clear()
            return

        active_id = selected_room_id or manager.active_room_id() or rooms[0]["id"]
        labels = []
        for index, room in enumerate(rooms, start=1):
            title = room.get("title") or "無題のチャット"
            label = f"{title}  ({room.get('updated_at', '')[:10]})"
            if label in self.room_label_to_id:
                label = f"{index}. {label}"
            labels.append(label)
            self.room_label_to_id[label] = room["id"]

        selected_label = next(
            (label for label, room_id in self.room_label_to_id.items() if room_id == active_id),
            labels[0],
        )
        self.current_room_id = self.room_label_to_id[selected_label]
        self.room_selection.set(selected_label)
        self.room_menu.configure(values=labels)
        # Selecting a project must stay read-only; only explicit user actions persist.
        if persist_active:
            manager.set_active_room(self.current_room_id)
        self._show_room_history()

    def _select_room(self, label: str) -> None:
        room_id = self.room_label_to_id.get(label, "")
        if not room_id:
            return
        self.current_room_id = room_id
        project = self.selected_project()
        if project:
            ChatRoomManager(project).set_active_room(room_id)
        self._show_room_history()

    def _new_room(self) -> None:
        project = self._require_project()
        if not project:
            return
        title = simpledialog.askstring("New chat", "チャット名", initialvalue="新しいチャット")
        if title is None:
            return
        room_id = ChatRoomManager(project).create_room(title)
        self._load_rooms(selected_room_id=room_id, persist_active=True)

    def _rename_room(self) -> None:
        project = self._require_project()
        if not project or not self.current_room_id:
            return
        manager = ChatRoomManager(project)
        room = manager.get_room(self.current_room_id)
        current_title = room.get("title") if room else "無題のチャット"
        title = simpledialog.askstring("Rename chat", "新しいチャット名", initialvalue=current_title)
        if title is None:
            return
        manager.rename_room(self.current_room_id, title)
        self._load_rooms(selected_room_id=self.current_room_id, persist_active=True)

    def _delete_room(self) -> None:
        project = self._require_project()
        if not project or not self.current_room_id:
            return
        if not messagebox.askyesno("Delete chat", "このチャットルームを削除しますか？"):
            return
        ChatRoomManager(project).delete_room(self.current_room_id)
        self._load_rooms(persist_active=True)
        if not self.current_room_id:
            self.timeline.clear()
            self.chat_title_label.configure(text="💬 チャット")

    def _show_room_history(self) -> None:
        project = self.selected_project()
        if not project or not self.current_room_id:
            self.timeline.clear()
            self.chat_title_label.configure(text="💬 チャット")
            return
        room = ChatRoomManager(project).get_room(self.current_room_id)
        if not room:
            self.timeline.clear()
            self.chat_title_label.configure(text="💬 チャット")
            return
        title = room.get("title", "無題のチャット")
        self.chat_title_label.configure(text=f"💬 {title}")
        messages = room.get("messages", [])
        self.timeline.load_messages(messages)

    def _require_project(self) -> Path | None:
        project = self.selected_project()
        if not project:
            messagebox.showerror("Missing project", "先にプロジェクトフォルダを選択してください。")
            return None
        return project

    # ------------------------------------------------------------ execution

    def collect_user_selections(self) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """Extracts user-selected models and efforts from GUI dropdowns without silently defaulting
        to maximum effort levels."""
        selected_model_ids: dict[str, str] = {}
        selected_efforts: dict[str, str] = {}
        unverified_models = []
        for agent_key, var in self.model_selections.items():
            val = var.get().strip()
            if val == CHAIR_AUTO_LABEL:
                # Exact-match, checked before any badge/split parsing below —
                # the sentinel must never be treated as a real model id, and
                # since its candidates are always safe_models_for()-filtered,
                # it must never trigger the unverified-model confirmation.
                selected_model_ids[agent_key] = agent_model_selector.CHAIR_AUTO_SELECT
                continue
            if val and val != "CLI既定モデル":
                model_id = val.split(" ")[0].strip()
                selected_model_ids[agent_key] = model_id
                if "[要確認]" in val:
                    unverified_models.append(f"- {agent_key}: {model_id}")
                effort_val = self.effort_selections.get(agent_key)
                effort_val = effort_val.get().strip() if effort_val else ""
                if effort_val and effort_val != EFFORT_UNSET_LABEL:
                    selected_efforts[agent_key] = effort_val
        return selected_model_ids, selected_efforts, unverified_models

    # ----------------------------------------------------------- run state

    @property
    def run_state(self) -> RunState:
        machine = self.run_state_machine
        return machine.state if machine is not None else RunState.IDLE

    @property
    def running(self) -> bool:
        """Whether this tab still owns a run slot.

        Derived from the state machine rather than stored separately, so it
        can no longer disagree with `cancelling` — a cancelling run is still
        running until its subprocesses are torn down and the slot released.
        """
        machine = self.run_state_machine
        return machine is not None and machine.is_active

    @property
    def cancelling(self) -> bool:
        return self.run_state is RunState.CANCELLING

    def apply_run_state(self, state: RunState) -> None:
        """Apply a phase reported by the worker thread. Main thread only.

        An illegal transition means the loop's phase wiring is wrong, which is
        a display bug, not a reason to kill the user's run — so it is logged
        and dropped rather than raised.
        """
        machine = self.run_state_machine
        if machine is None:
            return
        try:
            machine.transition_to(state)
        except InvalidRunStateTransition as exc:
            self.append_log(f"WARNING: run state: {exc}\n")
            return
        self._refresh_run_state_display()

    def _refresh_run_state_display(self) -> None:
        state = self.run_state
        try:
            self.status_label.configure(text=STATE_LABELS[state])
        except Exception:
            pass
        setter = getattr(self.app, "on_run_state_display_changed", None)
        if setter is not None:
            setter(self.tab_id, state)

    def start_brainstorm(self) -> None:
        if self.running:
            messagebox.showinfo("Running", "このタブのブレストはすでに実行中です。")
            return
        if getattr(self.app, "connection_test_running", False):
            messagebox.showinfo(
                "AI接続テスト中",
                "AI接続テストの実行中はブレストを開始できません。テスト完了後にお試しください。",
            )
            return
        project = self.project_path.strip()
        request = self.request_text.get("1.0", "end").strip()
        if not project:
            messagebox.showerror("Missing project", "プロジェクトフォルダを選択してください。")
            return
        if not request:
            messagebox.showerror("Missing request", "依頼内容を入力してください。")
            return

        selected_model_ids, selected_efforts, unverified_models = self.collect_user_selections()

        if unverified_models:
            msg = (
                "以下の選択モデルは料金区分が未検証、またはクレジット消費の可能性があります:\n\n"
                + "\n".join(unverified_models)
                + "\n\nこのまま指定して実行しますか？"
            )
            if not messagebox.askyesno("モデル実行確認", msg):
                return

        proj_path = Path(project).resolve()
        reservation = self.app.run_registry.try_start(proj_path, tab_id=self.tab_id)
        if reservation is None:
            messagebox.showinfo(
                "実行中",
                "同じフォルダのブレストが他のタブ（または閉じたタブの後始末中）で実行中です。"
                "完了をお待ちください。",
            )
            return

        try:
            room_id = self._ensure_room(proj_path, request)
        except Exception as exc:
            self.app.run_registry.release(reservation)
            messagebox.showerror("エラー", f"チャットルームの準備に失敗しました:\n{exc}")
            return
        if not room_id:
            self.app.run_registry.release(reservation)
            return

        catalog = load_catalog()
        cat_version = catalog.get("catalog_version", "") if isinstance(catalog, dict) else ""

        run_context = self.app.run_registry.finalize(
            reservation,
            room_id=room_id,
            request=request,
            automation_level=label_to_level(self.automation_level.get()),
            selected_models=selected_model_ids,
            selected_efforts=selected_efforts,
            catalog_version=cat_version,
        )
        if run_context is None:
            messagebox.showerror(
                "エラー",
                "実行枠の取得中に競合が発生しました。もう一度お試しください。",
            )
            return

        self._clear_outputs()
        self.active_run = run_context
        self.cancel_event = run_context.cancel_event
        self.run_state_machine = RunStateMachine(run_context.run_id, RunState.PREPARING)
        self.run_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self._refresh_run_state_display()
        self.app.on_run_state_changed(self.tab_id, True)

        # Timeline integration
        self.timeline.add_user_message(request)
        self.timeline.show_thinking("Claude, Antigravity, Codex に並列問い合わせ中...")
        self.request_text.delete("1.0", "end")

        try:
            self.worker_thread = threading.Thread(
                target=self._run_worker,
                args=(run_context,),
                daemon=True,
            )
            self.worker_thread.start()
        except Exception as exc:
            self.timeline.hide_thinking()
            self.app.run_registry.release(run_context)
            self._abort_pending_start(f"ブレストの開始に失敗しました:\n{exc}", original_request=request)

    def _abort_pending_start(self, error_message: str, original_request: str = "") -> None:
        self.active_run = None
        if self.run_state_machine is not None:
            self.run_state_machine.fail()
        self.worker_thread = None
        self.timeline.hide_thinking()
        if original_request and not self.request_text.get("1.0", "end").strip():
            self.request_text.insert("1.0", original_request)
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._refresh_run_state_display()
        self.run_state_machine = None
        self.append_log(f"ERROR: {error_message}\n")
        messagebox.showerror("起動エラー", error_message)
        self.app.on_run_state_changed(self.tab_id, False)

    def _ensure_room(self, project: Path, request: str) -> str:
        manager = ChatRoomManager(project)
        if self.current_room_id and manager.get_room(self.current_room_id):
            manager.set_active_room(self.current_room_id)
            return self.current_room_id
        room_id = manager.create_room(request[:32] or "新しいチャット")
        self._load_rooms(selected_room_id=room_id, persist_active=True)
        return room_id

    def cancel_brainstorm(self) -> None:
        if not self.request_cancel():
            return
        self.cancel_button.configure(state="disabled")
        self.timeline.update_thinking("キャンセル要求を送信しました。CLIプロセスを停止中...")
        self.append_log("Cancellation requested. Stopping running CLI processes...\n")

    def request_cancel(self) -> bool:
        if not self.running:
            return False
        self.run_state_machine.transition_to(RunState.CANCELLING)
        self.cancel_event.set()
        # A run cancelled while the approval gate is open must not leave the
        # dialog on screen inviting an approval that will be ignored.
        self._close_approval_dialog()
        self._refresh_run_state_display()
        return True

    def _close_approval_dialog(self) -> None:
        """Refuse and tear down whatever is waiting at the gate.

        Handles the case where the request is still sitting in the GUI queue
        and no dialog exists yet: dropping only the dialog would leave the
        gate installed, and a dialog built from that queued request could then
        answer a later run.
        """
        dialog = getattr(self, "_approval_dialog", None)
        if dialog is not None:
            try:
                # Routes through the dialog's own reject path, which resolves
                # the gate exactly once.
                dialog._reject()
            except Exception:
                pass
        self._approval_dialog = None
        gate = getattr(self, "_approval_gate", None)
        if gate is not None:
            self._resolve_approval(gate, False, "")

    def _discard_approval_gate(self, gate: ApprovalGate) -> None:
        """Uninstall `gate` if it is still the current one.

        Called from the worker thread when it gives up waiting. Only clears
        its own gate, so a newer run's gate is never removed by an older
        run's cleanup.
        """
        if getattr(self, "_approval_gate", None) is gate:
            self._approval_gate = None

    def owns_run(self, run_id: str) -> bool:
        return bool(self.active_run) and self.active_run.run_id == run_id

    def _run_worker(self, run_context: RunContext) -> None:
        def progress(message: str) -> None:
            self.app.queue.put(("log", self.tab_id, run_context.run_id, message + "\n"))

        def health_status(status: ToolStatus) -> None:
            self.app.queue.put(("agent_health", self.tab_id, run_context.run_id, status))

        def run_state(state: RunState) -> None:
            self.app.queue.put(("run_state", self.tab_id, run_context.run_id, state))

        def approval(request: ApprovalRequest) -> ApprovalDecision:
            """Ask the main thread, and block this worker until it answers.

            The worker must not touch Tk, so the request goes through the same
            queue as every other GUI event and the answer comes back through a
            one-shot slot guarded by an Event.
            """
            gate = ApprovalGate(run_id=run_context.run_id)
            self._approval_gate = gate
            self.app.queue.put(
                ("approval_request", self.tab_id, run_context.run_id, request)
            )
            while not gate.event.wait(timeout=0.2):
                if run_context.cancel_event.is_set():
                    # Cancelling while the dialog is open must not leave the
                    # worker parked here forever. Drop the gate too, so a
                    # dialog opened later cannot answer a run that is gone.
                    self._discard_approval_gate(gate)
                    return ApprovalDecision(approved=False, cancelled=True)
            if not gate.answer:
                return ApprovalDecision(approved=False, cancelled=True)
            return ApprovalDecision(
                approved=bool(gate.answer.get("approved")),
                feedback=str(gate.answer.get("feedback") or ""),
            )

        try:
            for warning in SafetyGuard().environment_warnings():
                progress("Warning: " + warning)
            loop = RefinementLoop(prefer_rtk=True)
            result = loop.run_sync(
                run_context.project_root,
                run_context.request,
                run_context.automation_level,
                progress,
                run_context.cancel_event,
                run_context,
                health_status,
                run_state,
                approval,
            )
            self.app.queue.put((
                "result",
                self.tab_id,
                run_context.run_id,
                (result, run_context.project_root, run_context.request, run_context.room_id),
            ))
        except Exception as exc:
            self.app.queue.put(("error", self.tab_id, run_context.run_id, str(exc)))
        finally:
            self.app.run_registry.release(run_context)

    def _on_automation_level_changed(self, label: str) -> None:
        """Describe the selected level from its capabilities, not from prose.

        Generated rather than hardcoded so a level cannot advertise a
        behaviour the run does not perform — the failure this menu had before.
        """
        caps = capabilities_for(label_to_level(label))
        parts = [f"AI相談 {caps.rounds}ラウンド"]
        if caps.builds_plan:
            parts.append("実装プラン作成")
        if caps.can_implement:
            parts.append("承認後にファイル編集")
        else:
            parts.append("ファイル編集なし（読み取りのみ）")
        if caps.runs_tests:
            parts.append(
                "テスト自動実行"
                if config.RUN_TESTS_AUTOMATICALLY
                else "テストはコマンド提示のみ（手動実行）"
            )
        text = " / ".join(parts)
        if caps.can_implement:
            text += "\n※ 実装前に承認ダイアログで確認します。"
        try:
            self.automation_hint.configure(text=text)
        except Exception:
            pass

    # ------------------------------------------------------------- approval

    def show_approval_dialog(self, request: ApprovalRequest) -> None:
        """Main thread only. Opens the gate dialog for a waiting worker."""
        gate = getattr(self, "_approval_gate", None)
        if gate is None:
            return
        # The queued request and the installed gate must be the same run: a
        # request can sit in the queue while the run it belongs to is
        # cancelled and another one starts.
        if request.run_id and request.run_id != gate.run_id:
            return
        if not self.owns_run(gate.run_id) or self.cancel_event.is_set():
            # The run this dialog belongs to is already gone, or was cancelled
            # between queueing and now; releasing the gate unapproved is the
            # safe resolution.
            self._resolve_approval(gate, False, "")
            return

        self.append_log(
            f"承認待ち: {request.implementer} による実装の可否を確認しています。\n"
        )
        try:
            self._approval_dialog = ApprovalDialog(
                self.app.root,
                request,
                on_decision=partial(self._resolve_approval, gate),
            )
        except Exception as exc:
            # A dialog that cannot be built must not grant write access, and
            # must not leave the worker blocked.
            self.append_log(f"承認ダイアログを表示できませんでした: {exc}\n")
            self._resolve_approval(gate, False, "")

    def _resolve_approval(
        self, gate: ApprovalGate, approved: bool, feedback: str
    ) -> None:
        """Answer one specific gate.

        `gate` is bound to the dialog that was opened for it, and is compared
        by identity against the gate currently installed. Without that, a
        dialog left over from a cancelled run could answer whatever gate
        happens to be waiting now — approving a *different* run's write access
        that the user was never shown. Reading `self._approval_gate` here is
        precisely the bug this signature exists to prevent.
        """
        current = getattr(self, "_approval_gate", None)
        if current is None or current is not gate:
            # Stale dialog from a superseded or cancelled run: it has no say.
            return
        if approved and not self.owns_run(gate.run_id):
            # The run moved on while the dialog was open. Never carry an
            # approval across runs; fall back to refusal.
            approved = False
        self._approval_gate = None
        self._approval_dialog = None
        gate.answer["approved"] = approved
        gate.answer["feedback"] = feedback
        self.append_log(
            "実装を承認しました。\n" if approved else "実装を承認しませんでした。\n"
        )
        gate.event.set()

    # -------------------------------------------------------------- outputs

    def display_result(
        self, result: BrainstormResult, project: Path, request: str, room_id: str
    ) -> None:
        # Extract per-agent outputs for multi-tab cards
        agent_outputs: dict[str, str] = {}
        for key, command_result in result.command_results.items():
            base = self._base_agent(key)
            out_text = command_result.stdout.strip() or command_result.stderr.strip()
            if out_text:
                if base in agent_outputs:
                    agent_outputs[base] += f"\n\n---\n\n{out_text}"
                else:
                    agent_outputs[base] = out_text

        saved_room_id = ChatRoomManager(project).append_turn(
            room_id,
            request,
            result.final_answer,
            result.session_id,
            agent_outputs=agent_outputs,
            implementation=outcome_to_dict(result.implementation),
        )
        self.current_room_id = saved_room_id
        self._load_rooms(selected_room_id=saved_room_id, persist_active=True)

        # Update Timeline Card
        self.timeline.add_assistant_turn(
            summary_content=result.final_answer,
            agent_outputs=agent_outputs,
            session_id=result.session_id,
            questions=result.questions,
            implementation=result.implementation,
        )

        # Append to log
        self.append_log(f"\nSession: {result.session_id}\n")
        for warning in result.warnings:
            self.append_log(f"Warning: {warning}\n")
        for key, command_result in result.command_results.items():
            self.append_log(
                f"\n[{key}] status={command_result.status} ok={command_result.ok} "
                f"elapsed={command_result.elapsed_seconds:.1f}s\n"
            )

        self.finish_run()

    def _base_agent(self, key: str) -> str:
        if key.startswith("round"):
            parts = key.split("_")
            if len(parts) >= 3:
                return parts[1]
        return key

    def append_log(self, text: str) -> None:
        self._append_readonly(self.log_text, text)
        clean = text.strip()
        if not clean or self.timeline.thinking_card is None:
            return

        # Friendly progress messages for thinking card (raw logs stay in the Log tab)
        if "Scanning project" in clean:
            self.timeline.update_thinking("プロジェクト構造をスキャン中...")
        elif "Running preflight" in clean:
            self.timeline.update_thinking("AI CLIの接続と事前確認中...")
        elif "Building local context" in clean:
            self.timeline.update_thinking("共通コンテキストを準備中...")
        elif "Starting brainstorm" in clean or "Running Round" in clean:
            self.timeline.update_thinking("Claude, Antigravity, Codex に並列ブレスト依頼中...")
        elif "Asking LM Studio" in clean or "ChairAgent" in clean or "Synthesizing" in clean:
            self.timeline.update_thinking(integrating_message())
        elif "Cancellation requested" in clean:
            self.timeline.update_thinking("キャンセル要求を処理中...")

    def _append_readonly(self, textbox: any, text: str) -> None:
        textbox.configure(state="normal")
        textbox.insert("end", text)
        if len(textbox.get("1.0", "end-1c")) > MAX_LOG_CHARS:
            textbox.delete("1.0", f"1.0+{MAX_LOG_CHARS // 4}c")
        textbox.see("end")
        textbox.configure(state="disabled")

    def _clear_outputs(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def finish_run(self, failed: bool = False) -> None:
        if self.run_state_machine is not None:
            # settle() resolves a cancelling run to `cancelled` even when the
            # loop reported a normal finish, so a cancellation that landed
            # just before the final answer isn't shown as a success.
            if failed:
                self.run_state_machine.fail()
            else:
                self.run_state_machine.settle()
        # Before active_run is cleared, so any gate still open is refused
        # rather than left for a later run to inherit.
        self._close_approval_dialog()
        self.active_run = None
        self.run_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")
        self._refresh_run_state_display()
        # Dropping the machine is what makes `running` false again, so it must
        # happen before on_run_state_changed() observes the tab.
        self.run_state_machine = None
        self.app.on_run_state_changed(self.tab_id, False)

    # ------------------------------------------------------------ lifecycle

    def submit_from_shortcut(self) -> None:
        content = self.request_text.get("1.0", "end-1c")
        if content.endswith("\n"):
            self.request_text.delete("end-2c", "end-1c")
        if not self.running:
            self.start_brainstorm()

    def session_state(self) -> dict:
        return {
            "project_path": self.project_path,
            "room_id": self.current_room_id,
            "automation_level": self.automation_level.get(),
        }

    def destroy(self) -> None:
        self.request_cancel()
        self.frame.destroy()
