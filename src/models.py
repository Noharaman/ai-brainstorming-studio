from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


ProgressCallback = Callable[[str], None]


@dataclass
class ScanResult:
    project_root: Path
    tree: list[str]
    important_files: dict[str, str]
    vendor_paths: list[str]



@dataclass
class CommandResult:
    agent: str
    command: list[str]
    ok: bool
    status: str
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    elapsed_seconds: float = 0.0
    used_rtk: bool = False


@dataclass
class BrainstormResult:
    session_id: str
    context_pack: str
    prompts: dict[str, str]
    command_results: dict[str, CommandResult]
    integrated_summary: str
    refinement_summary: str = ""
    final_answer: str = ""
    questions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Present for every run; `attempted=False` for the read-only levels.
    #: Typed loosely to keep models.py free of a service import.
    implementation: object | None = None

