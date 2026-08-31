from __future__ import annotations

import asyncio
from pathlib import Path

from src import config
from src.models import CommandResult, ProgressCallback
from src.services.agent_model_selector import AgentSelection
from src.services import cli_execution_policy, cli_status
from src.services.cli_adapters import CliAdapters
from src.services.process_runner import ProcessRunner
from src.services.write_grant import WriteGrant, grant_for


class CliRunner:
    def __init__(self, prefer_rtk: bool = True, policy=None):
        # One policy object shared by the adapters and the runner, so what the
        # availability check promises is what the launch actually does.
        self.policy = policy or cli_execution_policy.active()
        self.adapters = CliAdapters(prefer_rtk=prefer_rtk, policy=self.policy)
        self.process_runner = ProcessRunner(policy=self.policy)

    async def run_all(
        self,
        prompts: dict[str, str],
        cwd: Path,
        automation_level: int = 2,
        progress: ProgressCallback | None = None,
        cancel_event: object | None = None,
        agent_selection: AgentSelection | None = None,
        grant: WriteGrant | None = None,
        run_id: str = "",
    ) -> tuple[dict[str, CommandResult], list[str]]:
        def emit(message: str) -> None:
            if progress:
                progress(message)

        commands, warnings = self.adapters.build_commands(
            prompts,
            automation_level=automation_level,
            agent_selection=agent_selection,
            grant=grant,
            run_id=run_id,
        )
        for warning in warnings:
            emit(f"Warning: {warning}")
        wrapped = sorted(a for a in prompts if self.adapters.uses_rtk(a))
        if wrapped:
            emit(f"Through rtk: {', '.join(wrapped)}.")
        else:
            emit(
                "AI CLIs run directly, not through rtk: rtk saves 0% on them and would record "
                "the full prompt in its history database."
            )
        if self.policy.inherit_user_environment:
            emit(
                "Existing CLI Mode: each CLI runs with your own login, settings, and environment. "
                "This app does not change them, and cannot guarantee how the run is billed."
            )
        else:
            emit(
                "Subscription-only guard active. API key environment variables are stripped from "
                "CLI child processes."
            )
        tasks = []
        results: dict[str, CommandResult] = {}
        for agent, command in commands.items():
            if self._is_cancelled(cancel_event):
                emit("Cancellation requested before CLI start.")
                break
            if not self.adapters.command_exists(agent):
                status, message = self.adapters.skip_reason(agent)
                warnings.append(f"{agent} skipped: {message}")
                emit(f"{agent}: skipping — {message}")
                # Record it now: the generic backfill below would otherwise
                # flatten this to "skipped / CLI missing", losing the real
                # reason.
                results[agent] = CommandResult(
                    agent=agent,
                    command=[],
                    ok=False,
                    status=status,
                    stderr=message,
                    used_rtk=self.adapters.uses_rtk(agent),
                )
                continue
            executable = command[1] if command and command[0] == "rtk" and len(command) > 1 else command[0]
            emit(
                f"{agent}: starting non-interactive run "
                f"via {executable} "
                f"(timeout {config.DEFAULT_TIMEOUT_SECONDS}s, stdin disabled)."
            )
            spec = (
                self.adapters.claude_spec(prompts[agent], agent_selection)
                if agent == "claude"
                else None
            )
            agent_grant = grant_for(grant, agent, run_id)
            if agent_grant is not None:
                emit(f"{agent}: 承認済みの実装フェーズとして、書き込み権限付きで実行します。")
            tasks.append(
                self._run_one(agent, command, cwd, emit, cancel_event, spec, agent_grant)
            )
        if tasks:
            for result in await asyncio.gather(*tasks):
                results[result.agent] = result
                emit(
                    f"{result.agent}: finished with status={result.status}, "
                    f"ok={result.ok}, elapsed={result.elapsed_seconds:.1f}s."
                )
        for agent in prompts:
            if agent not in results:
                results[agent] = CommandResult(
                    agent=agent,
                    command=[],
                    ok=False,
                    status="cancelled" if self._is_cancelled(cancel_event) else "skipped",
                    stderr="Cancelled." if self._is_cancelled(cancel_event) else "CLI missing or skipped.",
                )
        return results, warnings

    async def _run_one(
        self,
        agent: str,
        command: list[str],
        cwd: Path,
        emit: ProgressCallback,
        cancel_event: object | None,
        claude_spec=None,
        grant: WriteGrant | None = None,
    ) -> CommandResult:
        result = await self.process_runner.run(
            agent=agent,
            command=command,
            cwd=cwd,
            used_rtk=self.adapters.uses_rtk(agent),
            cancel_event=cancel_event,
            claude_spec=claude_spec,
            grant=grant,
        )
        if result.status in cli_status.NOTABLE_STATUSES:
            emit(f"{agent}: {cli_status.guidance_for(result.status)}")
        return result

    def _is_cancelled(self, cancel_event: object | None) -> bool:
        is_set = getattr(cancel_event, "is_set", None)
        return bool(is_set and is_set())
