from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path

from src import config
from src.models import CommandResult
from src.services import (
    agent_model_selector,
    claude_command,
    cli_execution_policy,
    secret_redactor,
)
from src.services.write_grant import WriteGrant

# CSI escape sequences, so a coloured one-word reply still reads as that word.
_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# Punctuation and markup a model may wrap a one-word answer in. Stripped from
# both ends of a line before comparing it with the expected reply.
_REPLY_DECORATION = " \t\"'`*_.!?。、．！？"

_RATE_LIMIT_SIGNALS = (
    "rate limit",
    "rate-limit",
    "rate_limit",
    "ratelimit",
    "rate limit exceeded",
    "rate_limit_exceeded",
    "too many requests",
    "quota exceeded",
    "quota_exceeded",
    "quota has been exceeded",
    "quota exhausted",
    "quota limit reached",
    "insufficient quota",
    "resource exhausted",
    "resource_exhausted",
    "usage limit reached",
    "usage limit exceeded",
)

_AUTH_REQUIRED_SIGNALS = (
    "auth_required",
    "authentication required",
    "authentication failed",
    "authentication error",
    "authentication_failure",
    "authentication_error",
    "authorization required",
    "authorization failed",
    "auth failed",
    "auth failure",
    "auth error",
    "not authenticated",
    "unauthenticated",
    "not logged in",
    "login required",
    "log in required",
    "sign in required",
    "please login",
    "please log in",
    "please sign in",
    "please authenticate",
    "login to continue",
    "log in to continue",
    "sign in to continue",
    "opening authentication page",
    "oauth token expired",
    "access token expired",
    "invalid authentication credentials",
    "invalid credentials",
    "missing credentials",
    "credentials missing",
    "invalid_grant",
    "unauthorized",
)


class ProcessRunner:
    def __init__(self, policy=None):
        self.policy = policy or cli_execution_policy.active()

    async def run(
        self,
        agent: str,
        command: list[str],
        cwd: Path,
        timeout_seconds: int = config.DEFAULT_TIMEOUT_SECONDS,
        used_rtk: bool = False,
        cancel_event: object | None = None,
        success_patterns: tuple[str, ...] = (),
        claude_spec: "claude_command.ClaudeRunSpec | None" = None,
        grant: "WriteGrant | None" = None,
    ) -> CommandResult:
        started = time.monotonic()
        if grant is not None and not config.IMPLEMENTATION_WRITES_ENABLED:
            # Gate 3 of three, immediately before launch. The earlier gates
            # live in policy and in grant construction; this one catches a
            # grant that reached here by any route at all, including a test
            # helper or a direct call.
            return CommandResult(
                agent=agent,
                command=command,
                ok=False,
                status="safety_blocked",
                stderr=(
                    "書き込み権限付きの実行要求を拒否しました: "
                    "config.IMPLEMENTATION_WRITES_ENABLED が False です。"
                ),
                elapsed_seconds=time.monotonic() - started,
                used_rtk=used_rtk,
            )
        if not config.is_agent_slot_enabled(agent):
            # Last-resort gate. Callers are expected to check
            # CliAdapters.command_exists() first, but a disabled slot must not
            # depend on every caller remembering to — nothing below this line
            # runs, so create_subprocess_exec is never reached.
            return CommandResult(
                agent=agent,
                command=command,
                ok=False,
                status="slot_disabled",
                stderr=(
                    f"The {agent} slot is disabled in this build and was not started. "
                    "See config.CLAUDE_SLOT_ENABLED and docs/safety-model.md for why and what re-enables it."
                ),
                elapsed_seconds=time.monotonic() - started,
                used_rtk=used_rtk,
            )
        child_env, isolated_config_dir = self._child_env(agent)

        # Everything from here on is inside try/finally, starting at the line
        # that created the directory: the pre-launch checks below can raise,
        # and an unexpected exception in any of them would otherwise leave a
        # temp profile directory behind for the life of the machine.
        try:
            if (
                self.policy.isolate_anthropic_config_dir
                and agent in config.AGENTS_REQUIRING_PROFILE_ISOLATION
                and isolated_config_dir is None
            ):
                # Never downgrade to "run anyway" on a predictable shared path:
                # a directory we didn't just create can't be shown to be empty,
                # ours, or free of a planted profile.
                return CommandResult(
                    agent=agent,
                    command=command,
                    ok=False,
                    status="safety_blocked",
                    stderr=(
                        "Could not create the private Anthropic profile-isolation directory, so "
                        "this run was blocked instead of starting without that isolation."
                    ),
                    elapsed_seconds=time.monotonic() - started,
                    used_rtk=used_rtk,
                )

            if agent == "claude":
                # The argv is built here, from a typed spec, rather than
                # trusted from the caller. That property predates the policy
                # change and outlives it: a caller able to pass raw arguments
                # could launch claude without --permission-mode plan, without
                # --tools "", or with bypassPermissions, which is about not
                # letting an AI edit the user's project.
                executable = shutil.which("claude") or "claude"
                if claude_spec is None:
                    return CommandResult(
                        agent=agent,
                        command=command,
                        ok=False,
                        status="security_blocked",
                        stderr=(
                            "The Claude slot requires a typed run spec; this run was not started."
                        ),
                        elapsed_seconds=time.monotonic() - started,
                        used_rtk=used_rtk,
                    )
                model_id, effort = claude_spec.model_id, claude_spec.effort
                if self.policy.apply_explicit_model:
                    model_id, effort = agent_model_selector.validated_model_and_effort(
                        "claude", model_id, effort
                    )
                    if model_id is None:
                        return CommandResult(
                            agent=agent,
                            command=command,
                            ok=False,
                            status="security_blocked",
                            stderr=(
                                "No confirmed subscription-safe model could be resolved for "
                                "claude. The run was blocked."
                            ),
                            elapsed_seconds=time.monotonic() - started,
                            used_rtk=used_rtk,
                        )
                # A grant addressed to a different agent must not widen here.
                # CliRunner already narrowed it; this is the last-resort check,
                # matching the posture of the slot gate above.
                claude_grant = (
                    grant
                    if (grant and grant.is_authentic and grant.agent == "claude")
                    else None
                )
                command = claude_command.build(
                    claude_command.ClaudeRunSpec(
                        prompt=claude_spec.prompt, model_id=model_id, effort=effort
                    ),
                    executable,
                    self.policy,
                    grant=claude_grant,
                )

            return await self._run_with_env(
                agent, command, cwd, timeout_seconds, used_rtk, cancel_event,
                success_patterns, child_env, started,
            )
        finally:
            self._discard_isolated_config_dir(isolated_config_dir)

    async def _run_with_env(
        self,
        agent: str,
        command: list[str],
        cwd: Path,
        timeout_seconds: int,
        used_rtk: bool,
        cancel_event: object | None,
        success_patterns: tuple[str, ...],
        child_env: dict[str, str],
        started: float,
        # A credential this particular run used, scrubbed from every captured
        # byte before a CommandResult exists. Always None since Phase E removed
        # the app-scoped token: the app now holds no credential of its own, and
        # anything inherited from the user's environment is caught by
        # secret_redactor's structural patterns instead of by being enumerated.
        secret: str | None = None,
    ) -> CommandResult:
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own process group so cancellation can kill the whole tree
                # (e.g. rtk's own child) instead of orphaning it. macOS/Linux only,
                # matching this app's supported OS.
                start_new_session=True,
            )
            stdout_chunks: list[bytes] = []
            stderr_chunks: list[bytes] = []
            interactive_event = asyncio.Event()
            success_event = asyncio.Event()
            stdout_task = asyncio.create_task(
                self._collect_output(process.stdout, stdout_chunks, interactive_event, success_event, success_patterns)
            )
            stderr_task = asyncio.create_task(
                self._collect_output(process.stderr, stderr_chunks, interactive_event, success_event, success_patterns)
            )
            wait_task = asyncio.create_task(process.wait())
            interactive_task = asyncio.create_task(interactive_event.wait())
            success_task = asyncio.create_task(success_event.wait())
            cancel_task = asyncio.create_task(self._wait_for_cancel(cancel_event))

            done, _pending = await asyncio.wait(
                {wait_task, interactive_task, success_task, cancel_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            elapsed = time.monotonic() - started

            if success_task in done and wait_task not in done:
                # The expected reply is in, but that is only one of the three
                # things preflight needs to know. Returning here used to report
                # returncode=0 for a process that was killed mid-run, so an exit
                # code was asserted that nobody had observed: a CLI that printed
                # the reply and *then* hit a rate limit, or exited non-zero, was
                # indistinguishable from a clean success. Wait out a bounded
                # window for the real ending and let the branches below judge it.
                grace = min(
                    config.PREFLIGHT_EXIT_GRACE_SECONDS,
                    max(0.0, timeout_seconds - (time.monotonic() - started)),
                )
                done, _pending = await asyncio.wait(
                    {wait_task, interactive_task, cancel_task},
                    timeout=grace,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                elapsed = time.monotonic() - started
                if not done:
                    await self._kill_process(process)
                    interactive_task.cancel()
                    cancel_task.cancel()
                    stdout_task.cancel()
                    stderr_task.cancel()
                    await self._finish_tasks(
                        wait_task, interactive_task, cancel_task, stdout_task, stderr_task
                    )
                    stderr = self._decode(stderr_chunks, secret)
                    never_exited = (
                        "The expected reply arrived, but the process was still running when the "
                        "preflight window closed and had to be stopped, so its exit status is unknown."
                    )
                    return CommandResult(
                        agent=agent,
                        command=command,
                        ok=False,
                        status="no_clean_exit",
                        stdout=self._decode(stdout_chunks, secret),
                        stderr=f"{stderr}\n{never_exited}".strip(),
                        returncode=process.returncode,
                        elapsed_seconds=elapsed,
                        used_rtk=used_rtk,
                    )

            if cancel_task in done and wait_task not in done:
                await self._kill_process(process)
                interactive_task.cancel()
                success_task.cancel()
                stdout_task.cancel()
                stderr_task.cancel()
                await self._finish_tasks(wait_task, interactive_task, success_task, stdout_task, stderr_task)
                return CommandResult(
                    agent=agent,
                    command=command,
                    ok=False,
                    status="cancelled",
                    stdout=self._decode(stdout_chunks, secret),
                    stderr=self._decode(stderr_chunks, secret) or "Cancelled by user.",
                    returncode=process.returncode,
                    elapsed_seconds=elapsed,
                    used_rtk=used_rtk,
                )

            if interactive_task in done and wait_task not in done:
                await self._kill_process(process)
                success_task.cancel()
                cancel_task.cancel()
                stdout_task.cancel()
                stderr_task.cancel()
                await self._finish_tasks(wait_task, success_task, cancel_task, stdout_task, stderr_task)
                stdout = self._decode(stdout_chunks, secret)
                stderr = self._decode(stderr_chunks, secret)
                status = self._classify(process.returncode or 1, stdout, stderr)
                if status == "failed":
                    status = "auth_required"
                return CommandResult(
                    agent=agent,
                    command=command,
                    ok=False,
                    status=status,
                    stdout=stdout,
                    stderr=stderr,
                    returncode=process.returncode,
                    elapsed_seconds=elapsed,
                    used_rtk=used_rtk,
                )

            if wait_task not in done:
                await self._kill_process(process)
                interactive_task.cancel()
                success_task.cancel()
                cancel_task.cancel()
                stdout_task.cancel()
                stderr_task.cancel()
                await self._finish_tasks(wait_task, interactive_task, success_task, cancel_task, stdout_task, stderr_task)
                stdout = self._decode(stdout_chunks, secret)
                stderr = self._decode(stderr_chunks, secret)
                timeout_message = f"Timed out after {timeout_seconds}s"
                status = self._classify(1, stdout, stderr)
                if status == "failed":
                    status = "timeout"
                return CommandResult(
                    agent=agent,
                    command=command,
                    ok=False,
                    status=status,
                    stdout=stdout,
                    stderr=f"{stderr}\n{timeout_message}".strip(),
                    elapsed_seconds=elapsed,
                    used_rtk=used_rtk,
                )

            interactive_task.cancel()
            success_task.cancel()
            cancel_task.cancel()
            await self._finish_tasks(interactive_task, success_task, cancel_task, stdout_task, stderr_task)
            stdout = self._decode(stdout_chunks, secret)
            stderr = self._decode(stderr_chunks, secret)
            returncode = wait_task.result()
            status = (
                self._preflight_status(returncode, stdout, stderr, success_patterns)
                if success_patterns
                else self._classify(returncode, stdout, stderr)
            )
            return CommandResult(
                agent=agent,
                command=command,
                ok=status == "ok",
                status=status,
                stdout=stdout,
                stderr=stderr,
                returncode=returncode,
                elapsed_seconds=elapsed,
                used_rtk=used_rtk,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started
            if process:
                await self._kill_process(process)
            return CommandResult(
                agent=agent,
                command=command,
                ok=False,
                status="timeout",
                stderr=f"Timed out after {timeout_seconds}s",
                elapsed_seconds=elapsed,
                used_rtk=used_rtk,
            )
        except asyncio.CancelledError:
            if process:
                await self._kill_process(process)
            raise
        except FileNotFoundError:
            return CommandResult(
                agent=agent,
                command=command,
                ok=False,
                status="command_missing",
                stderr=f"Command not found: {command[0]}",
                used_rtk=used_rtk,
            )
        except Exception as exc:
            elapsed = time.monotonic() - started
            return CommandResult(
                agent=agent,
                command=command,
                ok=False,
                status="error",
                stderr=secret_redactor.redact(str(exc), secret),
                elapsed_seconds=elapsed,
                used_rtk=used_rtk,
            )

    async def _wait_for_cancel(self, cancel_event: object | None) -> None:
        while True:
            is_set = getattr(cancel_event, "is_set", None)
            if is_set and is_set():
                return
            await asyncio.sleep(0.2)

    async def _collect_output(
        self,
        stream: asyncio.StreamReader | None,
        chunks: list[bytes],
        interactive_event: asyncio.Event,
        success_event: asyncio.Event,
        success_patterns: tuple[str, ...],
    ) -> None:
        if stream is None:
            return
        while True:
            chunk = await stream.read(1024)
            if not chunk:
                return
            chunks.append(chunk)
            if self._looks_interactive(chunk.decode(errors="replace")):
                interactive_event.set()
            if success_patterns and self._matches_success(self._decode(chunks), success_patterns):
                success_event.set()

    async def _kill_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            # Kill the whole process group (start_new_session=True made this
            # process its leader), so a wrapper like `rtk` can't leave its own
            # child (claude/codex/agy) running as an orphan after cancellation.
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                process.kill()
            except ProcessLookupError:
                return
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except Exception:
            pass

    async def _finish_tasks(self, *tasks: asyncio.Task) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    def _decode(self, chunks: list[bytes], secret: str | None = None) -> str:
        """Decode captured output, scrubbing the credential this run used.

        Redacting here rather than at each display or save site means the
        CommandResult is already clean by the time anything downstream sees
        it — persistence under .ai-brainstorm/, the GUI panes, the text fed
        back to LM Studio, and the chat history all inherit it for free."""
        text = b"".join(chunks).decode(errors="replace")
        return secret_redactor.redact(text, secret)

    def _looks_interactive(self, text: str) -> bool:
        lowered = text.lower()
        lines = [line.strip() for line in lowered.splitlines() if line.strip()]
        recent_lines = lines[-5:]
        direct_prompts = (
            "enter verification code",
            "enter auth code",
            "press enter to open",
            "trust this directory",
        )
        yes_no_prompts = ("[y/n]", "(y/n)", "(yes/no)")
        for line in recent_lines:
            if len(line) > 220:
                continue
            if any(prompt in line for prompt in direct_prompts):
                return True
            if line.endswith(yes_no_prompts):
                return True
            if "opening authentication page" in line and ("http" in line or "browser" in line):
                return True
        return False

    def _matches_success(self, text: str, patterns: tuple[str, ...]) -> bool:
        """Whether the expected reply appears as a line of its own.

        Line-anchored rather than a substring search, so a banner that merely
        contains the word cannot pass. Normalized rather than exact, so the
        decoration a model puts around a one-word answer — "OK.", "**OK**",
        a colour code — doesn't fail a CLI that answered correctly.

        A line carrying anything else ("OK, ready to help") deliberately does
        not match. Once the reply is prose, it no longer distinguishes a
        working CLI from a stuck or logged-out one, which also produces prose.
        """
        expected = {pattern.strip().lower() for pattern in patterns if pattern.strip()}
        if not expected:
            return False
        for line in _ANSI_ESCAPE.sub("", text).splitlines():
            if line.strip().lower().strip(_REPLY_DECORATION) in expected:
                return True
        return False

    def _preflight_status(
        self,
        returncode: int | None,
        stdout: str,
        stderr: str,
        success_patterns: tuple[str, ...],
    ) -> str:
        """READY only when the whole run agrees, not just the exit code.

        Preflight asks one throwaway question whose only job is to show the CLI
        can answer right now, so the ending is checked as a whole: the process
        exited on its own, it exited 0, its output carries no negative signal,
        and the answer is actually in there.

        The last condition is what separates "the CLI launched" from "the model
        replied". A CLI can print a startup banner ("Update available") and exit
        0 without ever reaching the model — on a logged-out machine that is a
        realistic ending, and accepting it would send the real request to a slot
        that cannot serve it.
        """
        status = self._classify(returncode, stdout, stderr, strict=True)
        if status != "ok":
            return status
        if not stdout.strip() and not stderr.strip():
            return "empty_response"
        if not (
            self._matches_success(stdout, success_patterns)
            or self._matches_success(stderr, success_patterns)
        ):
            return "no_expected_reply"
        return "ok"

    def _classify(self, returncode: int | None, stdout: str, stderr: str, strict: bool = False) -> str:
        text = f"{stdout}\n{stderr}".lower()
        if returncode == 0:
            # `strict` is only set by preflight (which passes success_patterns and
            # expects a tiny constrained "OK" reply): a CLI that exits 0 without
            # ever emitting the expected pattern may still have failed silently
            # (e.g. printed "Please login" and exited 0). Free-form main-request
            # output is NOT checked here, since these substring heuristics would
            # false-positive on a normal answer that happens to mention "login"/"auth".
            if strict:
                return self._negative_signal_status(text) or "ok"
            return "ok"
        return self._negative_signal_status(text) or "failed"

    def _negative_signal_status(self, text: str) -> str | None:
        deprecated_client = "deprecated" in text and any(
            marker in text for marker in ("gemini", "antigravity", "code assist", "unsupported client")
        )
        if (
            "permission deny rule" in text
            or "matches no known tool" in text
            or "unknown option" in text
            or "unrecognized argument" in text
        ):
            return "config_error"
        if (
            "unsupported_client" in text
            or "ineligibletiererror" in text
            or "no longer supported" in text
            or "ineligible tier" in text
            or "migrate to antigravity" in text
            or "this client is no longer supported" in text
            or deprecated_client
        ):
            return "unsupported_client"
        if (
            "operation not permitted" in text
            or "failed to initialize in-process app-server client" in text
            or "permission denied" in text
        ):
            return "permission_error"
        if (
            any(signal in text for signal in _RATE_LIMIT_SIGNALS)
            or re.search(r"\b(?:http|status|error)\s*[:=]?\s*429\b", text)
            or re.search(r"[\"']code[\"']\s*:\s*429\b", text)
        ):
            return "rate_limited"
        if (
            "api key not found" in text
            or "missing api key" in text
            or "api key required" in text
            or "google ai studio key required" in text
            or "requires an api key" in text
        ):
            return "api_key_blocked"
        if (
            any(signal in text for signal in _AUTH_REQUIRED_SIGNALS)
            or re.search(r"\b(?:http|status|error)\s*[:=]?\s*401\b", text)
            or re.search(r"[\"']code[\"']\s*:\s*401\b", text)
        ):
            return "auth_required"
        return None

    def _child_env(self, agent: str = "") -> tuple[dict[str, str], str | None]:
        """Returns the child environment plus the path of any throwaway
        ANTHROPIC_CONFIG_DIR created for it, which the caller must discard when
        the child exits.

        Under existing_config the environment is inherited untouched: the API
        keys, provider selections, and cloud credentials the user has set are
        theirs, and stripping them was an attempt to force a billing path this
        app was never able to guarantee anyway. What the app still owes them is
        that it does not *print* those values — that is redaction's job, not
        this function's, and the two requirements are independent.

        The second element is None when isolation wasn't established. For an
        agent in AGENTS_REQUIRING_PROFILE_ISOLATION under a policy that
        requires isolation, that means the run must be blocked — there is
        deliberately no fallback path here, because any path we didn't just
        create can't be shown to be empty and ours."""
        env = os.environ.copy()

        if self.policy.inherit_user_environment:
            return env, None

        for name in config.BLOCKED_CHILD_ENV_VARS:
            env.pop(name, None)
        env["AI_BRAINSTORM_SUBSCRIPTION_ONLY"] = "1"

        if agent and agent not in config.AGENTS_REQUIRING_PROFILE_ISOLATION:
            # ANTHROPIC_CONFIG_DIR is Claude Code specific; don't create or
            # override anything for agents it doesn't apply to.
            return env, None

        isolated_config_dir = self._new_isolated_anthropic_config_dir()
        if isolated_config_dir is not None:
            env["ANTHROPIC_CONFIG_DIR"] = isolated_config_dir

        # An app-scoped CLAUDE_CODE_OAUTH_TOKEN used to be injected here.
        # Phase E deleted the store it came from, so the strict policy now
        # strips the inherited token (it is in BLOCKED_CHILD_ENV_VARS) without
        # putting one back — which is the honest end state: this app holds no
        # credential of its own.
        return env, isolated_config_dir

    def _new_isolated_anthropic_config_dir(self) -> str | None:
        """Point ANTHROPIC_CONFIG_DIR at a fresh private directory rather than
        removing it: removing it just falls back to the default
        ~/.config/anthropic, where a user's profile or federation credentials
        may live — and those outrank the /login subscription credential.

        A newly created mkdtemp() directory is, by construction, empty, not a
        symlink, owned by us, and mode 0700 — so there is no profile and no
        active_config file for the CLI to pick up, and no shared path another
        process could have planted one in. Returns None if creation fails —
        the caller then leaves ANTHROPIC_CONFIG_DIR unset and refuses to run
        any agent that requires this isolation, rather than falling back to a
        path it doesn't own."""
        try:
            return tempfile.mkdtemp(prefix="ai-brainstorm-anthropic-isolation-")
        except OSError:
            return None

    def _discard_isolated_config_dir(self, path: str | None) -> None:
        if not path:
            return
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass  # best-effort cleanup; never mask the run's own outcome
