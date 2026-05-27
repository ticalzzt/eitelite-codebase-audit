"""LLM backend - unified AI calling with multi-provider support."""

import json
import logging
import os
import ssl
import time
import urllib.request
import urllib.error
from pathlib import Path

logger = logging.getLogger("tical-code.llm")

TICAL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class LLMBackend:
    """Base class for AI calling backends."""

    def call(self, messages: list, tools: list = None,
             max_tokens: int = 4000) -> dict:
        """Call LLM with messages and tools. Handles errors, timeout, empty responses."""
        try:
            result = self._do_call(messages, tools, max_tokens)
            # _do_call returns {"content": ..., "tool_calls": [...]} already processed
            content = result.get("content", "")
            if not content and not result.get("tool_calls"):
                logger.error("LLM returned empty response")
                return {"error": "empty_response", "content": ""}
            return result
        except TimeoutError as e:
            logger.error(f"LLM call timed out: {e}")
            return {"error": f"timeout: {e}", "content": ""}
        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            return {"error": str(e), "content": ""}

    def _do_call(self, messages, tools, max_tokens):
        """Actual LLM call - implemented by subclasses."""
        raise NotImplementedError

class OpenAIBackend(LLMBackend):
    """OpenAI-compatible backend with retry, circuit breaker, and fallback."""

    def __init__(self, api_key: str, base_url: str, model: str):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._is_mimo = "mimo" in base_url.lower()
        self._fallback_model = os.environ.get("LLM_FALLBACK_MODEL", "")
        self._temperature = float(os.environ.get("LLM_TEMPERATURE", "0.1"))
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._providers = {
            "deepseek": {"base_url": "https://api.deepseek.com/v1", "default_model": "deepseek-v4-flash"},
            "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "default_model": "qwen-plus"},
            "openai": {"base_url": "https://api.openai.com/v1", "default_model": "gpt-4o"},
            "openrouter": {"base_url": "https://openrouter.ai/api/v1", "default_model": "openai/gpt-4o"},
        }
        logger.info(f"OpenAI backend: model={model} url={base_url}"
                    + (f" fallback={self._fallback_model}" if self._fallback_model else ""))

    def get_model(self) -> str:
        """Return current model name."""
        return self._model

    def get_provider(self) -> str:
        """Return current provider type."""
        for name, info in self._providers.items():
            if info["base_url"] == self._base_url or self._base_url.startswith(info["base_url"].rstrip("/").split("/")[2]):
                return name
        return "custom"

    def set_model(self, model: str, api_key: str = None, base_url: str = None):
        """Switch model at runtime. Can change provider, key, and endpoint."""
        old = f"{self._model} @ {self._base_url[:40]}..."
        self._model = model
        if api_key:
            self._api_key = api_key
        if base_url:
            self._base_url = base_url.rstrip("/")
            self._is_mimo = "mimo" in base_url.lower()
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        logger.info(f"Model switched: {old} → {self._model} @ {self._base_url[:50]}...")
        return {"ok": True, "model": self._model, "provider": self.get_provider(), "base_url": self._base_url}

    def list_models(self) -> list:
        """List available built-in providers and models."""
        result = []
        for name, info in self._providers.items():
            result.append({"provider": name, "base_url": info["base_url"], "default_model": info["default_model"]})
        return result

    def _do_call(self, messages, tools, max_tokens):
        """Call LLM with retry backoff + circuit breaker + fallback."""
        now = time.time()
        if self._circuit_open_until > now:
            logger.warning("Circuit breaker open — fast-failing")
            return {"content": "[LLM circuit breaker open]", "tool_calls": []}

        body = {"model": self._model, "messages": messages,
                "max_tokens": max_tokens,
                "temperature": self._temperature}
        if self._is_mimo and tools:
            body["tools"] = tools
        if tools:
            body["tools"] = tools

        if self._is_mimo:
            auth_header = self._api_key
            auth_key = "api-key"
        else:
            auth_header = f"Bearer {self._api_key}"
            auth_key = "Authorization"

        # Retry loop with exponential backoff
        RETRIABLE = (urllib.error.URLError, TimeoutError, ConnectionError,
                      ConnectionResetError, ConnectionRefusedError,
                      OSError)  # OSError catches IncompleteRead etc.

        for attempt in range(3):
            req = urllib.request.Request(
                f"{self._base_url}/chat/completions",
                data=json.dumps(body).encode(),
                headers={auth_key: auth_header,
                         "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=60,
                                            context=ssl.create_default_context()) as resp:
                    data = json.loads(resp.read())
                    break
            except urllib.error.HTTPError as e:
                detail = e.read().decode()[:200]
                logger.error(f"LLM HTTP {e.code}: {detail}")
                if e.code >= 500:
                    if attempt < 2:
                        time.sleep(2 ** attempt)
                        continue
                return {"content": f"[LLM error: HTTP {e.code}]",
                        "tool_calls": []}
            except RETRIABLE as e:
                if attempt == 2:
                    raise
                logger.warning(f"LLM retry {attempt + 1}/3: {e}")
                time.sleep(2 ** attempt)
                # Try fallback model on last attempt
                if attempt == 1 and self._fallback_model:
                    logger.info(f"Switching to fallback model: {self._fallback_model}")
                    body["model"] = self._fallback_model
        else:
            # All retries exhausted
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                self._circuit_open_until = time.time() + 30
                logger.error("Circuit breaker engaged for 30s")
            return {"content": "[LLM unreachable after 3 retries]",
                    "tool_calls": []}

        # Success — reset circuit breaker
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

        msg = data.get("choices", [{}])[0].get("message", {})
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            func = tc.get("function", {})
            args_str = func.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            tool_calls.append({
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "args": args,
            })
        content = msg.get("content", "") or ""
        reasoning = msg.get("reasoning_content", "") or ""
        # MiMo often puts response in reasoning_content, fallback content when empty
        if not content and reasoning:
            content = reasoning
        return {"content": content,
                "tool_calls": tool_calls,
                "reasoning_content": reasoning}

def _load_configs() -> list[tuple]:
    """Load all available configs, return [(cfg_dict, source_path)].

    DEPRECATED: Use tical_code.core.config.load_config() instead.
    This function reads from ~/tical_workers/*/config.json which is a legacy path.
    Kept for backward compatibility only.
    """
    import warnings
    warnings.warn("_load_configs() is deprecated, use config.load_config()", DeprecationWarning, stacklevel=2)
    configs = []

    for pattern in [
        os.path.expanduser("~/tical_workers/*/config.json"),
        "/root/tical_workers/*/config.json",
    ]:
        import glob
        for path in sorted(glob.glob(pattern)):
            try:
                cfg = json.loads(Path(path).read_bytes())
                if cfg.get("ai_endpoint") and cfg.get("ai_key"):
                    configs.append((cfg, path))
                    logger.info(f"loaded config from {path}")
            except Exception:
                continue
    return configs

def create_llm_backend(backend: str = "auto", model: str = "",
                       api_key: str = "", base_url: str = "") -> LLMBackend:
    """Factory: create LLM backend. Env vars first, config files fallback.

    DEPRECATED: Use tical_code.core.config.load_config() + DeepSeekProvider instead.
    """
    env_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    env_base = os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("DEEPSEEK_BASE_URL", "")

    # env first
    if env_key and not api_key:
        api_key = env_key
    if env_base and not base_url:
        base_url = env_base

    # config files fallback
    if not api_key or not base_url:
        configs = _load_configs()
        if configs:
            cfg, src = configs[0]
            if not api_key:
                api_key = cfg["ai_key"]
            if not base_url:
                base_url = cfg["ai_endpoint"]
            if not model:
                model = cfg.get("ai_model", "")
            logger.info(f"fallback to config {src}: {cfg.get('ai_model','')} @ {base_url[:50]}")
        else:
            raise ValueError("No LLM config found (env or ~/tical_workers/*/config.json)")

    return OpenAIBackend(api_key=api_key, base_url=base_url, model=model)
