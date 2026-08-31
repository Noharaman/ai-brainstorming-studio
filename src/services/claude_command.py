"""The only place a Claude Code command line is assembled.

`ProcessRunner` builds the argv itself from a `ClaudeRunSpec` rather than
accepting a `list[str]`, so that a caller cannot launch claude without
`--permission-mode plan`, without `--tools ""`, or with `bypassPermissions`.
That is about not letting an AI edit the user's project, and it holds no
matter which execution policy is active. Building here means the read-only
flags cannot be omitted, reordered away, or overridden by a trailing
duplicate.

Since 2026-08-31 there is exactly one way out of plan mode: passing a
`WriteGrant` that names this agent and this run. No grant means plan mode, so
every existing caller — and every future one that does not know about grants —
keeps the read-only argv. `bypassPermissions` is still never emitted: an
approved implementation runs with `acceptEdits`, which edits files but still
refuses the destructive operations this app promises not to perform.
"""
from __future__ import annotations

from dataclasses import dataclass

from src import config
from src.services import cli_execution_policy
from src.services.write_grant import WriteGrant


@dataclass(frozen=True)
class ClaudeRunSpec:
    """Everything a caller is allowed to influence about a Claude run."""

    prompt: str
    model_id: str | None = None
    effort: str | None = None


def build(
    spec: ClaudeRunSpec,
    executable: str,
    policy=None,
    grant: WriteGrant | None = None,
) -> list[str]:
    """The argv for one Claude run.

    `executable` must already be the resolved path, so the caller decides once
    which binary runs. The prompt goes last, after `-p`, so nothing in it can
    be read as a flag.

    Without a `grant` the constraints below are unconditional: plan mode, no
    tools, no session persistence, non-interactive. They are about not letting
    an AI edit the user's project, which has nothing to do with billing and
    does not change with the policy. Only the settings-isolation flags do —
    those exist to deny the user's own configuration, which existing_config
    permits.

    With a grant, plan mode becomes `acceptEdits` and the default tool set is
    enabled. The caller is responsible for having checked that the grant names
    this agent and this run (see `write_grant.grant_for`).
    """
    if policy is None:
        policy = cli_execution_policy.active()
    # --safe-mode goes on EVERY claude run, granted or not, and before the
    # mode flags so it cannot be read as their argument.
    #
    # Plan mode and --tools "" stop the *model* from using tools. They do not
    # stop hooks. `claude -p` treats the folder as trusted and runs whatever
    # the target project's .claude/settings.json declares — a SessionStart
    # hook is arbitrary shell with the user's full rights, so a read-only
    # consultation at level 1 could still rewrite files. That defeats the
    # whole "no grant means no writes" property, so it is closed here rather
    # than at the call sites.
    #
    # --safe-mode rather than --bare: --bare also skips hooks, but forces auth
    # to ANTHROPIC_API_KEY only, which would push this app onto API-key
    # billing and ignore the user's existing login — both things this project
    # promises not to do. --safe-mode disables hooks, plugins, MCP servers,
    # skills and CLAUDE.md discovery while leaving auth, model selection and
    # permissions working normally.
    #
    # Not a complete seal: admin-managed (policy) settings still apply, and
    # this app cannot disable those. See docs/safety-model.md.
    command = [executable, "--safe-mode"]
    # A grant that did not come from the approval path, or that names another
    # agent, is not a grant here. This is the last builder in the chain, so it
    # re-checks rather than trusting that every caller narrowed it with
    # write_grant.grant_for() — a codex grant handed to this function used to
    # produce acceptEdits.
    if grant is not None and not (grant.is_authentic and grant.agent == "claude"):
        grant = None
    if grant is None:
        command += [
            "--permission-mode",
            "plan",
            "--tools",
            "",
        ]
    else:
        command += [
            "--permission-mode",
            "acceptEdits",
            "--tools",
            "default",
            # Adds the approved root to the accessible set; it does not
            # restrict claude to it. Anything the user's own settings already
            # allow stays allowed, which is why the sandboxing gap is tracked
            # as an open task rather than claimed as solved.
            "--add-dir",
            str(grant.project_root),
        ]
    if not policy.inherit_user_cli_config:
        command += list(config.CLAUDE_SETTINGS_ISOLATION_ARGS)
    if spec.model_id:
        command += ["--model", spec.model_id]
    if spec.effort:
        command += ["--effort", spec.effort]
    command += ["--no-session-persistence", "-p", spec.prompt]
    return command
