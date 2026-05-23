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

    # -- C.2.3: Multi-round tool calling loop -----------------

    def chat_with_tools(
        self,
        messages: List[dict],
        tools: Optional[List[dict]] = None,
        executor=None,
        max_rounds: int = 20,
        **kwargs,
    ) -> ChatResponse:
        """
        Multi-round tool calling loop.

        Spec:
        1. Send messages + tools to LLM
        2. If LLM returns tool_calls, execute tool, append result, goto 1
        3. If LLM returns text (no tool_calls), exit, return content
        4. Max 20 rounds, force exit returning last response
        5. Only send tools on first call (subsequent rounds omit them)

        Args:
            messages: Conversation history (modified in-place with tool results)
            tools: Tool definitions (sent only on first round)
            executor: Callable[[str, dict], dict] - executes tool by name+args
            max_rounds: Maximum iteration count (default 20)
            **kwargs: Passed through to chat()

        Returns:
            ChatResponse from the final (text) round
        """
        _accumulated_usage = {}
        _total_elapsed = 0.0
        _round_count = 0

        for _round_count in range(1, max_rounds + 1):
            # C.2.4: per-round timeout protection
            try:
                # Only send tools on first round
                round_tools = tools if _round_count == 1 else None
                resp = self.chat(messages, tools=round_tools, **kwargs)
            except Exception as e:
                logger.error(f"[C.2.4] chat exception round {_round_count}: {e}")
                return ChatResponse(
                    content=f"[C.2.4] LLM call crashed at round {_round_count}: {e}",
                    finish_reason="error",
                    usage=_accumulated_usage,
                    elapsed_ms=_total_elapsed,
                )

            # Accumulate usage/elapsed
            if resp.usage:
                for k, v in resp.usage.items():
                    _accumulated_usage[k] = _accumulated_usage.get(k, 0) + (v if isinstance(v, int) else 0)
            _total_elapsed += resp.elapsed_ms

            # C.2.3 rule 3: No tool_calls -> exit with this response
            if not resp.has_tool_calls:
                resp.usage = _accumulated_usage
                resp.elapsed_ms = _total_elapsed
                return resp

            # C.2.4: Validate tool_calls - skip malformed ones
            valid_tcs = [tc for tc in resp.tool_calls if tc.name and tc.name.strip()]
            if not valid_tcs:
                # All tool_calls were malformed -> inject error and retry
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({
                    "role": "tool",
                    "tool_call_id": "error",
                    "content": "[C.2.4] All tool_calls were malformed (empty name). Please reply directly.",
                })
                continue

            # C.2.3 rule 2: Execute tools, append results
            # Append assistant message with tool_calls
            assistant_msg = {"role": "assistant", "content": resp.content}
            assistant_msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                for tc in valid_tcs
            ]
            messages.append(assistant_msg)

            # C.2.4: Execute each tool, catch per-tool errors
            if executor:
                for tc in valid_tcs:
                    try:
                        result = executor(tc.name, tc.arguments)
                        result_str = json.dumps(result, ensure_ascii=False)[:2000]
                        logger.info(f"[C.2.3] round {_round_count} tool={tc.name} ok")
                    except Exception as e:
                        result_str = json.dumps({"error": str(e)}, ensure_ascii=False)
                        logger.warning(f"[C.2.4] round {_round_count} tool={tc.name} error: {e}")
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result_str,
                    })
            else:
                # No executor - mock empty results
                for tc in valid_tcs:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "{}",
                    })

        # C.2.3 rule 4: Max rounds reached
        logger.warning(f"[C.2.3] max_rounds {max_rounds} reached, forcing exit")
        return ChatResponse(
            content=f"[C.2.3] Max rounds ({max_rounds}) reached.",
            finish_reason="max_rounds",
            usage=_accumulated_usage,
            elapsed_ms=_total_elapsed,
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
