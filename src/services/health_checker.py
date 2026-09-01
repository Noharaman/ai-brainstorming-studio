from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from src import config
from src.models import CommandResult, ProgressCallback
from src.services import (
    agent_model_selector,
    cli_execution_policy,
    cli_status,
)
from src.services.chair_agent import ChairAgent
from src.services.cli_adapters import CliAdapters
from src.services.lm_studio_manager import LMStudioManager
from src.services.process_runner import ProcessRunner


def _checked_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ToolStatus:
    name: str
    available: bool
    detail: str = ""
    status: str = ""
    source: str = "discovery"
    checked_at: str = field(default_factory=_checked_now)
    executable_path: str = ""

    def __post_init__(self) -> None:
        if not self.status:
            object.__setattr__(self, "status", "ok" if self.available else "unknown")


StatusCallback = Callable[[ToolStatus], None]


def tool_name_for_agent(agent: str) -> str:
    if agent.startswith("round"):
        parts = agent.split("_")
        if len(parts) == 3:
            agent = parts[1]
    return "Antigravity(agy)" if agent == "gemini" else agent


def tool_status_from_result(result: CommandResult, source: str = "preflight") -> ToolStatus:
    name = tool_name_for_agent(result.agent)
    if result.ok:
        detail = "AI接続テストに成功し、モデルから応答を受け取りました。"
    else:
        detail = cli_status.guidance_for(result.status)
    return ToolStatus(
        name=name,
        available=result.ok,
        detail=detail,
        status=result.status,
        source=source,
    )


def merge_health_statuses(
    discovery_statuses: list[ToolStatus],
    runtime_statuses: dict[str, ToolStatus],
) -> list[ToolStatus]:
    """Merge cheap discovery with the latest explicit runtime evidence.

    A fresh missing/disabled discovery result always wins.  Otherwise a recent
    preflight/run result is stronger evidence than merely finding a binary on
    PATH.  This prevents a post-run ``which`` refresh from painting a logged-
    out or rate-limited CLI green again.
    """
    merged: list[ToolStatus] = []
    seen: set[str] = set()
    discovery_blockers = {"command_missing", "slot_disabled", "unsupported_client"}
    for discovered in discovery_statuses:
        seen.add(discovered.name)
        runtime = runtime_statuses.get(discovered.name)
        if runtime is not None and discovered.status not in discovery_blockers:
            merged.append(
                replace(
                    runtime,
                    executable_path=discovered.executable_path or runtime.executable_path,
                )
            )
        else:
            merged.append(discovered)
    for name, runtime in runtime_statuses.items():
        if name not in seen:
            merged.append(runtime)
    return merged


class HealthChecker:
    def __init__(self, policy=None):
        # Same policy object the runner uses, so the badge and the launch
        # cannot disagree about whether a slot is usable.
        self.policy = policy or cli_execution_policy.active()

    def check_all(self, auto_start_lms: bool = True) -> list[ToolStatus]:
        chair = ChairAgent()
        is_available = chair.available()
        detail_msg = chair.base_url
        if not is_available and auto_start_lms:
            started, msg = LMStudioManager().ensure_server_running(timeout_seconds=6)
            if started:
                # Starting the server does not load the pinned chair model, so
                # re-probe rather than declaring availability from the server
                # coming up. Previously this reported "ok" for a running
                # server with no usable model behind it.
                chair.invalidate_cache()
                is_available = chair.available()
                detail_msg = f"{chair.base_url} (auto-started)"
            else:
                detail_msg = f"{chair.base_url} ({msg})"
        if is_available:
            detail_msg = f"{detail_msg} / model: {chair.model_name()}"
        else:
            reason = chair.unavailable_reason()
            if reason:
                detail_msg = f"{detail_msg} — {reason}"

        statuses = [
            self._check_command("lms"),
            self._check_claude(),
            self._check_antigravity(),
            self._check_command("codex", installed_status="installed_unverified"),
            ToolStatus(
                "LM Studio",
                is_available,
                detail_msg,
                status="ok" if is_available else "service_unavailable",
            ),
        ]
        return statuses

    def _check_claude(self) -> ToolStatus:
        claude_path = shutil.which("claude")
        if not claude_path:
            return ToolStatus("claude", False, "not found", status="command_missing")
        if not config.is_agent_slot_enabled("claude"):
            return ToolStatus(
                "claude",
                False,
                f"{claude_path} (slot switched off in this build)",
                status="slot_disabled",
                executable_path=claude_path,
            )
        return ToolStatus(
            "claude",
            True,
            claude_path,
            status="installed_unverified",
            executable_path=claude_path,
        )

    def _check_antigravity(self) -> ToolStatus:
        agy_path = shutil.which("agy")
        if agy_path:
            adapters = CliAdapters(prefer_rtk=False, policy=self.policy)
            if adapters.command_exists("gemini"):
                return ToolStatus(
                    "Antigravity(agy)",
                    True,
                    agy_path,
                    status="installed_unverified",
                    executable_path=agy_path,
                )
            return ToolStatus(
                "Antigravity(agy)",
                False,
                f"{agy_path} (no gemini model confirmed billing_status=subscription_safe yet; slot disabled)",
                status="slot_disabled",
                executable_path=agy_path,
            )
        gemini_path = shutil.which("gemini")
        if gemini_path:
            return ToolStatus(
                "Antigravity(agy)",
                False,
                f"agy not found; legacy gemini at {gemini_path}",
                status="unsupported_client",
                executable_path=gemini_path,
            )
        return ToolStatus("Antigravity(agy)", False, "not found", status="command_missing")

    def _check_command(self, command: str, installed_status: str = "installed") -> ToolStatus:
        path = shutil.which(command)
        if path and not config.is_agent_slot_enabled(command):
            # `_check_claude` and `_check_antigravity` each did this for
            # themselves, so codex was the one agent whose closed slot still
            # showed as an ordinary "installed" lamp — the header said Claude
            # and Antigravity were paused while Codex looked merely unverified,
            # for three slots that are all equally closed.
            return ToolStatus(
                command,
                False,
                f"{path} (slot switched off in this build)",
                status="slot_disabled",
                executable_path=path,
            )
        return ToolStatus(
            command,
            bool(path),
            path or "not found",
            status=installed_status if path else "command_missing",
            executable_path=path or "",
        )

    async def preflight_all(
        self,
        cwd: Path,
        automation_level: int = 1,
        prefer_rtk: bool = False,
        progress: ProgressCallback | None = None,
        cancel_event: object | None = None,
        status_callback: StatusCallback | None = None,
    ) -> tuple[dict[str, CommandResult], list[str]]:
        def emit(message: str) -> None:
            if progress:
                progress(message)

        def publish(result: CommandResult) -> None:
            if status_callback:
                status_callback(tool_status_from_result(result))

        prompts = {
            "claude": self._preflight_prompt("Claude"),
            "gemini": self._preflight_prompt("Antigravity"),
            "codex": self._preflight_prompt("Codex"),
        }
        adapters = CliAdapters(prefer_rtk=prefer_rtk, policy=self.policy)
        # No chair consultation happens before/during preflight (the user's
        # request, and even LM Studio's own availability, aren't known yet)
        # — but preflight must still never run claude/gemini on the CLI's
        # own unverified local default. default_selection() gives each agent
        # its catalog-curated selector_default with no chair involved.
        agent_selection = agent_model_selector.default_selection(set(prompts.keys()))
        commands, warnings = adapters.build_commands(
            prompts, automation_level=automation_level, agent_selection=agent_selection
        )
        runner = ProcessRunner(policy=self.policy)
        tasks = []
        results: dict[str, CommandResult] = {}

        emit(
            "Running CLI auth preflight with tiny safe prompts "
            f"(timeout {config.PREFLIGHT_TIMEOUT_SECONDS}s each, direct CLI without rtk)."
        )
        for warning in warnings:
            emit(f"Warning: {warning}")
        for agent, command in commands.items():
            if self._is_cancelled(cancel_event):
                break
            if not adapters.command_exists(agent):
                status, message = adapters.skip_reason(agent)
                results[agent] = CommandResult(
                    agent=agent,
                    command=[],
                    ok=False,
                    status=status,
                    stderr=message,
                    used_rtk=adapters.uses_rtk(agent),
                )
                publish(results[agent])
                emit(f"{agent}: preflight skipped; {message}")
                continue
            executable = command[1] if command and command[0] == "rtk" and len(command) > 1 else command[0]
            emit(f"{agent}: preflight starting via {executable}.")
            tasks.append(
                runner.run(
                    agent=agent,
                    command=command,
                    cwd=cwd,
                    timeout_seconds=config.PREFLIGHT_TIMEOUT_SECONDS,
                    used_rtk=adapters.uses_rtk(agent),
                    cancel_event=cancel_event,
                    success_patterns=("OK",),
                    claude_spec=(
                        adapters.claude_spec(prompts[agent], agent_selection)
                        if agent == "claude"
                        else None
                    ),
                )
            )

        if tasks:
            for result in await asyncio.gather(*tasks):
                results[result.agent] = result
                publish(result)
                emit(
                    f"{result.agent}: preflight finished with status={result.status}, "
                    f"ok={result.ok}, elapsed={result.elapsed_seconds:.1f}s."
                )

        for agent in prompts:
            if agent not in results:
                results[agent] = CommandResult(
                    agent=agent,
                    command=[],
                    ok=False,
                    status="cancelled" if self._is_cancelled(cancel_event) else "skipped",
                    stderr="Preflight cancelled." if self._is_cancelled(cancel_event) else "Preflight skipped.",
                )
                publish(results[agent])
        return results, warnings

    def preflight_all_sync(
        self,
        cwd: Path,
        automation_level: int = 1,
        prefer_rtk: bool = False,
        progress: ProgressCallback | None = None,
        cancel_event: object | None = None,
        status_callback: StatusCallback | None = None,
    ) -> tuple[dict[str, CommandResult], list[str]]:
        return asyncio.run(
            self.preflight_all(
                cwd,
                automation_level,
                prefer_rtk,
                progress,
                cancel_event,
                status_callback,
            )
        )

    def _preflight_prompt(self, label: str) -> str:
        return (
            f"{label} auth preflight. Reply exactly OK. "
            "Do not inspect files, run tools, edit files, or produce any explanation."
        )

    def _is_cancelled(self, cancel_event: object | None) -> bool:
        is_set = getattr(cancel_event, "is_set", None)
        return bool(is_set and is_set())
