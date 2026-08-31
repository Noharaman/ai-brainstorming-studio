from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from src.services.model_catalog_updater import (
    PARSER_SYSTEM_PROMPT,
    clean_json_response,
    parse_and_update_models_from_text,
)


class TestModelCatalogUpdater(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.catalog_path = Path(self.temp_dir.name) / "agent_models.json"
        initial_catalog = {
            "catalog_version": "test",
            "claude": {"models": [{"id": "sonnet", "label": "Sonnet 4", "billing_status": "subscription_safe"}]},
            "codex": {"models": []},
            "gemini": {"models": []},
        }
        self.catalog_path.write_text(json.dumps(initial_catalog), encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_clean_json_response_markdown(self) -> None:
        raw = "```json\n{\"claude\": []}\n```"
        self.assertEqual(clean_json_response(raw), '{"claude": []}')

    def test_clean_json_response_with_text(self) -> None:
        raw = "Here is the result:\n{\"codex\": [{\"id\": \"gpt-5\"}]}\nHope it helps!"
        self.assertEqual(clean_json_response(raw), '{"codex": [{"id": "gpt-5"}]}')

    def test_parse_and_update_empty_text(self) -> None:
        success, msg, agents = parse_and_update_models_from_text("", catalog_path=self.catalog_path)
        self.assertFalse(success)
        self.assertIn("空です", msg)

    def test_parse_and_update_successful_merge(self) -> None:
        mock_chair = MagicMock()
        mock_response = json.dumps({
            "claude": [
                {"id": "sonnet", "label": "Sonnet 5", "note": "Fast", "billing_status": "subscription_safe"},
                {"id": "fable", "label": "Fable 5", "note": "Heavy", "billing_status": "usage_credits"},
            ],
            "codex": [
                {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol", "note": "Frontier", "billing_status": "subscription_safe"}
            ],
        })
        mock_chair.chat.return_value = mock_response

        raw_text = "Some terminal text..."
        success, msg, agents = parse_and_update_models_from_text(
            raw_text, chair_agent=mock_chair, catalog_path=self.catalog_path
        )
        self.assertTrue(success, msg)
        self.assertIn("claude", agents)
        self.assertIn("codex", agents)

        updated_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        claude_models = {m["id"]: m for m in updated_catalog["claude"]["models"]}
        self.assertIn("sonnet", claude_models)
        self.assertEqual(claude_models["sonnet"]["label"], "Sonnet 5")
        self.assertIn("fable", claude_models)
        self.assertEqual(claude_models["fable"]["billing_status"], "usage_credits")

        codex_models = {m["id"]: m for m in updated_catalog["codex"]["models"]}
        self.assertIn("gpt-5.6-sol", codex_models)

    def test_chat_is_called_with_system_prompt_first(self) -> None:
        """Regression: chair.chat(prompt, PARSER_SYSTEM_PROMPT) had these
        swapped — ChairAgent.chat(system_prompt, user_prompt, ...) means the
        JSON-format instructions were landing in the user slot and the raw
        pasted text in the system slot."""
        mock_chair = MagicMock()
        mock_chair.chat.return_value = json.dumps({"claude": [{"id": "sonnet"}]})

        parse_and_update_models_from_text(
            "some pasted text", chair_agent=mock_chair, catalog_path=self.catalog_path
        )

        args, _kwargs = mock_chair.chat.call_args
        self.assertEqual(args[0], PARSER_SYSTEM_PROMPT)
        self.assertIn("some pasted text", args[1])

    def test_merge_preserves_reviewed_and_selector_fields(self) -> None:
        """A model that already exists in the catalog must keep its
        human-reviewed fields across a re-paste that doesn't mention them —
        a /model screen never surfaces selector_default/reviewed_at/
        reviewed_source/discovery_source, so without carrying them forward
        every re-import silently erased them."""
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        catalog["claude"]["models"][0].update({
            "selector_default": True,
            "reviewed_at": "2026-08-01",
            "reviewed_source": "human",
            "discovery_source": "manual",
        })
        self.catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

        mock_chair = MagicMock()
        mock_chair.chat.return_value = json.dumps({
            "claude": [{"id": "sonnet", "label": "Sonnet (renamed)", "billing_status": "subscription_safe"}],
        })
        success, msg, _agents = parse_and_update_models_from_text(
            "re-pasted /model output", chair_agent=mock_chair, catalog_path=self.catalog_path
        )
        self.assertTrue(success, msg)

        updated = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        sonnet = next(m for m in updated["claude"]["models"] if m["id"] == "sonnet")
        self.assertEqual(sonnet["label"], "Sonnet (renamed)")
        self.assertIs(sonnet["selector_default"], True)
        self.assertEqual(sonnet["reviewed_at"], "2026-08-01")
        self.assertEqual(sonnet["reviewed_source"], "human")
        self.assertEqual(sonnet["discovery_source"], "manual")

    def test_merge_defaults_new_model_billing_status_to_unknown(self) -> None:
        """A brand-new model id whose billing_status the pasted text didn't
        specify must default to unknown (fail-closed), never
        subscription_safe — matching refresh_gemini_catalog_from_agy()'s
        convention for any model never reviewed before."""
        mock_chair = MagicMock()
        mock_chair.chat.return_value = json.dumps({
            "codex": [{"id": "gpt-6-new", "label": "GPT-6 New"}],
        })
        success, msg, _agents = parse_and_update_models_from_text(
            "some text", chair_agent=mock_chair, catalog_path=self.catalog_path
        )
        self.assertTrue(success, msg)

        updated = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        new_model = next(m for m in updated["codex"]["models"] if m["id"] == "gpt-6-new")
        self.assertEqual(new_model["billing_status"], "unknown")

    def test_parser_prompt_does_not_default_to_subscription_safe(self) -> None:
        self.assertIn("unknown", PARSER_SYSTEM_PROMPT)
        self.assertNotIn('Otherwise, set "subscription_safe"', PARSER_SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main()
