from __future__ import annotations

from pathlib import Path
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:
    ctk = None


class MarkdownEditor(ctk.CTkFrame if ctk else object):
    def __init__(
        self,
        master: any,
        height: int = 300,
        initial_text: str = "",
        on_save_callback: any = None,
        **kwargs,
    ) -> None:
        if ctk is None:
            raise RuntimeError("customtkinter is required for MarkdownEditor")

        super().__init__(master, **kwargs)
        self.height = height
        self.on_save_callback = on_save_callback
        self.is_preview_mode = False
        self.raw_markdown = initial_text or ""

        self._build_ui()
        if initial_text:
            self.set_text(initial_text)

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # --- Toolbar (Grid layout to prevent layout collapse on window resize) ---
        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=4, pady=(2, 6))
        toolbar.grid_columnconfigure(0, weight=0)
        toolbar.grid_columnconfigure(1, weight=1)
        toolbar.grid_columnconfigure(2, weight=0)

        # 1. Mode switch (Left)
        self.mode_segmented_button = ctk.CTkSegmentedButton(
            toolbar,
            values=["✏️ 編集", "👁️ プレビュー"],
            command=self._on_mode_change,
            width=140,
        )
        self.mode_segmented_button.set("✏️ 編集")
        self.mode_segmented_button.grid(row=0, column=0, sticky="w", padx=(0, 6))

        # 2. Formatting quick buttons (Center - Scrollable or Grid to prevent squishing)
        self.format_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        self.format_frame.grid(row=0, column=1, sticky="w", padx=2)

        btn_specs = [
            ("H1", "# ", ""),
            ("H2", "## ", ""),
            ("太字", "**", "**"),
            ("斜体", "*", "*"),
            ("リスト", "- ", ""),
            ("コード", "`", "`"),
            ("ブロック", "```\n", "\n```"),
            ("引用", "> ", ""),
        ]
        for col_idx, (label, prefix, suffix) in enumerate(btn_specs):
            btn = ctk.CTkButton(
                self.format_frame,
                text=label,
                width=36,
                height=26,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color=("gray75", "gray25"),
                hover_color=("gray65", "gray35"),
                command=lambda p=prefix, s=suffix: self._insert_formatting(p, s),
            )
            btn.grid(row=0, column=col_idx, padx=1)

        # 3. Action buttons (Right)
        actions_frame = ctk.CTkFrame(toolbar, fg_color="transparent")
        actions_frame.grid(row=0, column=2, sticky="e", padx=(6, 0))

        self.copy_button = ctk.CTkButton(
            actions_frame,
            text="📋 コピー",
            width=68,
            height=26,
            command=self._copy_to_clipboard,
        )
        self.copy_button.pack(side="left", padx=2)

        self.save_button = ctk.CTkButton(
            actions_frame,
            text="💾 保存",
            width=65,
            height=26,
            fg_color="#1f6aa5",
            hover_color="#144870",
            command=self._save_to_file,
        )
        self.save_button.pack(side="left", padx=(2, 0))

        # --- Textarea Area ---
        self.textbox = ctk.CTkTextbox(self, height=self.height, font=ctk.CTkFont(family="Consolas", size=13))
        self.textbox.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)

        # Listen to key release in edit mode to update raw_markdown
        self.textbox.bind("<KeyRelease>", self._on_text_change)

        # Configure Tkinter tags for preview rendering
        self._configure_preview_tags()

    def _configure_preview_tags(self) -> None:
        text_widget = self.textbox._textbox
        text_widget.tag_config("h1", font=("Helvetica", 18, "bold"), foreground="#4A90E2")
        text_widget.tag_config("h2", font=("Helvetica", 15, "bold"), foreground="#50E3C2")
        text_widget.tag_config("h3", font=("Helvetica", 13, "bold"), foreground="#F5A623")
        text_widget.tag_config("bold", font=("Helvetica", 12, "bold"), foreground="#E0E0E0")
        text_widget.tag_config("italic", font=("Helvetica", 12, "italic"), foreground="#BDBDBD")
        text_widget.tag_config("code", font=("Courier", 11), background="#2D2D2D", foreground="#E6DB74")
        text_widget.tag_config("quote", font=("Helvetica", 12, "italic"), foreground="#90A4AE", lmargin1=15, lmargin2=15)
        text_widget.tag_config("list", lmargin1=10, lmargin2=20, foreground="#E0E0E0")

    def _on_text_change(self, _event: object | None = None) -> None:
        if not self.is_preview_mode:
            self.raw_markdown = self.textbox.get("1.0", "end-1c")

    def _insert_formatting(self, prefix: str, suffix: str) -> None:
        if self.is_preview_mode:
            return
        try:
            sel_start = self.textbox.index("sel.first")
            sel_end = self.textbox.index("sel.last")
            selected_text = self.textbox.get(sel_start, sel_end)
            self.textbox.delete(sel_start, sel_end)
            self.textbox.insert(sel_start, f"{prefix}{selected_text}{suffix}")
        except Exception:
            cursor_pos = self.textbox.index("insert")
            self.textbox.insert(cursor_pos, f"{prefix}{suffix}")
            if suffix:
                new_index = f"{cursor_pos}+{len(prefix)}c"
                self.textbox.mark_set("insert", new_index)
        self.raw_markdown = self.textbox.get("1.0", "end-1c")

    def _on_mode_change(self, value: str) -> None:
        if value == "👁️ プレビュー":
            self._switch_to_preview()
        else:
            self._switch_to_edit()

    def _switch_to_preview(self) -> None:
        if not self.is_preview_mode:
            # Sync raw markdown before switching
            self.raw_markdown = self.textbox.get("1.0", "end-1c")

        self.is_preview_mode = True

        # Hide quick format buttons in preview mode
        self.format_frame.grid_remove()

        # Render formatted markdown from raw_markdown
        text_widget = self.textbox._textbox
        text_widget.config(state="normal")
        self.textbox.delete("1.0", "end")

        lines = self.raw_markdown.splitlines()
        in_code_block = False

        for i, line in enumerate(lines):
            line_str = line + ("\n" if i < len(lines) - 1 else "")
            stripped = line.strip()

            if stripped.startswith("```"):
                in_code_block = not in_code_block
                self.textbox.insert("end", line_str, ("code",))
                continue

            if in_code_block:
                self.textbox.insert("end", line_str, ("code",))
                continue

            if line.startswith("# "):
                self.textbox.insert("end", line[2:] + "\n", ("h1",))
            elif line.startswith("## "):
                self.textbox.insert("end", line[3:] + "\n", ("h2",))
            elif line.startswith("### "):
                self.textbox.insert("end", line[4:] + "\n", ("h3",))
            elif line.startswith("> "):
                self.textbox.insert("end", line[2:] + "\n", ("quote",))
            elif line.startswith("- ") or line.startswith("* "):
                self.textbox.insert("end", "• " + line[2:] + "\n", ("list",))
            else:
                self._insert_formatted_line(line_str)

        text_widget.config(state="disabled")

    def _insert_formatted_line(self, line: str) -> None:
        parts = line.split("**")
        for idx, part in enumerate(parts):
            if not part:
                continue
            tags = ("bold",) if idx % 2 == 1 else ()
            self.textbox.insert("end", part, tags)

    def _switch_to_edit(self) -> None:
        self.is_preview_mode = False
        text_widget = self.textbox._textbox
        text_widget.config(state="normal")
        self.textbox.delete("1.0", "end")
        self.textbox.insert("1.0", self.raw_markdown)

        # Re-show format buttons in edit mode
        self.format_frame.grid(row=0, column=1, sticky="w", padx=2)

    def get_text(self) -> str:
        if self.is_preview_mode:
            return self.raw_markdown
        return self.textbox.get("1.0", "end-1c")

    def set_text(self, text: str) -> None:
        self.raw_markdown = text or ""
        text_widget = self.textbox._textbox
        text_widget.config(state="normal")
        if self.is_preview_mode:
            self._switch_to_preview()
        else:
            self.textbox.delete("1.0", "end")
            self.textbox.insert("1.0", self.raw_markdown)

    def _copy_to_clipboard(self) -> None:
        text = self.get_text()
        self.clipboard_clear()
        self.clipboard_append(text)
        messagebox.showinfo("クリップボード", "テキストをクリップボードにコピーしました！")

    def _save_to_file(self) -> None:
        text = self.get_text()
        if not text.strip():
            messagebox.showwarning("警告", "保存するテキストが空です。")
            return

        file_path = filedialog.asksaveasfilename(
            defaultextension=".md",
            filetypes=[("Markdown files", "*.md"), ("Text files", "*.txt"), ("All files", "*.*")],
            title="議長AIまとめを保存",
        )
        if file_path:
            try:
                Path(file_path).write_text(text, encoding="utf-8")
                messagebox.showinfo("保存完了", f"保存されました:\n{file_path}")
                if self.on_save_callback:
                    self.on_save_callback(Path(file_path))
            except Exception as exc:
                messagebox.showerror("エラー", f"ファイルの保存に失敗しました:\n{exc}")
