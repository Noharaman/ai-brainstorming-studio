from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from src import config
from src.services import secret_redactor


class WorkspaceManager:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.brainstorm_dir = config.app_dir(self.project_root)

    def validate(self) -> None:
        if not self.project_root.exists() or not self.project_root.is_dir():
            raise ValueError(f"Project folder does not exist: {self.project_root}")

    def initialize(self) -> None:
        self.validate()
        self.brainstorm_dir.mkdir(exist_ok=True)
        for subdir in ("prompts", "sessions", "vendor_context"):
            (self.brainstorm_dir / subdir).mkdir(exist_ok=True)
        self._write_manifest()
        self._write_if_missing("context.md", "# Project Context\n\n")
        self._write_if_missing("memory.md", "# Memory\n\n- No extra API cost.\n- Keep user-facing summaries short.\n")
        self._write_if_missing("decisions.md", "# Decisions\n\n")
        self._write_if_missing("current_session.md", "# Current Session\n\n")
        self._write_if_missing("context_pack.md", "")
        self._write_if_missing("history.md", "# AI Brainstorm History\n\n")

    def write_context_files(self, context: str, current_session: str, context_pack: str) -> None:
        self.initialize()
        self.write_text("context.md", context)
        self.write_text("current_session.md", current_session)
        self.write_text("context_pack.md", context_pack)

    def write_vendor_context(self, vendor_paths: list[str], important_files: dict[str, str]) -> None:
        lines = [
            "# Vendor / Agent Context",
            "",
            "Existing AI-related files and directories are detected, but not moved or overwritten.",
            "",
            "## Detected Paths",
            "",
        ]
        if vendor_paths:
            lines.extend(f"- {path}" for path in vendor_paths)
        else:
            lines.append("- none")
        lines.append("")
        for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"):
            content = important_files.get(name)
            if content:
                lines.extend([f"## {name}", "", content[:4000], ""])
        self.write_text("vendor_context/detected.md", "\n".join(lines))

    def write_prompt(self, agent: str, prompt: str, session_id: str) -> None:
        prompt_dir = self.brainstorm_dir / "prompts" / session_id
        prompt_dir.mkdir(parents=True, exist_ok=True)
        (prompt_dir / f"{agent}.md").write_text(secret_redactor.redact(prompt))

    def write_session_artifact(self, session_id: str, name: str, content: str) -> None:
        """Persist one artifact. Redaction happens here, at the boundary.

        Artifacts carry CLI output, diffs and test logs straight from the
        user's repository, and any of those can contain a credential. Relying
        on each caller to redact first means one forgotten call site leaks —
        and there are a dozen call sites. Doing it here costs a pass over the
        text and cannot be skipped.
        """
        session_dir = self.brainstorm_dir / "sessions" / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / name).write_text(secret_redactor.redact(content))

    def append_history(self, session_id: str, content: str) -> None:
        history = self.brainstorm_dir / "history.md"
        timestamp = datetime.now(timezone.utc).isoformat()
        with history.open("a") as fh:
            fh.write(
                f"\n\n## Session {session_id} ({timestamp})\n\n"
                f"{secret_redactor.redact(content)}\n"
            )

    def write_text(self, relative_path: str, content: str) -> None:
        path = self.brainstorm_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def _write_if_missing(self, relative_path: str, content: str) -> None:
        path = self.brainstorm_dir / relative_path
        if not path.exists():
            path.write_text(content)

    def _write_manifest(self) -> None:
        path = self.brainstorm_dir / "manifest.json"
        now = datetime.now(timezone.utc).isoformat()
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError:
                data = {}
            if not isinstance(data, dict):
                data = {}
        else:
            data = {"created_at": now}
        data.update(
            {
                "schema_version": 1,
                "updated_at": now,
                "app": "AI Brainstorming Studio",
            }
        )
        path.write_text(json.dumps(data, indent=2))
