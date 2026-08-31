from pathlib import Path

APP_DIR_NAME = ".ai-brainstorm"
LM_STUDIO_BASE_URL = "http://localhost:1234/v1"

# The chair model is pinned by name. Before this, ChairAgent used whatever
# /v1/models happened to list first, so loading a different model in LM Studio
# silently swapped the chair out with nothing in the app recording which model
# was actually consulted. Set to "" to deliberately accept whatever is loaded
# (the old behaviour) — the app then cannot tell you which model chaired a run.
LM_STUDIO_CHAIR_MODEL = "qwen/qwen3.8-27b"

# Sent verbatim as the OpenAI-compatible `reasoning_effort` field; "" omits it.
#
# This is not a tuning preference, it is what makes the pinned model usable.
# qwen3.8-27b defaults to maximum reasoning effort: measured on an M5 Pro it
# spent its entire 1800-token budget on reasoning and returned an *empty*
# message, which the app reads as "chair unavailable" and silently degrades to
# the no-chair fallback — while LM Studio is running normally. It also took
# 119s, over the chat timeout. With reasoning off the same prompt takes 46-66s
# and produces equivalent output (1801 vs 1818 characters, all seven required
# sections either way). The in-prompt `/no_think` tag does NOT work; only this
# API field does.
#
# Models with no reasoning mode accept and ignore the field (verified against
# gemma-4-12b-qat, which returns HTTP 200 either way), so this is safe to send
# unconditionally.
LM_STUDIO_CHAIR_REASONING_EFFORT = "none"

# Only used when LM_STUDIO_CHAIR_MODEL is "" and /v1/models reports nothing
# usable. Never a model that exists — it is a last-resort label.
DEFAULT_LM_STUDIO_MODEL = "local-model"
DEFAULT_TIMEOUT_SECONDS = 180
PREFLIGHT_TIMEOUT_SECONDS = 45
# How long preflight waits for a CLI to exit on its own *after* the expected
# reply has already arrived. Without this the run was killed at the first
# matching line and reported as exit 0 — an exit code nobody ever observed.
# It is a shortcut off the full PREFLIGHT_TIMEOUT_SECONDS, not a second
# budget: the grace window is clipped to whatever time is left.
PREFLIGHT_EXIT_GRACE_SECONDS = 10
# Measured with the pinned chair model on an M5 Pro (48GB), using the app's
# real prompts: the integrate call (1800 tokens) took 46.6s and 65.5s across
# runs; the context-pack call (1200 tokens) took 21.7s. The old 90s/45s pair
# left the context call at ~2x its measured cost, which a slightly longer
# project scan would have eaten.
#
# Tradeoff to be aware of: ChairAgent.chat() cannot abort an HTTP request that
# is already in flight, so raising these also lengthens the window in which a
# cancellation is requested but not yet acted on.
LM_STUDIO_CHAT_TIMEOUT_SECONDS = 120
LM_STUDIO_CONTEXT_TIMEOUT_SECONDS = 60
MAX_CONTEXT_PACK_CHARS = 8000
MAX_FILE_READ_CHARS = 8000
MAX_TREE_ITEMS = 300
MAX_REFINEMENT_ROUNDS = 1

# How often BrainstormApp re-checks CLI versions / re-syncs the Antigravity
# model list while the app stays open. These are cheap local subprocess calls
# (--version, agy models), so cost isn't the limiter — version bumps for these
# CLIs happen on the order of days, not minutes, so nothing is gained by
# polling faster. 30 minutes catches an update within any normal multi-hour
# session without adding noticeable background noise.
MODEL_CATALOG_CHECK_INTERVAL_SECONDS = 1800

# The blunt kill switch for the Claude slot. Under Existing CLI Mode nothing
# else gates it: whether Claude can run is a question about the user's own
# login, which the app reads no credential to answer — a logged-out CLI
# reports auth_required from the run itself. Set this to False to keep claude
# out of a build entirely.
CLAUDE_SLOT_ENABLED = False

# The Codex slot is off until MCP servers can be provably isolated per run.
#
# Codex reads top-level `mcp_servers.<id>` entries from the user's config, and
# MCP tools act outside the shell sandbox: an enabled server with filesystem or
# network access can change files and send data with no approval prompt, and
# nothing it does shows up in the git diff. `--disable plugins` covers
# plugin-provided servers only.
#
# Measured on codex-cli 0.149.0 (2026-08-31):
#   - `-c mcp_servers={}` does NOT clear the table: 7 enabled servers before,
#     7 after.
#   - Per-server `-c mcp_servers.<id>.enabled=false` works for a plain name
#     (node_repl: 7 -> 6) but breaks the whole config for a hyphenated one
#     ("Error: failed to load bootstrap configuration ... invalid transport in
#     `mcp_servers.cloudflare-api`"), because the override replaces the
#     server's table instead of merging into it.
#   - Pointing CODEX_HOME at a throwaway directory would remove the servers,
#     but it also removes the user's login — the same trade this project
#     refuses for claude's --bare.
#
# So this app cannot promise that an approved implementation stays inside the
# approved folder while codex runs. Fail closed rather than ship a boundary we
# cannot enforce. Re-enable once per-run MCP isolation is demonstrated; see
# docs/safety-model.md.
CODEX_SLOT_ENABLED = False

# All three AI CLI slots are off; the app runs on LM Studio alone.
#
# Claude is closed for a different reason than the other two. Its hooks can be
# disabled with --safe-mode (verified against the real CLI), but
# admin-managed policy settings survive that flag and this app has no way to
# detect or refuse them. So OS-level read-only cannot be guaranteed for
# claude either, and shipping it while codex and Antigravity are closed for an
# unproven boundary would be applying two different standards to the same
# question.
#
# What remains is a local-consultation build: LM Studio answers, no external
# AI CLI is launched. That is a smaller product than the three-way comparison
# this app was for, and the docs say so plainly.

# The Antigravity slot is off for the same reason as codex: this app cannot
# show that a run is isolated from the user's MCP servers and plugins.
#
# `agy --help` (1.1.22) offers `agy mcp` and `agy plugin` for *managing* those
# — which edits the user's configuration, something this app will not do — but
# no per-invocation flag that disables them for one run. `--sandbox` restricts
# the terminal, and `--disable-slash-commands` stops skill expansion; neither
# covers an MCP server with filesystem or network access, which runs outside
# the shell sandbox and leaves nothing in the git diff.
#
# Leaving it enabled while codex is closed for the identical, unproven risk
# would be inconsistent rather than pragmatic. Re-enable once per-run
# isolation is demonstrated; see docs/safety-model.md.
#
# The internal agent name is "gemini" (the slot predates the rename to
# Antigravity), so this gates is_agent_slot_enabled("gemini").
ANTIGRAVITY_SLOT_ENABLED = False

# Master switch for letting an AI edit the user's files. Off.
#
# The write path is built and tested, but it depends on confining the CLI
# process, and no OS sandbox backend exists yet (see process_sandbox.py for
# what was measured). Without one, "the AI may only touch the approved folder"
# is a claim rather than a boundary, so the capability is withdrawn from the
# product rather than shipped on trust.
#
# This is a separate flag from the slot switches and from the automation
# levels on purpose: it is checked at all three points where write access
# could otherwise be reached — the level's capability, the construction of a
# WriteGrant, and the launch itself — so restoring one of them by mistake, or
# loading a tab saved by an older build, cannot reopen the path.
IMPLEMENTATION_WRITES_ENABLED = False

# Whether an approved implementation also runs the project's test suite.
#
# Off until the tests can be executed inside an OS sandbox. Running them means
# executing code the AI has just written, with this app's own user rights,
# unrestricted network, and the full parent environment. Approving an
# implementation is not the same as approving "run arbitrary code as me", and
# the app must not quietly treat it that way.
#
# While this is False the run still *detects* the project's test command and
# shows it, so the user can run it themselves in a terminal they control.
#
# Re-enabling requires the sandbox to demonstrably confine a test run:
# writes succeed inside the project root and fail for siblings, HOME, symlinks
# pointing outward, /tmp and $TMPDIR; network is unreachable; secret
# environment variables are not inherited; children and grandchildren share
# the boundary; and cancel/timeout leaves no process group behind. A sandbox
# failure must not fall back to an unsandboxed run.
RUN_TESTS_AUTOMATICALLY = False

# Agents whose child process needs the ANTHROPIC_CONFIG_DIR profile isolation.
# ANTHROPIC_CONFIG_DIR is Claude Code specific, so isolating it is only
# required — and only enforced — for claude. A failure to establish that
# isolation must never silently downgrade to "run anyway".
AGENTS_REQUIRING_PROFILE_ISOLATION = frozenset({"claude"})

# Agents that must never be launched through a wrapper such as rtk. A wrapper
# is a separate process that receives the whole child environment, which this
# app neither audits nor controls. No AI CLI is wrapped today (rtk saved
# nothing on them and recorded the prompts), so this is belt-and-braces.
AGENTS_NEVER_WRAPPED = frozenset({"claude"})

# Settings-isolation flags for the legacy strict_subscription policy: they tell
# Claude Code to ignore the user's own settings.json and pin the login method.
# Existing CLI Mode deliberately does not send them — the user's configuration
# is the source of truth — so claude_command only appends them when a policy
# with inherit_user_cli_config=False is in effect.
CLAUDE_SETTINGS_ISOLATION_ARGS = (
    "--setting-sources",
    "",
    "--settings",
    '{"forceLoginMethod":"claudeai"}',
)

BLOCKED_CHILD_ENV_VARS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_LOCATION",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    # These control what the sonnet/opus/haiku/fable aliases resolve to (or
    # override the model entirely). Left in the child env, they could
    # silently redirect this app's explicit --model <alias> (e.g. "sonnet")
    # to a different, unverified model such as a credit-requiring one.
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_CUSTOM_MODEL_OPTION",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_NAME",
    "ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION",
    # Anthropic profile / Workload Identity Federation credentials. Per
    # Claude Code's authentication precedence, these outrank the /login
    # subscription credential, so leaving them in the child env could route
    # requests through a non-subscription (billed) path.
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
    # OAuth refresh/scope material, and a header injection point that can
    # carry an Authorization header of its own.
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN",
    "CLAUDE_CODE_OAUTH_SCOPES",
    "ANTHROPIC_CUSTOM_HEADERS",
    # Third-party provider / gateway credentials and endpoints. Each routes
    # inference somewhere this app can't verify as subscription-billed.
    "ANTHROPIC_AWS_API_KEY",
    "ANTHROPIC_AWS_BASE_URL",
    "ANTHROPIC_AWS_WORKSPACE_ID",
    "ANTHROPIC_BEDROCK_MANTLE_BASE_URL",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "ANTHROPIC_VERTEX_BASE_URL",
    "AWS_BEARER_TOKEN_BEDROCK",
    # Redirects Claude Code's whole config directory (and with it the stored
    # credentials it reads).
    "CLAUDE_CONFIG_DIR",
    # Both confirmed present in Claude Code's published environment-variable
    # reference. (An earlier comment here said they weren't; that was wrong —
    # a doc summary had missed them and the page was later re-checked
    # directly.)
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_VERTEX",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GCLOUD_PROJECT",
    "CLOUDSDK_CORE_PROJECT",
    "VERTEXAI_LOCATION",
    "VERTEXAI_PROJECT",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "CODEX_ACCESS_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
}

EXCLUDED_DIRS = {
    ".git",
    APP_DIR_NAME,
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".turbo",
}

IMPORTANT_FILES = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    "package.json",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
)

AI_VENDOR_PATHS = (
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
    ".claude",
    ".gemini",
    ".codex",
)


def app_dir(project_root: Path) -> Path:
    return project_root / APP_DIR_NAME


def is_agent_slot_enabled(agent: str) -> bool:
    """Whether this agent may run at all, independent of whether its CLI is
    installed. Both the pre-flight gate (CliAdapters.command_exists) and the
    last-resort gate in ProcessRunner.run() call this, so a disabled slot
    can't be reached by a caller that forgets to check first."""
    if agent == "claude":
        return CLAUDE_SLOT_ENABLED
    if agent == "codex":
        return CODEX_SLOT_ENABLED
    if agent == "gemini":
        return ANTIGRAVITY_SLOT_ENABLED
    return True
