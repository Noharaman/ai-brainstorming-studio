from __future__ import annotations

import shutil
import subprocess


class UsageTracker:
    def rtk_gain_summary(self) -> str:
        if not shutil.which("rtk"):
            return "RTK gain: rtk missing"
        try:
            completed = subprocess.run(
                ["rtk", "gain"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=3,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return "RTK gain: timeout"
        except Exception:
            return "RTK gain: unavailable"

        text = completed.stdout.strip() if completed.stdout.strip() else completed.stderr.strip()
        if completed.returncode != 0 or not text:
            return "RTK gain: unavailable"
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if len(first_line) > 80:
            first_line = first_line[:77] + "..."
        return f"RTK gain: {first_line or 'available'}"
