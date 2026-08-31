"""How much of the user's own CLI setup this app is allowed to override.

Every question of the form "should we replace what the user configured?" is
answered here, in one object, rather than by booleans scattered through
config.py. Exactly one policy is active at a time: availability checks,
preflight, and the real launch all read the same one, so the health badge
cannot promise something the launch then refuses (a failure mode this project
has already had to remove three times).

`existing_config` is the product direction (see docs/decisions.md, 2026-08-16):
launch each CLI the way its owner set it up. `strict_subscription` is the
previous design, kept as reversible scaffolding so the difference between the
two stays testable rather than only described. It is not selectable at runtime
and nothing chooses it but the tests. Phase E removed the modules that made it
whole (the token store and the auth guard), so what remains of it is the
environment stripping, the settings isolation, and the model gating.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CliExecutionPolicy:
    name: str

    # Keep the parent environment as-is, including API keys and cloud-provider
    # variables. Stripping them was an attempt to force subscription billing;
    # it also silently broke setups the user had deliberately chosen.
    inherit_user_environment: bool

    # Leave each CLI's own config files loaded: no `--ignore-user-config`,
    # no `--setting-sources ""`, no forced login method or pinned provider.
    inherit_user_cli_config: bool

    # Point ANTHROPIC_CONFIG_DIR at a throwaway directory per run.
    isolate_anthropic_config_dir: bool

    # Refuse to run an installed CLI when agent_models.json has no model
    # marked billing-safe for it.
    gate_availability_on_billing: bool

    # Pass a `--model` chosen by the chair. Under existing_config the model is
    # the CLI's business unless the *user* picks an override (Phase D).
    apply_explicit_model: bool

    # Route AI CLI invocations through `rtk`. Measured 2026-08-16: 0% savings
    # for all three CLIs, and rtk persists the full command line — and so the
    # whole prompt — to its history database. See docs/decisions.md.
    wrap_ai_cli_in_rtk: bool


EXISTING_CONFIG = CliExecutionPolicy(
    name="existing_config",
    inherit_user_environment=True,
    inherit_user_cli_config=True,
    isolate_anthropic_config_dir=False,
    gate_availability_on_billing=False,
    apply_explicit_model=False,
    wrap_ai_cli_in_rtk=False,
)

STRICT_SUBSCRIPTION = CliExecutionPolicy(
    name="strict_subscription",
    inherit_user_environment=False,
    inherit_user_cli_config=False,
    isolate_anthropic_config_dir=True,
    gate_availability_on_billing=True,
    apply_explicit_model=True,
    wrap_ai_cli_in_rtk=True,
)

_ACTIVE = EXISTING_CONFIG


def active() -> CliExecutionPolicy:
    """The policy every caller uses unless a test injects another one."""
    return _ACTIVE
