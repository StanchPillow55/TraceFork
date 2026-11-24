"""OpenAI-compatible JSON extraction client.

The client records token usage only when the response includes it. It does not
invent token counts. Callers still have to ground every quotation.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

import httpx

from exploration.backend.config import HTTP_TIMEOUT_SECONDS, llm_api_key, llm_base_url, llm_model


class LLMNotConfigured(RuntimeError):
    pass


ANTHROPIC_MAX_TOKENS = 2048


class LLMResponseNotJSON(ValueError):
    """The model answered, but no complete JSON object could be parsed.

    The message records why (finish reason, length) without the response text.
    """


class LocalExtractionUnavailable(LLMNotConfigured):
    """Local generation does not finish inside the extraction deadline."""


def local_ollama_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def anthropic_endpoint(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host == "api.anthropic.com" or host.endswith(".anthropic.com")


def strip_json_fence(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: cleaned.rfind("```")]
    return cleaned.strip()


def extract_json_object(text: str) -> str:
    """Return the first balanced top-level JSON object in text.

    Anthropic models sometimes wrap the object in prose or a sentence. This
    recovers the object without inventing content; if none is found, the
    original text is returned so the caller's json.loads raises as before.
    """
    cleaned = strip_json_fence(text)
    objects = _balanced_objects(cleaned)
    if len(objects) > 1:
        # Prose such as "Example only: {...}. {...}" puts an illustrative
        # object before the answer; the final complete object is the payload.
        for candidate in reversed(objects):
            try:
                json.loads(candidate)
            except ValueError:
                continue
            return candidate
    start = cleaned.find("{")
    if start == -1:
        return cleaned
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : index + 1]
    return cleaned[start:]


def _balanced_objects(text: str) -> list[str]:
    """Every balanced top-level {...} span in text, in order."""
    found: list[str] = []
    depth = 0
    in_string = False
    escape = False
    start = -1
    for index, char in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"' and depth > 0:
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                found.append(text[start : index + 1])
    return found


class OpenAICompatibleClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float = 15.0,
    ):
        self.api_key = api_key if api_key is not None else llm_api_key()
        self.base_url = (base_url or llm_base_url()).rstrip("/")
        self.model = model or llm_model()
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def complete_json(self, system: str, user: str) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self.api_key:
            raise LLMNotConfigured("LLM_API_KEY and OPENAI_API_KEY are missing")
        if local_ollama_endpoint(self.base_url) and os.environ.get("LLM_FORCE_LOCAL") != "1":
            raise LocalExtractionUnavailable(
                "Local LLM extraction is experimental for "
                f"{self.model} at {self.base_url}. The request was not sent, and no relationships were invented."
            )
        url = f"{self.base_url}/chat/completions"
        is_anthropic = anthropic_endpoint(self.base_url)
        # Anthropic models (Haiku especially) are more verbose than gpt-4o-mini,
        # and the extraction JSON can be large; 300 tokens truncates it mid-string.
        # 2048 (was 1024): two live runs (2026-11-14, 2026-11-20) recorded a JSONDecodeError,
        # consistent with an answer cut off mid-object. The prompt also caps list lengths.
        max_tokens = ANTHROPIC_MAX_TOKENS if is_anthropic else 300
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if not is_anthropic:
            # Newer Claude models reject `temperature` (deprecated). Only send
            # it to endpoints that still accept it.
            payload["temperature"] = 0
        if is_anthropic:
            # Anthropic's OpenAI-compatibility layer rejects response_format
            # {"type": "json_object"} with a 400. It only accepts json_schema
            # with strict:true, which cannot express this loosely-typed nested
            # shape. Drop the field and rely on the system prompt instead; the
            # caller already strips code fences and parses defensively.
            payload["messages"][0]["content"] = (
                f"{system}\n\nReturn only a single JSON object. No prose, no code fences."
            )
        else:
            payload["response_format"] = {"type": "json_object"}
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        choice = body["choices"][0]
        content = choice["message"]["content"] or ""
        try:
            parsed = json.loads(extract_json_object(content))
        except ValueError as exc:
            finish = choice.get("finish_reason") or "unknown"
            hint = f"; output hit max_tokens={max_tokens}" if finish == "length" else ""
            raise LLMResponseNotJSON(
                f"response was not a complete JSON object (finish_reason={finish}, {len(content)} chars{hint})"
            ) from exc
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        usage.setdefault("model", self.model)
        usage.setdefault("backend", "openai-compatible")
        return parsed, usage

    def complete_text(self, system: str, user: str) -> tuple[str, dict[str, Any]]:
        """Return free-form text (not JSON) from the chat endpoint.

        Shares the Anthropic-compatibility handling with complete_json but does
        not request or parse JSON. Used for prose answer synthesis.
        """
        if not self.api_key:
            raise LLMNotConfigured("LLM_API_KEY and OPENAI_API_KEY are missing")
        if local_ollama_endpoint(self.base_url) and os.environ.get("LLM_FORCE_LOCAL") != "1":
            raise LocalExtractionUnavailable(
                "Local LLM is experimental for "
                f"{self.model} at {self.base_url}. The request was not sent."
            )
        url = f"{self.base_url}/chat/completions"
        is_anthropic = anthropic_endpoint(self.base_url)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if not is_anthropic:
            payload["temperature"] = 0.2
        timeout = httpx.Timeout(self.timeout, connect=10.0)
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        usage.setdefault("model", self.model)
        usage.setdefault("backend", "openai-compatible")
        return content, usage
