from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from src.services.agent_model_detector import AgentModelDetector
from src.services.agent_model_selector import (
    AgentSelection,
    all_models_for,
    safe_models_for,
    save_catalog_atomically,
)
from src.services.cli_adapters import CliAdapters
from src.services.cli_execution_policy import EXISTING_CONFIG
from src.services.refinement_loop import RefinementLoop
from src.services.run_registry import ProjectRunRegistry


class AgentModelDetectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = TemporaryDirectory()
        self.sandbox = Path(self.tmp_dir.name)
        self.catalog_path = self.sandbox / "agent_models.json"
        self.sample_catalog = {
            "catalog_version": "2026-08-16-v2",
            "claude": {
                "detected_version": "1.0.0",
                "first_seen": "2026-08-16",
                "last_seen": "2026-08-16",
                "supports_separate_effort": True,
                "models": [
                    {
                        "id": "sonnet",
                        "label": "Sonnet 5",
                        "billing_status": "subscription_safe",
                        "selector_default": True,
                        "effort_levels": ["low", "high"],
                    },
                    {
                        "id": "fable",
                        "label": "Fable 5",
                        "billing_status": "usage_credits",
                    },
                ],
            },
            "gemini": {
                "detected_version": "0.1.0",
                "first_seen": "2026-08-16",
                "last_seen": "2026-08-16",
                "supports_separate_effort": False,
                "models": [
                    {
                        "id": "gemini-3.6-flash-high",
                        "label": "Gemini 3.6 Flash",
                        "billing_status": "unknown",
                    }
                ],
            },
            "codex": {
                "detected_version": "2.0.0",
                "first_seen": "2026-08-16",
                "last_seen": "2026-08-16",
                "supports_separate_effort": False,
                "models": [
                    {"id": "gpt-4o", "label": "GPT-4o", "billing_status": "unknown"},
                    {"id": "o3-mini", "label": "o3-mini", "billing_status": "unknown", "effort_levels": ["low", "medium", "high"]},
                ],
            },
        }
        self.catalog_path.write_text(
            json.dumps(self.sample_catalog, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_all_models_for_returns_all_entries(self) -> None:
        all_models = all_models_for("claude", self.sample_catalog)
        self.assertEqual(len(all_models), 2)

        safe_models = safe_models_for("claude", self.sample_catalog)
        self.assertEqual(len(safe_models), 1)
        self.assertEqual(safe_models[0]["id"], "sonnet")

    @patch("shutil.which")
    def test_detector_detects_cli_version_and_metadata_change(self, mock_which: MagicMock) -> None:
        mock_which.return_value = "/bin/claude"
        detector = AgentModelDetector(catalog_path=self.catalog_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1.1.0\n")
            ver, changed = detector.detect_cli_version("claude")
            self.assertEqual(ver, "1.1.0")
            self.assertTrue(changed)

            # Re-read catalog to verify detected_version, first_seen, last_seen
            updated_catalog = json.loads(self.catalog_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_catalog["claude"]["detected_version"], "1.1.0")
            self.assertIn("first_seen", updated_catalog["claude"])
            self.assertIn("last_seen", updated_catalog["claude"])

    @patch("shutil.which")
    def test_detector_refresh_all_isolated(self, mock_which: MagicMock) -> None:
        mock_which.return_value = None  # No real agy/claude/codex in test
        detector = AgentModelDetector(catalog_path=self.catalog_path)

        res = detector.refresh_all()
        self.assertIn("gemini", res)
        self.assertIn("claude", res)
        self.assertIn("codex", res)
        self.assertEqual(res["gemini"].source, "none")

    def test_run_context_is_strictly_immutable(self) -> None:
        """Verifies P1: RunContext.selected_models and selected_efforts are MappingProxyTypes rejecting assignment."""
        registry = ProjectRunRegistry()
        res = registry.try_start(self.sandbox, tab_id="t1")
        self.assertIsNotNone(res)
        ctx = registry.finalize(
            res,
            room_id="r1",
            request="req",
            automation_level=2,
            selected_models={"claude": "sonnet"},
            selected_efforts={"claude": "high"},
            effort="high",
            catalog_version="v1",
        )
        self.assertIsNotNone(ctx)
        self.assertIsInstance(ctx.selected_models, MappingProxyType)
        self.assertIsInstance(ctx.selected_efforts, MappingProxyType)
        self.assertEqual(ctx.selected_efforts.get("claude"), "high")
        self.assertEqual(ctx.catalog_version, "v1")

        with self.assertRaises(TypeError):
            ctx.selected_models["claude"] = "mutated"

        with self.assertRaises(TypeError):
            ctx.selected_efforts["claude"] = "mutated"

    @patch("src.services.agent_model_selector.select")
    def test_all_cli_defaults_suppresses_chair_via_refinement_loop(
        self, mock_chair_select: MagicMock
    ) -> None:
        """Verifies P1 & P2: when all models are set to CLI defaults, RefinementLoop.determine_agent_selection
        does NOT call chair AI and returns an AgentSelection with no models so no --model flag is generated."""
        registry = ProjectRunRegistry()
        res = registry.try_start(self.sandbox, tab_id="t1")
        ctx = registry.finalize(
            res,
            room_id="r1",
            request="req",
            automation_level=2,
            selected_models={"claude": "", "gemini": "", "codex": ""},
            catalog_version="2026-08-16-v2",
        )
        self.assertIsNotNone(ctx)

        loop = RefinementLoop(prefer_rtk=False)
        agent_selection = loop.determine_agent_selection(
            user_request="req",
            available_agents={"claude", "gemini", "codex"},
            run_context=ctx,
        )

        mock_chair_select.assert_not_called()

        adapters = CliAdapters(policy=EXISTING_CONFIG)
        prompts = {"claude": "p1", "codex": "p2"}

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/bin/mock"
            cmds, _ = adapters.build_commands(prompts, agent_selection=agent_selection)
            for agent, argv in cmds.items():
                self.assertNotIn("--model", argv, f"Agent {agent} unexpectedly got --model")
                self.assertNotIn("-m", argv, f"Agent {agent} unexpectedly got -m")

    def test_codex_and_claude_explicit_models_and_efforts_reflected_in_argv(self) -> None:
        """Verifies P1: Codex and Claude model/effort selections are accurately generated in CLI argv."""
        registry = ProjectRunRegistry()
        res = registry.try_start(self.sandbox, tab_id="t1")
        ctx = registry.finalize(
            res,
            room_id="r1",
            request="req",
            automation_level=2,
            selected_models={"claude": "opus", "codex": "o3-mini"},
            selected_efforts={"claude": "high", "codex": "medium"},
            catalog_version="2026-08-16-v2",
        )
        self.assertIsNotNone(ctx)

        loop = RefinementLoop(prefer_rtk=False)
        agent_selection = loop.determine_agent_selection(
            user_request="req",
            available_agents={"claude", "codex"},
            run_context=ctx,
        )

        adapters = CliAdapters(policy=EXISTING_CONFIG)
        prompts = {"claude": "p1", "codex": "p2"}

        with patch("shutil.which") as mock_which:
            mock_which.return_value = "/bin/mock"
            cmds, _ = adapters.build_commands(prompts, agent_selection=agent_selection)

            # Claude checks
            self.assertIn("--model", cmds["claude"])
            claude_model_idx = cmds["claude"].index("--model")
            self.assertEqual(cmds["claude"][claude_model_idx + 1], "opus")

            # Codex checks
            self.assertIn("-m", cmds["codex"])
            codex_model_idx = cmds["codex"].index("-m")
            self.assertEqual(cmds["codex"][codex_model_idx + 1], "o3-mini")

            # Codex effort check
            self.assertIn("-c", cmds["codex"])
            self.assertIn("model_reasoning_effort=\"medium\"", cmds["codex"])

    @patch("subprocess.run")
    @patch("shutil.which")
    def test_catalog_refresh_isolated_and_does_not_mutate_in_flight_run_context(
        self, mock_which: MagicMock, mock_run: MagicMock
    ) -> None:
        """Verifies P2: detector.refresh_all() is fully mocked against real CLI binaries
        and does not alter an in-flight RunContext."""
        mock_which.return_value = "/bin/mock"
        mock_run.return_value = MagicMock(returncode=0, stdout="1.0.0\n")

        registry = ProjectRunRegistry()
        res = registry.try_start(self.sandbox, tab_id="t1")
        ctx = registry.finalize(
            res,
            room_id="r1",
            request="req",
            automation_level=2,
            selected_models={"claude": "sonnet"},
            selected_efforts={"claude": "high"},
            catalog_version="2026-08-16-v2",
        )

        detector = AgentModelDetector(catalog_path=self.catalog_path)
        detector.refresh_all()

        # In-flight RunContext remains unchanged
        self.assertEqual(ctx.selected_models["claude"], "sonnet")
        self.assertEqual(ctx.selected_efforts["claude"], "high")
        self.assertEqual(ctx.catalog_version, "2026-08-16-v2")

    def test_atomic_catalog_write(self) -> None:
        """Verifies P1: save_catalog_atomically correctly updates catalog file safely."""
        cat = {"catalog_version": "v999", "claude": {"models": []}}
        ok = save_catalog_atomically(cat, path=self.catalog_path)
        self.assertTrue(ok)
        loaded = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["catalog_version"], "v999")

    def test_gui_model_selection_does_not_implicitly_set_max_effort(self) -> None:
        """Verifies P1: Selecting a model in GUI dropdown does not implicitly set maximum effort."""
        from src.gui.project_tab import EFFORT_UNSET_LABEL, ProjectTab

        tab_mock = MagicMock()
        tab_mock.model_selections = {
            "claude": MagicMock(get=lambda: "sonnet"),
            "gemini": MagicMock(get=lambda: "CLI既定モデル"),
            "codex": MagicMock(get=lambda: "CLI既定モデル"),
        }
        tab_mock.effort_selections = {
            "claude": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
            "gemini": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
            "codex": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
        }

        selected_models, selected_efforts, unverified = ProjectTab.collect_user_selections(tab_mock)

        self.assertEqual(selected_models.get("claude"), "sonnet")
        self.assertEqual(selected_efforts, {}, "selected_efforts must be empty when selecting model only")
        self.assertEqual(unverified, [])

    def test_gui_effort_selection_is_collected_when_explicitly_chosen(self) -> None:
        """The counterpart to the above: an actually-chosen effort level IS
        collected — P1 forbids an *implicit* max, not effort selection
        itself."""
        from src.gui.project_tab import EFFORT_UNSET_LABEL, ProjectTab

        tab_mock = MagicMock()
        tab_mock.model_selections = {
            "claude": MagicMock(get=lambda: "sonnet"),
            "gemini": MagicMock(get=lambda: "CLI既定モデル"),
            "codex": MagicMock(get=lambda: "CLI既定モデル"),
        }
        tab_mock.effort_selections = {
            "claude": MagicMock(get=lambda: "high"),
            "gemini": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
            "codex": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
        }

        _selected_models, selected_efforts, _unverified = ProjectTab.collect_user_selections(tab_mock)

        self.assertEqual(selected_efforts, {"claude": "high"})

    def test_chair_auto_select_choice_maps_to_the_sentinel(self) -> None:
        from src.gui.project_tab import CHAIR_AUTO_LABEL, EFFORT_UNSET_LABEL, ProjectTab
        from src.services.agent_model_selector import CHAIR_AUTO_SELECT

        tab_mock = MagicMock()
        tab_mock.model_selections = {
            "claude": MagicMock(get=lambda: CHAIR_AUTO_LABEL),
            "gemini": MagicMock(get=lambda: "CLI既定モデル"),
            "codex": MagicMock(get=lambda: "CLI既定モデル"),
        }
        tab_mock.effort_selections = {
            "claude": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
            "gemini": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
            "codex": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
        }

        selected_models, selected_efforts, unverified = ProjectTab.collect_user_selections(tab_mock)

        self.assertEqual(selected_models["claude"], CHAIR_AUTO_SELECT)
        self.assertEqual(unverified, [], "the sentinel must never trigger the unverified-model confirmation")
        self.assertEqual(selected_efforts, {})

    def test_a_mixed_run_resolves_explicit_auto_and_default_independently(self) -> None:
        """Claude explicit, Gemini auto, Codex default — all in one call,
        the exact scenario 議長AIにお任せ exists for."""
        from src.gui.project_tab import CHAIR_AUTO_LABEL, EFFORT_UNSET_LABEL, ProjectTab
        from src.services.agent_model_selector import CHAIR_AUTO_SELECT

        tab_mock = MagicMock()
        tab_mock.model_selections = {
            "claude": MagicMock(get=lambda: "opus"),
            "gemini": MagicMock(get=lambda: CHAIR_AUTO_LABEL),
            "codex": MagicMock(get=lambda: "CLI既定モデル"),
        }
        tab_mock.effort_selections = {
            "claude": MagicMock(get=lambda: "high"),
            "gemini": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
            "codex": MagicMock(get=lambda: EFFORT_UNSET_LABEL),
        }

        selected_models, selected_efforts, unverified = ProjectTab.collect_user_selections(tab_mock)

        self.assertEqual(selected_models, {"claude": "opus", "gemini": CHAIR_AUTO_SELECT})
        self.assertNotIn("codex", selected_models, "CLI既定モデル must stay omitted, not stored as a value")
        self.assertEqual(selected_efforts, {"claude": "high"})
        self.assertEqual(unverified, [])


if __name__ == "__main__":
    unittest.main()
