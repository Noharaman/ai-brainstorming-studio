from __future__ import annotations

from pathlib import Path

from src import config
from src.models import ScanResult


class ContextScanner:
    def __init__(self, project_root: Path):
        self.project_root = project_root.resolve()
        self.gitignore_patterns = self._load_gitignore()

    def scan(self) -> ScanResult:
        tree = self._build_tree()
        important_files = self._read_important_files()
        vendor_paths = self._detect_vendor_paths()
        return ScanResult(
            project_root=self.project_root,
            tree=tree,
            important_files=important_files,
            vendor_paths=vendor_paths,
        )

    def _load_gitignore(self) -> list[str]:
        path = self.project_root / ".gitignore"
        if not path.exists():
            return []
        patterns: list[str] = []
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("!"):
                patterns.append(line.rstrip("/"))
        return patterns

    def _ignored(self, path: Path) -> bool:
        rel = path.relative_to(self.project_root).as_posix()
        parts = set(path.relative_to(self.project_root).parts)
        if parts & config.EXCLUDED_DIRS:
            return True
        for pattern in self.gitignore_patterns:
            if not pattern:
                continue
            if rel == pattern or rel.startswith(pattern + "/") or path.name == pattern:
                return True
        return False

    def _build_tree(self) -> list[str]:
        items: list[str] = []
        for path in sorted(self.project_root.rglob("*")):
            if len(items) >= config.MAX_TREE_ITEMS:
                items.append("... truncated ...")
                break
            try:
                rel_path = path.relative_to(self.project_root)
            except ValueError:
                continue
            if self._ignored(path):
                continue
            depth = len(rel_path.parts) - 1
            if depth > 5:
                continue
            suffix = "/" if path.is_dir() else ""
            items.append(f"{'  ' * depth}{path.name}{suffix}")
        return items

    def _read_important_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for name in config.IMPORTANT_FILES:
            path = self.project_root / name
            if path.is_file():
                files[name] = path.read_text(errors="ignore")[: config.MAX_FILE_READ_CHARS]
        return files

    def _detect_vendor_paths(self) -> list[str]:
        found: list[str] = []
        for name in config.AI_VENDOR_PATHS:
            path = self.project_root / name
            if path.exists():
                found.append(name + ("/" if path.is_dir() else ""))
        return found

