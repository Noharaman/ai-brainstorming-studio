from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class RunContext:
    """Identity and parameters for one brainstorm run.

    Frozen so a run's own identity can never be swapped out from under an
    in-flight worker thread; the mutable `cancel_event` inside it is still
    how cancellation is signaled, as before.
    """

    run_id: str
    tab_id: str
    project_root: Path
    room_id: str
    request: str
    automation_level: int
    cancel_event: threading.Event = field(default_factory=threading.Event)
    selected_models: Mapping[str, str] = field(default_factory=dict)
    selected_efforts: Mapping[str, str] = field(default_factory=dict)
    effort: str | None = None
    catalog_version: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.selected_models, MappingProxyType):
            object.__setattr__(
                self,
                "selected_models",
                MappingProxyType(dict(self.selected_models)),
            )
        if not isinstance(self.selected_efforts, MappingProxyType):
            object.__setattr__(
                self,
                "selected_efforts",
                MappingProxyType(dict(self.selected_efforts)),
            )


class ProjectRunRegistry:
    """Ensures at most one active run per canonical/resolved project path,
    independent of any tab's lifecycle.

    A tab that gets closed mid-run still has its worker thread alive for a
    short grace period (cancellation + subprocess kill can take a couple of
    seconds); this registry is the source of truth that a *new* run on the
    same folder must wait for, since tab existence alone isn't enough to
    detect that race.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[str, RunContext] = {}

    def try_start(self, project_root: Path, tab_id: str) -> RunContext | None:
        """Phase 1: reserve the path before any disk writes happen (e.g. chat
        room creation). The returned RunContext has empty room_id/request —
        call finalize() once those are known to fill them in.
        """
        resolved = str(Path(project_root).resolve())
        with self._lock:
            if resolved in self._active:
                return None
            context = RunContext(
                run_id=uuid.uuid4().hex,
                tab_id=tab_id,
                project_root=Path(resolved),
                room_id="",
                request="",
                automation_level=0,
            )
            self._active[resolved] = context
            return context

    def finalize(
        self,
        run_context: RunContext,
        room_id: str,
        request: str,
        automation_level: int,
        selected_models: dict[str, str] | None = None,
        selected_efforts: dict[str, str] | None = None,
        effort: str | None = None,
        catalog_version: str = "",
    ) -> RunContext | None:
        """Phase 2: fill in the run's parameters once they're known, keeping
        the same run_id/cancel_event. Must be called after try_start().

        Returns None if `run_context`'s reservation is no longer the one the
        registry holds for this path (released, or superseded by a newer
        run) — the caller must not treat a None result as a valid, owned run.
        """
        resolved = str(Path(run_context.project_root).resolve())
        with self._lock:
            current = self._active.get(resolved)
            if current is None or current.run_id != run_context.run_id:
                return None
            updated = replace(
                run_context,
                room_id=room_id,
                request=request,
                automation_level=automation_level,
                selected_models=selected_models or {},
                selected_efforts=selected_efforts or {},
                effort=effort,
                catalog_version=catalog_version,
            )
            self._active[resolved] = updated
            return updated

    def release(self, run_context: RunContext) -> None:
        resolved = str(Path(run_context.project_root).resolve())
        with self._lock:
            current = self._active.get(resolved)
            # Only the run that actually holds the slot may release it, so a
            # slow-to-clean-up old run can't accidentally free a newer one.
            if current is not None and current.run_id == run_context.run_id:
                del self._active[resolved]

    def active_run_for(self, project_root: Path) -> RunContext | None:
        with self._lock:
            return self._active.get(str(Path(project_root).resolve()))
