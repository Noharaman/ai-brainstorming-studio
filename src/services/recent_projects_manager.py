from __future__ import annotations

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".ai-brainstorm-studio"
RECENT_PROJECTS_FILE = CONFIG_DIR / "recent_projects.json"
MAX_RECENT_COUNT = 8


class RecentProjectsManager:
    def __init__(self, file_path: Path = RECENT_PROJECTS_FILE):
        self.file_path = file_path

    def get_recent_projects(self) -> list[str]:
        if not self.file_path.exists():
            return []
        try:
            data = json.loads(self.file_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                # Filter out paths that no longer exist
                valid_paths = [path for path in data if isinstance(path, str) and Path(path).exists()]
                return valid_paths[:MAX_RECENT_COUNT]
        except Exception:
            pass
        return []

    def add_project(self, project_path: str | Path) -> list[str]:
        path_str = str(Path(project_path).resolve())
        if not Path(path_str).exists():
            return self.get_recent_projects()

        recent = self.get_recent_projects()
        if path_str in recent:
            recent.remove(path_str)
        recent.insert(0, path_str)
        recent = recent[:MAX_RECENT_COUNT]

        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            self.file_path.write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
        return recent
