from __future__ import annotations

import os
from pathlib import Path

from src import config


class SafetyGuard:
    def environment_warnings(self) -> list[str]:
        detected = [name for name in sorted(config.BLOCKED_CHILD_ENV_VARS) if os.environ.get(name)]
        if not detected:
            return []
        joined = ", ".join(detected[:8])
        suffix = "" if len(detected) <= 8 else f", ... (+{len(detected) - 8})"
        return [
            "Existing CLI Mode: these billing/provider environment variables are set and are "
            f"inherited by the AI CLIs as you configured them ({joined}{suffix}). This app does "
            "not change or remove them, and never displays or stores their values."
        ]

    def project_warnings(self, project_root: Path) -> list[str]:
        warnings: list[str] = []
        if (project_root / "README.md").exists():
            warnings.append("README.md exists and will not be used as internal AI memory.")
        if config.app_dir(project_root).exists():
            warnings.append(".ai-brainstorm/ already exists and will be reused.")
        return warnings
