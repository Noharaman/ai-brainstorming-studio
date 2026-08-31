from __future__ import annotations

import compileall
import importlib
from pathlib import Path


class VerificationRunner:
    def compile_sources(self, root: Path) -> bool:
        return compileall.compile_dir(str(root / "src"), quiet=1)

    def import_smoke_test(self) -> bool:
        try:
            importlib.import_module("src.main")
            importlib.import_module("src.gui.app")
            importlib.import_module("src.services.refinement_loop")
            return True
        except Exception:
            return False

    def run_basic_checks(self, root: Path) -> bool:
        return self.compile_sources(root) and self.import_smoke_test()
