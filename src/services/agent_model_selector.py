from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from src.services.chair_agent import ChairAgent

CATALOG_PATH = Path(__file__).resolve().parent.parent / "agent_models.json"

AGENT_DISPLAY_NAMES: dict[str, str] = {
    "claude": "Claude",
    "codex": "Codex",
    "gemini": "Antigravity (Gemini)",
}

# Sentinel stored in RunContext.selected_models[agent] to mean "the user opted
# in to letting the chair pick this agent's model/effort", distinct from a
# real catalog id (dunder-wrapped, matching no id in any known catalog) and
# from "" / absent (which means "no override, use the CLI's own default").
CHAIR_AUTO_SELECT = "__chair_auto__"

SELECTOR_SYSTEM_PROMPT = """You help pick, per AI CLI agent, which model and
(if supported) effort/thinking level best fits the user's request today.
You may only choose from the exact model ids and effort levels listed for
each agent — never invent one. If you are unsure, pick the entry marked
(default) and write effort=none."""


@dataclass(frozen=True)
class AgentSelection:
    """Per-agent chosen (model_id, effort) for one run. Either or both may be
    None, meaning "no override — let the CLI use its own default"."""

    choices: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)
    #: Which agents in `choices` were resolved by asking the chair (as opposed
    #: to the user explicitly picking a model), purely for the render() note —
    #: does not affect argv building, which treats every entry in `choices`
    #: identically regardless of who picked it.
    chair_auto_agents: frozenset[str] = field(default_factory=frozenset)

    def for_agent(self, agent: str) -> tuple[str | None, str | None]:
        return self.choices.get(agent, (None, None))

    @staticmethod
    def empty() -> "AgentSelection":
        return AgentSelection(choices={})

    def render(self) -> str:
        if not self.choices:
            return "(no model/effort overrides; each CLI uses its own default)"
        lines = []
        for agent, (model_id, effort) in self.choices.items():
            if not model_id:
                continue
            suffix = f" / effort={effort}" if effort else ""
            auto_note = "  [議長AI自動選択]" if agent in self.chair_auto_agents else ""
            lines.append(f"- {agent}: model={model_id}{suffix}{auto_note}")
        return "\n".join(lines) if lines else "(no model/effort overrides)"


def load_catalog(path: Path = CATALOG_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def all_models_for(agent: str, catalog: dict) -> list[dict]:
    """All known models for `agent` regardless of billing_status.
    Used for UI selection options."""
    agent_data = catalog.get(agent) if isinstance(catalog, dict) else None
    if not isinstance(agent_data, dict):
        return []
    models = agent_data.get("models")
    if not isinstance(models, list):
        return []
    return [
        m for m in models
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip()
    ]


def safe_models_for(agent: str, catalog: dict) -> list[dict]:
    """Models confirmed safe under subscription. Accepts 'subscription_safe'
    or 'subscription_included'."""
    agent_data = catalog.get(agent) if isinstance(catalog, dict) else None
    if not isinstance(agent_data, dict):
        return []
    models = agent_data.get("models")
    if not isinstance(models, list):
        return []
    return [
        m
        for m in models
        if isinstance(m, dict)
        and m.get("billing_status") in ("subscription_safe", "subscription_included")
        and isinstance(m.get("id"), str)
        and m["id"].strip()
    ]


def save_catalog_atomically(catalog: dict, path: Path = CATALOG_PATH) -> bool:
    """Atomically writes `catalog` dictionary to `path` using a temporary file and os.replace
    to prevent file corruption and race conditions during concurrent updates.
    """
    payload = json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"
    tmp_path = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
        return True
    except OSError:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        return False


def refresh_gemini_catalog_from_agy(path: Path = CATALOG_PATH) -> bool:
    """Best-effort: re-populate the 'gemini' entry from `agy models`'s live,
    official output. Not called automatically by select() (would add a
    subprocess call + failure mode to every run) — this is a manual/periodic
    maintenance utility. Leaves the file untouched on any failure.
    """
    if not shutil.which("agy"):
        return False
    try:
        completed = subprocess.run(
            ["agy", "models"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    if completed.returncode != 0 or not completed.stdout.strip():
        return False

    if path.exists():
        try:
            catalog = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return False
    else:
        catalog = {}
    gemini_entry = catalog.get("gemini") or {"supports_separate_effort": False}
    existing_by_id = {m["id"]: m for m in gemini_entry.get("models") or []}

    models = []
    for line in completed.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2 or not parts[0].strip():
            continue
        model_id, label = parts[0].strip(), parts[1].strip()
        existing = existing_by_id.get(model_id)
        if existing is not None:
            # Keep the human-reviewed billing_status (and any other reviewed
            # fields); only the label is refreshed from the live listing.
            updated = dict(existing)
            updated["label"] = label
            models.append(updated)
        else:
            # A model `agy models` has never shown before is unverified by
            # definition — it must not inherit safety from an unrelated
            # entry, and no entry gets an unearned selector_default.
            models.append({
                "id": model_id,
                "label": label,
                "billing_status": "unknown",
                "discovery_source": "agy_models",
                "availability": True,
            })
    if not models:
        return False

    gemini_entry["models"] = models
    catalog["gemini"] = gemini_entry
    return save_catalog_atomically(catalog, path)


def resolve_chair_auto_agents(run_context: object | None, available_agents: set[str]) -> set[str]:
    """Which of `available_agents` are marked CHAIR_AUTO_SELECT in
    run_context.selected_models.

    `run_context=None` means no per-agent GUI selection exists at all (e.g. a
    caller that never went through the GUI, or a direct test call) — every
    available agent goes to the chair, matching select()'s original,
    pre-GUI-selection design intent of picking for the whole set.

    Takes `run_context` untyped (not `RunContext`) deliberately: this module
    only ever reads one attribute off it via getattr, the same loose-coupling
    already used for run_context elsewhere in this codebase, so it doesn't
    need to import the GUI-adjacent run_registry module at all.
    """
    if run_context is None:
        return set(available_agents)
    selected = getattr(run_context, "selected_models", {}) or {}
    return {agent for agent in available_agents if selected.get(agent) == CHAIR_AUTO_SELECT}


def _is_cancelled(cancel_event: object | None) -> bool:
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set and is_set())


def _build_prompt(user_request: str, candidates: dict[str, list[dict]], catalog: dict) -> str:
    sections = []
    for agent, models in candidates.items():
        agent_catalog = catalog.get(agent) or {}
        supports_effort = bool(agent_catalog.get("supports_separate_effort"))
        lines = [f"## {agent}"]
        for model in models:
            default_tag = " (default)" if model.get("selector_default") else ""
            note = f" — {model['note']}" if model.get("note") else ""
            effort_levels = model.get("effort_levels") or []
            if supports_effort and effort_levels:
                effort_note = f" [efforts: {', '.join(effort_levels)}]"
            else:
                effort_note = " [no separate effort level; reply effort=none]"
            lines.append(f"- {model['id']}{default_tag}{note}{effort_note}")
        sections.append("\n".join(lines))

    return f"""User request:
{user_request}

Candidates (choose ONLY from these ids):
{chr(10).join(sections)}

Reply with exactly one line per agent, in this form:
<agent>: model=<model_id> effort=<level_or_none>
"""


_RESPONSE_LINE = re.compile(r"^\s*(\w+)\s*:\s*model=(\S+)(?:\s+effort=(\S+))?", re.IGNORECASE)


def _parse_and_validate(answer: str, candidates: dict[str, list[dict]], catalog: dict) -> AgentSelection:
    choices: dict[str, tuple[str | None, str | None]] = {}
    for line in answer.splitlines():
        match = _RESPONSE_LINE.match(line)
        if not match:
            continue
        agent, model_id, effort = match.group(1), match.group(2), match.group(3)
        if agent not in candidates:
            continue
        model_by_id = {m["id"]: m for m in candidates[agent]}
        if model_id not in model_by_id:
            continue

        agent_catalog = catalog.get(agent) or {}
        supports_effort = bool(agent_catalog.get("supports_separate_effort"))
        allowed_efforts = set(model_by_id[model_id].get("effort_levels") or [])
        resolved_effort = None
        if supports_effort and effort and effort.lower() != "none" and effort in allowed_efforts:
            resolved_effort = effort

        choices[agent] = (model_id, resolved_effort)
    return AgentSelection(choices=choices)


def _confirmed_default_model(models: list[dict]) -> dict | None:
    """The single model flagged selector_default=True (strict boolean, not
    merely truthy — a stray string like "false" must not pass) among an
    already billing-safe candidate list, with a non-empty string id. Returns
    None — never an arbitrary guess — if there isn't exactly one such model,
    so a catalog authoring mistake (a truthy-but-wrong flag, two defaults, or
    a blank id) can't silently produce an unsafe or broken --model."""
    defaults = [
        m
        for m in models
        if isinstance(m, dict)
        and m.get("selector_default") is True
        and isinstance(m.get("id"), str)
        and m["id"].strip()
    ]
    if len(defaults) != 1:
        return None
    return defaults[0]


def confirmed_default_for(agent: str, catalog: dict) -> dict | None:
    """The single confirmed-default (billing_status=subscription_safe AND
    selector_default=True, with a valid id) model for `agent`, or None."""
    return _confirmed_default_model(safe_models_for(agent, catalog))


def has_confirmed_default(agent: str, catalog: dict) -> bool:
    """True only if `agent` has exactly one confirmed-default model — i.e.
    there's a model this app can always pass as an explicit --model, so a
    chair-selection failure (or no selection at all) never falls through to
    the CLI's own unverified local default."""
    return confirmed_default_for(agent, catalog) is not None


def _fallback_selection(candidates: dict[str, list[dict]]) -> AgentSelection:
    """Curated safety net: whenever the chair doesn't produce a usable pick
    for an agent (unavailable, no reply, or an invalid/credit-requiring
    reply), that agent falls back to its catalog-curated selector_default
    model — never to "no override", which would let the CLI's own local
    default (possibly a credit-requiring model a user selected in an
    unrelated interactive session) run unchecked. An agent with no confirmed
    default among its safe candidates (e.g. gemini, whose models are all
    unreviewed) simply isn't included — its CLI keeps running on its own
    default, unchanged from before this selector existed."""
    choices: dict[str, tuple[str | None, str | None]] = {}
    for agent, models in candidates.items():
        default_model = _confirmed_default_model(models)
        if default_model:
            choices[agent] = (default_model["id"], None)
    return AgentSelection(choices=choices)


def default_selection(available_agents: set[str], catalog: dict | None = None) -> AgentSelection:
    """Chair-free selection using only the catalog's curated
    selector_default per agent. Used wherever consulting the chair isn't
    appropriate — e.g. preflight, which runs before the user's request or LM
    Studio's own availability is even known — but a safe, explicit --model
    is still required rather than trusting the CLI's own local default."""
    if catalog is None:
        catalog = load_catalog()
    candidates = {
        agent: safe_models_for(agent, catalog)
        for agent in available_agents
        if safe_models_for(agent, catalog)
    }
    return _fallback_selection(candidates)


def validated_model_and_effort(
    agent: str, model_id: str | None, effort: str | None, catalog: dict | None = None
) -> tuple[str | None, str | None]:
    """Re-check a model/effort pair against the catalog, substituting the
    confirmed default for an unusable model and dropping an effort the chosen
    model doesn't support.

    Shared so that CliAdapters (when assembling) and ProcessRunner (right
    before launching) apply exactly the same rule — a second implementation
    would be a second chance to disagree."""
    if catalog is None:
        catalog = load_catalog()
    safe_by_id = {m["id"]: m for m in safe_models_for(agent, catalog)}
    if model_id not in safe_by_id:
        default_model = confirmed_default_for(agent, catalog)
        if default_model is None:
            return None, None
        model_id = default_model["id"]

    if not effort:
        return model_id, None
    agent_catalog = catalog.get(agent) or {}
    if not agent_catalog.get("supports_separate_effort"):
        return model_id, None
    allowed = safe_by_id.get(model_id, {}).get("effort_levels") or []
    return model_id, (effort if effort in allowed else None)


def select(
    user_request: str,
    available_agents: set[str],
    chair: ChairAgent,
    catalog: dict | None = None,
    cancel_event: object | None = None,
) -> AgentSelection:
    """Asks the chair to pick, per available agent, a model (and effort where
    supported) from the safety-filtered catalog. If there are no safe
    candidates at all, or the request was cancelled, returns an empty
    selection (no overrides). Otherwise, whenever the chair is unavailable or
    its reply for an agent doesn't validate, that agent falls back to its
    catalog-curated selector_default model rather than silently letting the
    CLI's own local default run — see _fallback_selection()."""
    if catalog is None:
        catalog = load_catalog()

    candidates = {
        agent: safe_models_for(agent, catalog)
        for agent in available_agents
        if safe_models_for(agent, catalog)
    }
    if not candidates:
        return AgentSelection.empty()

    if _is_cancelled(cancel_event):
        return AgentSelection.empty()

    prompt = _build_prompt(user_request, candidates, catalog)

    if _is_cancelled(cancel_event):
        return AgentSelection.empty()

    fallback = _fallback_selection(candidates)

    answer = chair.chat(SELECTOR_SYSTEM_PROMPT, prompt, max_tokens=400)
    if not answer:
        return fallback

    parsed = _parse_and_validate(answer, candidates, catalog)
    merged = dict(fallback.choices)
    merged.update(parsed.choices)
    return AgentSelection(choices=merged)
