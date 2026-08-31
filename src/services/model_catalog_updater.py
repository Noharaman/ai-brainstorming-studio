from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from src.services.agent_model_selector import (
    AGENT_DISPLAY_NAMES,
    CATALOG_PATH,
    load_catalog,
    save_catalog_atomically,
)
from src.services.chair_agent import ChairAgent


PARSER_SYSTEM_PROMPT = """You are a specialized parser that extracts AI CLI model configurations from terminal output.
Analyze the provided text and extract model information for each AI CLI agent: "claude", "codex", or "gemini".

Return ONLY a JSON object with this exact structure:
{
  "claude": [
    {
      "id": "model-id",
      "label": "Display Name",
      "note": "Short description",
      "billing_status": "subscription_safe" | "usage_credits"
    }
  ],
  "codex": [],
  "gemini": []
}

Rules:
1. Identify which AI agent each section belongs to using keywords (e.g. "Claude", "codex", "Gemini", "Antigravity").
2. "id": Use the clean command-line model identifier (e.g. "sonnet", "opus", "haiku", "fable", "gpt-5.6-sol", "gemini-3.7-flash-high"). Do not include prefixes like "1. " or "› ".
3. "label": Human-readable display label (e.g. "Sonnet 5", "GPT-5.6 Sol", "Gemini 3.7 Flash").
4. "note": The description text provided in the terminal output.
5. "billing_status": If the text mentions "usage credits", "requires credits", or "pay as you go", set "usage_credits". Otherwise, set "unknown" — do NOT guess "subscription_safe" for a model whose billing you cannot confirm from the text.
6. Only include agents that are actually present in the input text. If no models for an agent are found, provide an empty list or omit the key.
7. Return raw valid JSON only. Do not wrap in markdown backticks or include any conversational filler."""


def clean_json_response(raw_response: str) -> str:
    """Extracts JSON substring if wrapped in markdown code blocks or filler."""
    text = raw_response.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    
    # Try finding first { and last }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


def parse_and_update_models_from_text(
    raw_text: str,
    chair_agent: ChairAgent | None = None,
    catalog_path: Path = CATALOG_PATH,
) -> tuple[bool, str, list[str]]:
    """Sends raw /model output to ChairAgent, parses the returned JSON, and merges it into agent_models.json.

    Returns:
        (success: bool, message: str, updated_agents: list[str])
    """
    if not raw_text or not raw_text.strip():
        return False, "テキストが空です。/model の出力テキストを貼り付けてください。", []

    chair = chair_agent or ChairAgent()
    prompt = f"Extract all models from this terminal text:\n\n{raw_text.strip()}"

    try:
        # ChairAgent.chat(system_prompt, user_prompt, ...) — these were swapped,
        # which put the JSON-format instructions in the *user* slot and the raw
        # pasted terminal text in the *system* slot. Most chat templates weight
        # the system prompt more heavily for format adherence, so this likely
        # degraded how reliably the reply came back as parseable JSON.
        response = chair.chat(PARSER_SYSTEM_PROMPT, prompt)
    except Exception as exc:
        return False, f"議長AI（LM Studio）への問い合わせに失敗しました:\n{exc}", []

    if not response or not response.strip():
        return False, "議長AIから空の応答が返されました。LM Studioが起動しているか確認してください。", []

    cleaned = clean_json_response(response)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return False, f"議長AIの応答をJSONとして解釈できませんでした:\n{cleaned[:200]}...", []

    if not isinstance(parsed, dict):
        return False, "議長AIの応答フォーマットが不正です。", []

    catalog = load_catalog(catalog_path)
    today = datetime.now().strftime("%Y-%m-%d")
    updated_agents: list[str] = []

    for agent_key in ("claude", "codex", "gemini"):
        models_list = parsed.get(agent_key)
        if not isinstance(models_list, list) or not models_list:
            continue

        agent_entry = catalog.get(agent_key) or {}
        existing_models = {
            m.get("id"): m
            for m in agent_entry.get("models", [])
            if isinstance(m, dict) and m.get("id")
        }

        merged_models: list[dict] = []
        seen_ids: set[str] = set()

        for new_m in models_list:
            if not isinstance(new_m, dict):
                continue
            m_id = str(new_m.get("id", "")).strip().lower()
            # Clean up common artifacts
            if m_id.startswith("1.") or m_id.startswith("›") or m_id.startswith("❯"):
                m_id = m_id.lstrip("1234567890. ›❯✔").strip()
            if not m_id or m_id in seen_ids or m_id == "default":
                continue

            seen_ids.add(m_id)
            existing = existing_models.get(m_id, {})
            merged = {
                "id": m_id,
                "label": str(new_m.get("label") or existing.get("label") or m_id),
                "note": str(new_m.get("note") or existing.get("note") or ""),
                # Fail-closed default: a model this paste didn't confirm the
                # billing of must not be labelled subscription_safe, matching
                # the convention refresh_gemini_catalog_from_agy() already
                # uses for models it has never reviewed.
                "billing_status": str(new_m.get("billing_status") or existing.get("billing_status") or "unknown"),
                "availability": True,
                "last_seen": today,
                "first_seen": existing.get("first_seen", today),
            }
            if "effort_levels" in existing:
                merged["effort_levels"] = existing["effort_levels"]
            elif agent_key == "claude" and m_id in ("sonnet", "opus", "fable"):
                merged["effort_levels"] = ["low", "medium", "high", "xhigh", "max"]

            # Carry forward human-reviewed/provenance fields for a model that
            # already existed. A re-paste of the same /model screen never
            # mentions these, so without this they were silently wiped on
            # every subsequent import of an id that had already been reviewed.
            for carry_field in ("selector_default", "reviewed_at", "reviewed_source", "discovery_source"):
                if carry_field in existing:
                    merged[carry_field] = existing[carry_field]

            merged_models.append(merged)

        # Retain any models from existing catalog that weren't in the new text
        for old_id, old_m in existing_models.items():
            if old_id not in seen_ids:
                merged_models.append(old_m)

        if merged_models:
            agent_entry["models"] = merged_models
            agent_entry["last_seen"] = today
            catalog[agent_key] = agent_entry
            updated_agents.append(agent_key)

    if not updated_agents:
        return False, "テキストから有効なモデル情報を検出できませんでした。", []

    catalog["catalog_version"] = f"{today}-user-updated"
    saved = save_catalog_atomically(catalog, catalog_path)
    if not saved:
        return False, "カタログファイルへの保存に失敗しました。", []

    names_str = "、".join(AGENT_DISPLAY_NAMES.get(a, a) for a in updated_agents)
    return True, f"{names_str} のモデル一覧を正常に更新しました！", updated_agents
