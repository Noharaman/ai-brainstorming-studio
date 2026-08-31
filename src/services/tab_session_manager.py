from __future__ import annotations

import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".ai-brainstorm-studio"
OPEN_TABS_FILE = CONFIG_DIR / "open_tabs.json"
MAX_OPEN_TABS = 12


class TabSessionManager:
    """Remembers which project folders were open so tabs survive a restart.

    Only paths and lightweight UI state are stored, and always under the user's
    home config directory. Nothing is written into the target projects.
    """

    def __init__(self, file_path: Path = OPEN_TABS_FILE):
        self.file_path = file_path

    def load(self) -> list[dict]:
        if not self.file_path.exists():
            return []
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return []
        if not isinstance(data, dict):
            return []
        tabs = data.get("tabs")
        if not isinstance(tabs, list):
            return []

        restored: list[dict] = []
        for entry in tabs[:MAX_OPEN_TABS]:
            if not isinstance(entry, dict):
                continue
            path = entry.get("project_path")
            if not isinstance(path, str) or not path or not Path(path).is_dir():
                continue
            restored.append(
                {
                    "project_path": path,
                    "room_id": str(entry.get("room_id") or ""),
                    "automation_level": str(entry.get("automation_level") or ""),
                }
            )
        return restored

    def save(self, tabs: list[dict], active_index: int = 0) -> None:
        payload = {
            "schema_version": 1,
            "active_index": max(0, active_index),
            "tabs": tabs[:MAX_OPEN_TABS],
        }
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp_path = self.file_path.with_name(f"{self.file_path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, self.file_path)
        except OSError:
            pass

    def active_index(self) -> int:
        if not self.file_path.exists():
            return 0
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            return 0
        if not isinstance(data, dict):
            return 0
        try:
            return max(0, int(data.get("active_index") or 0))
        except (TypeError, ValueError):
            return 0
