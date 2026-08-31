from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from src import config
from src.services.chair_agent import ChairAgent


class LMStudioManager:
    def __init__(self, base_url: str = config.LM_STUDIO_BASE_URL):
        self.chair = ChairAgent(base_url=base_url)
        self.lms_path = self._find_lms_binary()

    def _find_lms_binary(self) -> str | None:
        path = shutil.which("lms")
        if path:
            return path
        # Fallback to standard LM Studio installation paths
        user_home = Path.home()
        candidates = [
            user_home / ".lmstudio" / "bin" / "lms",
            Path("/usr/local/bin/lms"),
            Path("/opt/homebrew/bin/lms"),
        ]
        for candidate in candidates:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        return None

    def ensure_server_running(
        self, timeout_seconds: int = 15, cancel_event: object | None = None
    ) -> tuple[bool, str]:
        # 1. Check if server is already responding
        if self.chair.available():
            return True, "LM Studio server is already running."

        if self._is_cancelled(cancel_event):
            return False, "Cancelled before starting LM Studio."

        # 2. Check if lms binary is available
        if not self.lms_path:
            return False, "lms CLI binary not found. Please start LM Studio manually."

        # 3. Attempt to start server via lms CLI
        try:
            subprocess.Popen(
                [self.lms_path, "server", "start"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:
            return False, f"Failed to execute lms server start: {exc}"

        # 4. Wait & poll for the server to become available. self.chair caches
        # its availability after the very first probe above, so it must be
        # invalidated before each re-check here — otherwise every poll in
        # this loop just replays that same stale (offline) result and the
        # loop can never actually detect a successful startup.
        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_seconds:
            if self._wait_or_cancelled(cancel_event, 1.0):
                return False, "Cancelled while waiting for LM Studio to start."
            self.chair.invalidate_cache()
            if self.chair.available():
                return True, "LM Studio server started successfully via CLI."

        return False, f"LM Studio server start command executed, but server did not respond within {timeout_seconds}s."

    def _wait_or_cancelled(self, cancel_event: object | None, seconds: float) -> bool:
        """Sleeps for `seconds`, waking immediately if cancelled. Returns
        whether it woke up due to cancellation."""
        wait = getattr(cancel_event, "wait", None)
        if callable(wait):
            return bool(wait(seconds))
        time.sleep(seconds)
        return False

    def _is_cancelled(self, cancel_event: object | None) -> bool:
        is_set = getattr(cancel_event, "is_set", None)
        return bool(is_set and is_set())
