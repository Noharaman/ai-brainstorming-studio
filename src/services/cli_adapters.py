from __future__ import annotations

import shutil

from src import config
from src.services import (
    agent_model_selector,
    claude_command,
    cli_execution_policy,
)
from src.services.agent_model_selector import AgentSelection
from src.services.write_grant import WriteGrant, grant_for


#: Codex customizations disabled on every invocation.
#:
#: `codex features list` reports hooks, plugins, apps, browser_use and
#: computer_use as stable and enabled by default. Hooks run as ordinary child
#: processes outside the model sandbox, so `--sandbox read-only` does not
#: contain them; the rest widen what a run can reach (a browser, the desktop)
#: well beyond the folder the user approved. None of it is something this app
#: asked for, and none of it appears in the diff.
_CODEX_DISABLED_FEATURES = (
    "--disable", "hooks",
    "--disable", "plugins",
    "--disable", "apps",
    "--disable", "browser_use",
    "--disable", "browser_use_external",
    "--disable", "browser_use_full_cdp_access",
    "--disable", "computer_use",
)


class CliAdapters:
    def __init__(self, prefer_rtk: bool = True, policy=None):
        self.policy = policy or cli_execution_policy.active()
        self.prefer_rtk = prefer_rtk
        self.rtk_path = shutil.which("rtk")
        self.agy_path = shutil.which("agy")
        self.legacy_gemini_path = shutil.which("gemini")

    def claude_spec(
        self,
        prompt: str,
        agent_selection: AgentSelection | None,
        catalog: dict | None = None,
    ) -> claude_command.ClaudeRunSpec:
        """The typed input ProcessRunner needs to build a Claude argv itself."""
        if not self.policy.apply_explicit_model:
            return claude_command.ClaudeRunSpec(prompt=prompt)
        model_id, effort = agent_selection.for_agent("claude") if agent_selection else (None, None)
        model_id, effort = agent_model_selector.validated_model_and_effort(
            "claude", model_id, effort, catalog
        )
        return claude_command.ClaudeRunSpec(prompt=prompt, model_id=model_id, effort=effort)

    def build_commands(
        self,
        prompts: dict[str, str],
        automation_level: int = 2,
        agent_selection: AgentSelection | None = None,
        grant: WriteGrant | None = None,
        run_id: str = "",
    ) -> tuple[dict[str, list[str]], list[str]]:
        warnings: list[str] = []
        if self.policy.wrap_ai_cli_in_rtk and self.prefer_rtk and not self.rtk_path:
            warnings.append("RTK not found. Running without token-saving wrapper if CLI exists.")
        if grant is not None:
            warnings.append(
                "承認済みの実装フェーズです: " + grant.describe()
            )
        if "claude" in prompts and not self.command_exists("claude"):
            if not config.CLAUDE_SLOT_ENABLED:
                warnings.append("The Claude slot is switched off in this build; it will be skipped.")
            elif not shutil.which("claude"):
                warnings.append("Claude CLI (`claude`) not found; the Claude slot will be skipped.")
            else:
                warnings.append(
                    "Claude CLI (`claude`) found, but agent_models.json has no confirmed safe default "
                    "for claude (file missing/corrupt, or no billing_status=subscription_safe model "
                    "flagged selector_default). This app never runs claude without an explicit, "
                    "verified --model, so the Claude slot stays disabled until the catalog is fixed."
                )
        if "gemini" in prompts and not self.command_exists("gemini"):
            if not self.agy_path:
                if self.legacy_gemini_path:
                    warnings.append(
                        "Antigravity CLI (`agy`) not found; legacy `gemini` CLI was detected at "
                        f"{self.legacy_gemini_path} but will not run automatically because it cannot "
                        "guarantee plan/read-only mode. Install/login `agy` to enable the Antigravity slot."
                    )
                else:
                    warnings.append("Antigravity CLI (`agy`) not found; the Antigravity slot will be skipped.")
            else:
                warnings.append(
                    "Antigravity CLI (`agy`) found, but no gemini model in agent_models.json is yet "
                    "confirmed billing_status=subscription_safe and flagged selector_default. This app "
                    "cannot verify Antigravity's 'Use AI Credits' overage-billing setting, so the Antigravity "
                    "slot stays disabled until a human review confirms at least one model is safe."
                )
        commands: dict[str, list[str]] = {}
        catalog: dict | None = None
        for agent, prompt in prompts.items():
            model_id, effort = agent_selection.for_agent(agent) if agent_selection else (None, None)
            if self.policy.apply_explicit_model:
                if agent in ("claude", "gemini", "codex"):
                    if catalog is None:
                        catalog = agent_model_selector.load_catalog()
                    model_id, effort = self._validated_model_and_effort(agent, model_id, effort, catalog)
            else:
                if model_id and agent in ("claude", "gemini", "codex"):
                    pass
                else:
                    model_id, effort = None, None
            # grant_for() re-checks agent and run: a grant issued to the
            # implementer must not widen to the critic sharing this loop.
            agent_grant = grant_for(grant, agent, run_id)
            base = self._base_command(agent, prompt, model_id, effort, agent_grant)
            # Ask uses_rtk() rather than recomputing the rule: a second copy of
            # it here is how the policy got bypassed for gemini/codex.
            commands[agent] = (["rtk"] + base) if self.uses_rtk(agent) else base
        return commands, warnings

    def _validated_model_and_effort(
        self,
        agent: str,
        model_id: str | None,
        effort: str | None,
        catalog: dict,
    ) -> tuple[str | None, str | None]:
        """Last line of defense before a command is built. Delegates to the
        shared rule so ProcessRunner's pre-launch re-check can't diverge."""
        return agent_model_selector.validated_model_and_effort(agent, model_id, effort, catalog)

    def command_exists(self, agent: str, catalog: dict | None = None) -> bool:
        # Applies to every agent, not just claude, so disabling a different
        # slot later needs no change here. ProcessRunner.run() enforces the
        # same predicate as a last resort — this is the early exit, not the
        # only one. See config.CLAUDE_SLOT_ENABLED for what's disabled today.
        if not config.is_agent_slot_enabled(agent):
            return False
        if agent == "claude":
            if not shutil.which("claude"):
                return False
            if not self.policy.gate_availability_on_billing:
                # Installed and logged in is the user's business. A logged-out
                # claude still runs and reports auth_required, which is a
                # fixable message; reporting it as unavailable here is not.
                return True
            # Authentication is deliberately NOT checked here. It depends on
            # the runtime environment (the app-scoped token has to be in the
            # child env for the probe to see what the run will see), and
            # duplicating that here made the pre-flight answer differ from the
            # launch gate's — on a token-only machine it said "no" and the
            # correct gate in ProcessRunner.run() was never reached.
            # A missing/corrupt agent_models.json means this app can't hand
            # claude a verified-safe --model — running it on the CLI's own
            # local default (which may itself be a credit-requiring model a
            # user picked in an unrelated interactive session) is exactly
            # the fail-open scenario this catalog exists to prevent. Same
            # fail-closed posture as gemini below.
            return self._has_a_confirmed_default(agent, catalog)
        if agent == "gemini":
            # Legacy `gemini` is detected (for the warning above) but never
            # auto-run: it cannot guarantee plan/read-only mode like `agy` does.
            # That exclusion is about read-only safety, not billing, so it
            # survives the policy change.
            if not self.agy_path:
                return False
            if not self.policy.gate_availability_on_billing:
                return True
            # `agy` being installed only means Antigravity CLI exists — it
            # says nothing about whether Antigravity's "Use AI Credits"
            # overage-billing toggle is off. This app has no way to verify
            # that setting, so the Antigravity slot stays disabled until a human
            # has reviewed at least one model as billing_status=
            # subscription_safe AND flagged it selector_default.
            return self._has_a_confirmed_default(agent, catalog)
        command = self._command_name(agent)
        return bool(shutil.which(command))

    def skip_reason(self, agent: str) -> tuple[str, str]:
        """Why command_exists() said no, as (status, human message). Reporting
        "CLI not found" for an installed CLI would send someone reinstalling
        software that isn't the problem."""
        if not config.is_agent_slot_enabled(agent):
            return "slot_disabled", f"the {agent} slot is switched off in this build."
        return "command_missing", "CLI executable was not found."

    def _has_a_confirmed_default(self, agent: str, catalog: dict | None) -> bool:
        """True only if the catalog has exactly one confirmed-default model
        for `agent` — i.e. there is a model this app can always pass as an
        explicit --model, so a chair-selection failure (or no selection at
        all) never falls through to the CLI's own unverified local default."""
        if catalog is None:
            catalog = agent_model_selector.load_catalog()
        return agent_model_selector.has_confirmed_default(agent, catalog)

    def using_rtk(self) -> bool:
        """Whether rtk is available at all. For whether a *specific* agent
        goes through it, use uses_rtk(agent) — claude never does."""
        return self.prefer_rtk and bool(self.rtk_path)

    def uses_rtk(self, agent: str) -> bool:
        # Under existing_config no AI CLI goes through rtk: it saves 0% on all
        # three (it compacts tool output, not model answers) and persists the
        # full command line — the whole prompt — to its history database.
        if not self.policy.wrap_ai_cli_in_rtk:
            return False
        return self.using_rtk() and agent not in config.AGENTS_NEVER_WRAPPED

    def _base_command(
        self,
        agent: str,
        prompt: str,
        model_id: str | None = None,
        effort: str | None = None,
        grant: WriteGrant | None = None,
    ) -> list[str]:
        # Same posture as claude_command.build(): a grant is honoured only if
        # it came from approval AND names the agent being built. Checking
        # authenticity alone let a grant for one agent open another.
        if grant is not None and not (grant.is_authentic and grant.agent == agent):
            grant = None
        if agent == "claude":
            executable = shutil.which("claude") or "claude"
            return claude_command.build(
                claude_command.ClaudeRunSpec(prompt=prompt, model_id=model_id, effort=effort),
                executable,
                self.policy,
                grant=grant,
            )
        if agent == "gemini":
            # `--sandbox` (terminal restrictions) is kept in both modes: an
            # approved implementation may edit files, but this app never
            # promises it may run unrestricted shell commands.
            mode = "accept-edits" if grant else "plan"
            command = [
                "agy",
                "--mode",
                mode,
                "--sandbox",
                # Stops slash-command and skill expansion in print mode, so a
                # prompt (or content the model read) cannot invoke a skill the
                # user happens to have installed.
                #
                # Scope note: this covers skill expansion and nothing else. It
                # is NOT the reason MCP servers or plugins would be safe —
                # those load independently and act outside the terminal
                # sandbox, which is why the slot is closed
                # (config.ANTIGRAVITY_SLOT_ENABLED).
                "--disable-slash-commands",
            ]
            if model_id:
                command += ["--model", model_id]
            if effort:
                command += ["--effort", effort]
            command += ["-p", prompt]
            return command
        if agent == "codex":
            command = [
                "codex",
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                # workspace-write confines edits to the working directory.
                # danger-full-access is never emitted by this app.
                "workspace-write" if grant else "read-only",
                # Session hygiene, not a config override: nothing of this run
                # is persisted to the user's session store.
                "--ephemeral",
                # Codex runs hooks too, and `codex features list` shows hooks,
                # plugins and apps as stable and ON by default. A trusted
                # SessionStart hook is a normal child process with the user's
                # rights, so --sandbox read-only does not stop it: the same
                # hole that --safe-mode closes for claude. Disabled on EVERY
                # codex run, granted or not.
                *_CODEX_DISABLED_FEATURES,
                # Never let the model escalate out of the sandbox by asking.
                "-c",
                'approval_policy="never"',
                # execpolicy rules are a separate layer from the sandbox: a
                # rule in the user's or the project's `.rules` can permit a
                # command to run outside it, and approval_policy="never" means
                # nothing prompts when that happens. Previously this was only
                # applied under the retired strict policy.
                "--ignore-rules",
                # Fail closed on a config key this codex version does not know.
                # Applied to every run, not just granted ones: the read-only
                # runs depend on these flags being understood too.
                "--strict-config",
            ]
            if grant:
                # workspace-write is not "the folder the user approved" by
                # itself: ~/.codex/config.toml can add writable_roots, and
                # /tmp and $TMPDIR are writable unless excluded. Those are the
                # user's settings for their own interactive use; they are not
                # what this app showed on the approval screen, and edits there
                # would not appear in the diff. Pinned per invocation (-c
                # overrides config.toml) so the sandbox matches what was
                # approved.
                #
                # --strict-config makes codex fail on a key it does not
                # recognise. That is deliberate and it is the fail-closed
                # choice: if a future codex renames one of these keys, the
                # right outcome is a broken implementation run the user can
                # see, not a silently wider write boundary. An earlier version
                # of this omitted it so runs would "degrade gracefully" — that
                # degradation was into unrestricted writes.
                #
                # NOT YET VERIFIED against a real codex run — see
                # docs/safety-model.md.
                command += [
                    "--cd",
                    str(grant.project_root),
                    "-c",
                    "sandbox_workspace_write.writable_roots=[]",
                    "-c",
                    "sandbox_workspace_write.network_access=false",
                    "-c",
                    "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                    "-c",
                    "sandbox_workspace_write.exclude_slash_tmp=true",
                ]
            if model_id:
                command += ["-m", model_id]
            if effort:
                command += ["-c", f'model_reasoning_effort="{effort}"']
            if not self.policy.inherit_user_cli_config:
                # Legacy strict mode: refuse the user's own config so billing
                # can be forced onto the ChatGPT subscription path. Under
                # existing_config none of this is applied — ~/.codex/config.toml,
                # the user's execpolicy .rules, their login method, provider and
                # endpoints are all left exactly as they set them.
                command += [
                    "--ignore-user-config",
                    "--ignore-rules",
                    "-c",
                    'forced_login_method="chatgpt"',
                    "-c",
                    'model_provider="openai"',
                    "-c",
                    'openai_base_url="https://chatgpt.com/backend-api/codex"',
                    "-c",
                    'chatgpt_base_url="https://chatgpt.com/backend-api/"',
                ]
            command.append(prompt)
            return command
        raise ValueError(f"Unknown agent: {agent}")

    def _command_name(self, agent: str) -> str:
        if agent == "gemini":
            return "agy"
        if agent in {"claude", "codex"}:
            return agent
        raise ValueError(f"Unknown agent: {agent}")
