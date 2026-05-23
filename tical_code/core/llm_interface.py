"""
EITElite LLM Interface — clean abstraction for LLM providers.

Provides:
- ChatMessage, ToolCall, ChatResponse: structured data types
- LLMProvider(ABC): provider interface
- DeepSeekProvider: concrete implementation for DeepSeek API

C.2.1: chat() accepts tools parameter
C.2.2: returns ChatResponse with parsed ToolCall objects
"""

import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tical-code.llm_interface")


# ═══════════════════════════════════════════════════════════════
# C.2.2: Data types
# ═══════════════════════════════════════════════════════════════

@dataclass
class ToolCall:
    """A single tool/function call returned by the LLM."""
    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ChatResponse:
    """Structured response from an LLM chat completion."""
    content: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: Dict[str, int] = field(default_factory=dict)
    elapsed_ms: float = 0.0

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


# ═══════════════════════════════════════════════════════════════
# C.2.1: Provider interface
# ═══════════════════════════════════════════════════════════════

class LLMProvider(ABC):
    """Abstract LLM provider interface."""

    @abstractmethod
    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        **kwargs,
    ) -> ChatResponse:
        ...

    @abstractmethod
    def get_model(self) -> str:
        ...

    def set_model(self, model: str, api_key: str = None, base_url: str = None):
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════
# C.2.1 + C.2.2: DeepSeekProvider
# ═══════════════════════════════════════════════════════════════

class DeepSeekProvider(LLMProvider):
    """
    DeepSeek (OpenAI-compatible) provider.
    
    C.2.1: chat() accepts tools parameter → forwards to API
    C.2.2: parses tool_calls from response → returns ChatResponse with ToolCall list
    """

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "https://api.deepseek.com/v1",
        model: str = "deepseek-chat",
        max_tokens: int = 4000,
        timeout: int = 30,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout

    # ── C.2.1: chat() with tools parameter ──────────────────

    def chat(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        **kwargs,
    ) -> ChatResponse:
        """
        Send a chat completion request.
        
        Args:
            messages: Conversation history (OpenAI format)
            tools: Optional list of tool definitions (sent to API)
            **kwargs: Overrides for model, max_tokens, timeout
        
        Returns:
            ChatResponse with content and/or tool_calls
        """
        model = kwargs.get("model", self._model)
        max_tokens = kwargs.get("max_tokens", self._max_tokens)
        timeout = kwargs.get("timeout", self._timeout)

        # Build request body
        body = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
        }
        # C.2.1: tools parameter → API body
        if tools:
            body["tools"] = tools

        t0 = time.time()
        try:
            data = self._post(body, timeout)
            elapsed = (time.time() - t0) * 1000
            return self._parse_response(data, elapsed)
        except urllib.error.HTTPError as e:
            elapsed = (time.time() - t0) * 1000
            error_body = e.read().decode("utf-8", errors="replace")[:200]
            logger.error(f"LLM HTTP {e.code}: {error_body}")
            hint = self._error_hint(e.code, error_body)
            return ChatResponse(
                content=f"[LLM error: HTTP {e.code}.{hint}]",
                finish_reason="error",
                elapsed_ms=elapsed,
            )
        except Exception as e:
            elapsed = (time.time() - t0) * 1000
            logger.error(f"LLM call failed: {e}")
            return ChatResponse(
                content=f"[LLM error: {e}]",
                finish_reason="error",
                elapsed_ms=elapsed,
            )

    # ── C.2.2: Parse tool_calls from response ───────────────

    def _parse_response(self, data: dict, elapsed_ms: float) -> ChatResponse:
        """Parse API response into ChatResponse with ToolCall objects."""
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        content = msg.get("content", "") or ""

        # C.2.2: Extract tool_calls
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            raw_args = func.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=func.get("name", ""),
                arguments=args if isinstance(args, dict) else {},
            ))

        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason", "stop"),
            usage=data.get("usage", {}),
            elapsed_ms=elapsed_ms,
        )

    # ── Error hint ──────────────────────────────────────────

    @staticmethod
    def _error_hint(status_code: int, body: str) -> str:
        if status_code in (401, 403):
            return " API key error — use switch_model to set a valid key."
        elif status_code == 400:
            return " Model may be unavailable — use list_models + switch_model to change."
        elif status_code == 429 or "rate" in body.lower():
            return " Rate limited — retry or switch_model to a different model."
        return ""

    # ── HTTP POST ───────────────────────────────────────────

    def _post(self, body: dict, timeout: int) -> dict:
        """POST to /chat/completions, return parsed JSON."""
        data_bytes = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data_bytes,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read())

    # ── Model management ────────────────────────────────────

    def get_model(self) -> str:
        return self._model

    def set_model(self, model: str, api_key: str = None, base_url: str = None):
        self._model = model
        if api_key:
            self._api_key = api_key
        if base_url:
            self._base_url = base_url.rstrip("/")
        logger.info(f"[llm_interface] model switched to {model}")

    def get_provider(self) -> str:
        return "deepseek"
