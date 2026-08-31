from __future__ import annotations

import io
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from src import config
from src.services.chair_agent import ChairAgent


class _Response(io.BytesIO):
    """Minimal stand-in for the object urlopen() yields as a context manager."""

    def __init__(self, payload: dict, status: int = 200) -> None:
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = status

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args) -> bool:
        return False


def _models_payload(*model_ids: str) -> dict:
    return {"data": [{"id": model_id} for model_id in model_ids]}


def _completion_payload(content, reasoning: str = "") -> dict:
    message = {"content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    return {"choices": [{"message": message}]}


@contextmanager
def _serving(*model_ids: str, completion: dict | None = None):
    """Serve /models from `model_ids` and /chat/completions from `completion`,
    recording the request bodies that were sent."""
    sent: list[dict] = []

    def fake_urlopen(request, timeout=None):
        url = request.full_url if hasattr(request, "full_url") else str(request)
        if url.endswith("/models"):
            return _Response(_models_payload(*model_ids))
        sent.append(json.loads(request.data.decode("utf-8")))
        return _Response(completion or _completion_payload("ok"))

    with patch("src.services.chair_agent.urllib.request.urlopen", fake_urlopen):
        yield sent


class PinnedChairModelTest(unittest.TestCase):
    def test_the_pinned_model_is_used_when_it_is_loaded(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("other/model", "pinned/model"):
                chair = ChairAgent()
                self.assertTrue(chair.available())
                self.assertEqual(chair.model_name(), "pinned/model")
                self.assertEqual(chair.unavailable_reason(), "")

    def test_an_unloaded_pinned_model_fails_closed_instead_of_using_another(self) -> None:
        """The whole point of pinning: the chair must not silently become a
        different model with different reasoning defaults."""
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("some/other-model"):
                chair = ChairAgent()
                self.assertFalse(chair.available())
                self.assertEqual(chair.model_name(), "")
                reason = chair.unavailable_reason()
                self.assertIn("pinned/model", reason)
                self.assertIn("some/other-model", reason)

    def test_an_unloaded_pinned_model_sends_no_request_at_all(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("some/other-model") as sent:
                self.assertIsNone(ChairAgent().chat("system", "user"))
                self.assertEqual(sent, [])

    def test_an_empty_pin_opts_in_to_whatever_is_loaded(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", ""):
            with _serving("first/model", "second/model"):
                chair = ChairAgent()
                self.assertTrue(chair.available())
                self.assertEqual(chair.model_name(), "first/model")

    def test_an_empty_pin_with_no_models_loaded_is_unavailable(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", ""):
            with _serving():
                chair = ChairAgent()
                self.assertFalse(chair.available())
                self.assertIn("モデル", chair.unavailable_reason())

    def test_an_unreachable_server_reports_a_connection_reason(self) -> None:
        def boom(request, timeout=None):
            raise OSError("connection refused")

        with patch("src.services.chair_agent.urllib.request.urlopen", boom):
            chair = ChairAgent()
            self.assertFalse(chair.available())
            self.assertIn("接続", chair.unavailable_reason())

    def test_invalidate_cache_clears_a_previous_reason(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("some/other-model"):
                chair = ChairAgent()
                self.assertNotEqual(chair.unavailable_reason(), "")
            with _serving("pinned/model"):
                chair.invalidate_cache()
                self.assertTrue(chair.available())
                self.assertEqual(chair.unavailable_reason(), "")


class ChairReasoningEffortTest(unittest.TestCase):
    def test_the_configured_effort_is_sent(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"), \
             patch.object(config, "LM_STUDIO_CHAIR_REASONING_EFFORT", "none"):
            with _serving("pinned/model") as sent:
                ChairAgent().chat("system", "user")
        self.assertEqual(sent[0]["reasoning_effort"], "none")
        self.assertEqual(sent[0]["model"], "pinned/model")

    def test_an_empty_effort_omits_the_field_entirely(self) -> None:
        """Some servers reject unknown fields; "" must send a clean payload."""
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"), \
             patch.object(config, "LM_STUDIO_CHAIR_REASONING_EFFORT", ""):
            with _serving("pinned/model") as sent:
                ChairAgent().chat("system", "user")
        self.assertNotIn("reasoning_effort", sent[0])


class ChairEmptyResponseTest(unittest.TestCase):
    """The failure actually observed with qwen3.8-27b at its default reasoning
    effort: HTTP 200, a full token budget spent reasoning, and no message."""

    def test_a_reasoning_only_response_degrades_to_falsy_without_raising(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving(
                "pinned/model",
                completion=_completion_payload("", reasoning="x" * 8758),
            ):
                answer = ChairAgent().chat("system", "user")
        self.assertFalse(answer)

    def test_a_null_content_response_does_not_crash(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("pinned/model", completion=_completion_payload(None)):
                answer = ChairAgent().chat("system", "user")
        self.assertFalse(answer)

    def test_a_normal_response_is_returned_stripped(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("pinned/model", completion=_completion_payload("  結論\n")):
                self.assertEqual(ChairAgent().chat("system", "user"), "結論")


class ChairHealthReportingTest(unittest.TestCase):
    def _lm_studio_status(self):
        from src.services.health_checker import HealthChecker

        statuses = HealthChecker().check_all(auto_start_lms=True)
        return next(s for s in statuses if s.name == "LM Studio")

    def test_starting_the_server_does_not_imply_the_chair_model_is_loaded(self) -> None:
        """`lms server start` brings the server up; it does not load a model.
        Reporting green off the back of a successful start hid exactly the
        condition that makes the chair unusable."""
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"), \
             patch("src.services.health_checker.LMStudioManager") as manager:
            manager.return_value.ensure_server_running.return_value = (True, "started")
            with _serving("some/other-model"):
                status = self._lm_studio_status()

        self.assertFalse(status.available)
        self.assertEqual(status.status, "service_unavailable")
        self.assertIn("pinned/model", status.detail)

    def test_a_healthy_chair_reports_which_model_it_resolved(self) -> None:
        with patch.object(config, "LM_STUDIO_CHAIR_MODEL", "pinned/model"):
            with _serving("pinned/model"):
                status = self._lm_studio_status()

        self.assertTrue(status.available)
        self.assertIn("pinned/model", status.detail)


class ChairTimeoutBudgetTest(unittest.TestCase):
    def test_the_timeouts_clear_the_measured_cost_of_the_pinned_model(self) -> None:
        """Measured on an M5 Pro: integrate 46.6-65.5s, context pack 21.7s.
        These are the numbers the config comment cites; if someone lowers the
        timeouts below them, the chair starts timing out on a healthy setup."""
        self.assertGreaterEqual(config.LM_STUDIO_CHAT_TIMEOUT_SECONDS, 66)
        self.assertGreaterEqual(config.LM_STUDIO_CONTEXT_TIMEOUT_SECONDS, 22)


if __name__ == "__main__":
    unittest.main()
