from __future__ import annotations

import json
import urllib.error
import urllib.request

from src import config


class ChairAgent:
    def __init__(self, base_url: str = config.LM_STUDIO_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self._available_cache: bool | None = None
        self._model_cache: str | None = None
        self._reason: str = ""

    def is_localhost(self) -> bool:
        return any(host in self.base_url for host in ("localhost", "127.0.0.1", "::1"))

    def available(self) -> bool:
        available, _model = self._model_info()
        return available

    def unavailable_reason(self) -> str:
        """Why the chair is unusable, in Japanese, or "" when it is usable.

        Distinguishes "LM Studio is not answering" from "LM Studio is fine but
        the pinned chair model is not loaded" — those need different fixes and
        used to be indistinguishable, because an unloaded chair model simply
        meant some other model got used instead.
        """
        self._model_info()
        return self._reason

    def model_name(self) -> str:
        """The model this chair will actually consult, or "" when unusable."""
        return self._model_name()

    def invalidate_cache(self) -> None:
        """Drop the cached availability so a later probe re-checks the server.

        The cache is sticky for the lifetime of the instance, so it must be
        cleared whenever LM Studio may have come up after the first probe
        (for example right after `lms server start`), or whenever the user may
        have loaded or unloaded a model.
        """
        self._available_cache = None
        self._model_cache = None
        self._reason = ""

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int = 1200,
        timeout_seconds: int = config.LM_STUDIO_CHAT_TIMEOUT_SECONDS,
    ) -> str | None:
        if not self.is_localhost():
            return None
        model = self._model_name()
        if not model:
            return None
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        # Without this a reasoning model can burn the whole max_tokens budget
        # thinking and return an empty message, which every caller here reads
        # as "the chair is unavailable". See the config comment for the
        # measurements behind the default.
        effort = (config.LM_STUDIO_CHAIR_REASONING_EFFORT or "").strip()
        if effort:
            payload["reasoning_effort"] = effort
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
            message = data["choices"][0]["message"]
            content = (message.get("content") or "").strip()
            if not content:
                # A reasoning model that spent its whole budget thinking
                # returns HTTP 200 with an empty message. Callers treat a
                # falsy return as "chair unavailable", which is the right
                # degradation — but say why, because it is indistinguishable
                # from a dead server otherwise.
                reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
                print(
                    f"[ChairAgent] {model} returned an empty message "
                    f"(max_tokens={max_tokens}, reasoning={len(reasoning)} chars). "
                    f"If this model reasons by default, set "
                    f"config.LM_STUDIO_CHAIR_REASONING_EFFORT."
                )
            return content
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read().decode("utf-8")
            except Exception:
                err_body = str(exc)
            print(f"[ChairAgent] HTTP error {exc.code} from LM Studio: {err_body}")
            return None
        except Exception as exc:
            print(f"[ChairAgent] Error communicating with LM Studio: {type(exc).__name__}: {exc}")
            return None

    def _model_name(self) -> str:
        available, model = self._model_info()
        return model if available else ""

    def _model_info(self) -> tuple[bool, str]:
        """Resolve which model chairs this run, failing closed on a mismatch.

        When `config.LM_STUDIO_CHAIR_MODEL` names a model, that model must be
        loaded. Falling back to "whatever else is loaded" is exactly the
        behaviour this replaces: the chair would silently become a different
        model with different reasoning defaults, and nothing recorded it.
        """
        if self._available_cache is not None:
            return self._available_cache, self._model_cache or ""
        try:
            with urllib.request.urlopen(f"{self.base_url}/models", timeout=2) as response:
                data = json.loads(response.read().decode("utf-8"))
            reachable = response.status < 500
            model_ids = [
                entry.get("id")
                for entry in (data.get("data") or [])
                if isinstance(entry, dict) and entry.get("id")
            ]
        except Exception:
            return self._remember(False, "", "LM Studio に接続できません。")

        if not reachable:
            return self._remember(False, "", "LM Studio がエラーを返しています。")

        pinned = (config.LM_STUDIO_CHAIR_MODEL or "").strip()
        if pinned:
            if pinned in model_ids:
                return self._remember(True, pinned, "")
            loaded = "、".join(model_ids) if model_ids else "（なし）"
            return self._remember(
                False,
                "",
                f"議長AIモデル『{pinned}』が LM Studio にロードされていません。"
                f"現在ロード済み: {loaded}",
            )

        # Opt-in "use whatever is loaded" mode.
        if not model_ids:
            return self._remember(False, "", "LM Studio にモデルがロードされていません。")
        return self._remember(True, model_ids[0], "")

    def _remember(self, available: bool, model: str, reason: str) -> tuple[bool, str]:
        self._available_cache = available
        self._model_cache = model
        self._reason = reason
        return available, model


CHAIR_SYSTEM_PROMPT = """You are the local secretary-chair AI.
Summarize the user's request, coordinate Claude/Antigravity/Codex, compare answers,
extract missing points, contradictions, risks, and user decisions.
Keep user-facing output short. Do not suggest API-key billing, external paid APIs,
or dangerous operations without confirmation. Do not create or edit README.md as
internal AI memory. Inject only a short context pack into sub-agent prompts."""
