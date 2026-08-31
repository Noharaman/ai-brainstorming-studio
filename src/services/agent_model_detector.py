from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from src.services.agent_model_selector import (
    CATALOG_PATH,
    load_catalog,
    refresh_gemini_catalog_from_agy,
    save_catalog_atomically,
)


@dataclass
class ModelDiscoveryResult:
    agent: str
    models: list[dict] = field(default_factory=list)
    version: str = ""
    source: str = "catalog"
    changed: bool = False


class AgentModelDetector:
    """Discovers available models and CLI version changes per agent without blocking GUI.

    - Antigravity: parses `agy models` output.
    - Claude / Codex: checks `--version` for catalog maintenance.
    """

    def __init__(self, catalog_path: Path = CATALOG_PATH) -> None:
        self.catalog_path = catalog_path

    def detect_antigravity_models(self) -> ModelDiscoveryResult:
        cmd_path = shutil.which("agy")
        if not cmd_path:
            return ModelDiscoveryResult(agent="gemini", source="none")

        old_catalog = load_catalog(self.catalog_path)
        old_ids = {m["id"] for m in old_catalog.get("gemini", {}).get("models", []) if isinstance(m, dict) and "id" in m}

        success = refresh_gemini_catalog_from_agy(self.catalog_path)
        today = datetime.now().strftime("%Y-%m-%d")
        new_catalog = load_catalog(self.catalog_path)
        gemini_data = new_catalog.get("gemini", {})
        new_models = gemini_data.get("models", [])
        new_ids = {m["id"] for m in new_models if isinstance(m, dict) and "id" in m}

        # Timestamp models
        for m in new_models:
            if isinstance(m, dict):
                m["last_seen"] = today
                if "first_seen" not in m:
                    m["first_seen"] = today
        if new_catalog:
            save_catalog_atomically(new_catalog, self.catalog_path)

        changed = old_ids != new_ids
        return ModelDiscoveryResult(
            agent="gemini",
            models=new_models,
            source="agy_models",
            changed=changed,
        )

    def detect_cli_version(self, agent: str) -> tuple[str, bool]:
        cmd_name = "agy" if agent == "gemini" else agent
        if not shutil.which(cmd_name):
            return "", False
        try:
            res = subprocess.run(
                [cmd_name, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
            )
            if res.returncode == 0:
                current_ver = res.stdout.strip()
                catalog = load_catalog(self.catalog_path)
                agent_data = catalog.get(agent, {})
                old_ver = agent_data.get("detected_version", "")
                changed = (current_ver != old_ver)
                today = datetime.now().strftime("%Y-%m-%d")
                agent_data["detected_version"] = current_ver
                agent_data["last_seen"] = today
                if "first_seen" not in agent_data:
                    agent_data["first_seen"] = today
                if catalog:
                    catalog[agent] = agent_data
                    save_catalog_atomically(catalog, self.catalog_path)
                return current_ver, changed
        except Exception:
            pass
        return "", False

    def refresh_all(self) -> dict[str, ModelDiscoveryResult]:
        results: dict[str, ModelDiscoveryResult] = {}
        results["gemini"] = self.detect_antigravity_models()

        catalog = load_catalog(self.catalog_path)
        for agent in ("gemini", "claude", "codex"):
            ver, ver_changed = self.detect_cli_version(agent)
            if agent == "gemini":
                results["gemini"].version = ver
                if ver_changed:
                    results["gemini"].changed = True
            else:
                agent_models = catalog.get(agent, {}).get("models", [])
                results[agent] = ModelDiscoveryResult(
                    agent=agent,
                    models=agent_models,
                    version=ver,
                    source="catalog",
                    changed=ver_changed,
                )
        return results
