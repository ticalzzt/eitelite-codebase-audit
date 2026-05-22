"""Module 2: Context Compaction - token-aware conversation trimming."""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

logger = logging.getLogger("tical-code.context_compactor")

class ContextCompactor:
    """SMART compaction: prune → summarize → fallback."""

    _SUMMARY_PROMPT = (
        "Summarize the following conversation, preserving key decisions, "
        "verified results, and user preferences. Discard greetings, failed "
        "attempts, and intermediate reasoning. Be concise (max 500 tokens)."
    )

    def __init__(self, max_tokens: int = 6000, keep_recent: int = 6):
        self.max_tokens = max_tokens
        self.keep_recent = keep_recent

    @staticmethod
    def _count_cjk(text: str) -> int:
        return sum(
            1 for ch in text
            if "\u4e00" <= ch <= "\u9fff" or "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af"
        )

    def estimate_tokens(self, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            text = msg.get("content", "")
            if msg.get("tool_calls"):
                text += json.dumps(msg["tool_calls"])
            cjk = self._count_cjk(text)
            other = max(0, len(text) - cjk)
            total += (cjk + 1) // 2 + (other + 3) // 4 + 4  # +4 overhead per msg
        return total

    def needs_compaction(self, messages: list[dict]) -> bool:
        return self.estimate_tokens(messages) > self.max_tokens

    def compact(self, messages: list[dict], llm_call_fn: Callable) -> list[dict]:
        try:
            if not messages:
                return messages

            system_msg = messages[0]  # never touch
            start_idx = 1 if messages[0].get("role") == "system" else 0

            # Phase 1: Prune LOW importance — truncate long tool outputs
            pruned: list[dict] = [system_msg] if start_idx else []
            for i in range(start_idx, len(messages)):
                msg = messages[i]
                new_msg = dict(msg)
                content = new_msg.get("content", "")
                if new_msg.get("role") == "tool" and len(content) > 500:
                    new_msg["content"] = (
                        f"[output truncated: {content[:100]}... ({len(content)} chars total)]"
                    )
                pruned.append(new_msg)

            if not self.needs_compaction(pruned):
                return pruned

            # Phase 2: LLM summarization of older messages
            if len(pruned) <= self.keep_recent + start_idx:
                # Too short to summarize meaningfully
                tail = pruned[-self.keep_recent:]
                return ([system_msg] if start_idx else []) + tail

            to_summarize = pruned[start_idx:-self.keep_recent]
            intact_tail = pruned[-self.keep_recent:]

            summary_text = self._generate_summary(to_summarize, llm_call_fn)
            if summary_text:
                summary_msg = {"role": "system", "content": f"[Context summary]\n{summary_text}"}
                result = ([system_msg] if start_idx else []) + [summary_msg] + intact_tail
                return result

            # Phase 3: Fallback — keep system prompt + recent only
            result = ([system_msg] if start_idx else []) + intact_tail
            return result

        except Exception:
            logger.exception("compaction failed catastrophically")
            try:
                return [messages[0]] + messages[-self.keep_recent:]
            except Exception:
                return messages[-self.keep_recent:]

    def _generate_summary(self, messages: list[dict], llm_call_fn: Callable) -> str:
        try:
            parts = []
            for m in messages:
                role = m.get("role", "?")
                content = m.get("content", "")
                parts.append(f"[{role}] {content}")
            dialogue = "\n".join(parts)

            prompt = f"{self._SUMMARY_PROMPT}\n\n---\n{dialogue}\n---"
            call_msgs = [
                {"role": "system", "content": "You are a helpful summarizer."},
                {"role": "user", "content": prompt},
            ]
            response = llm_call_fn(call_msgs)
            if isinstance(response, dict):
                return (response.get("content") or "").strip()
            return str(response).strip()
        except Exception:
            logger.exception("LLM summary failed")
            return ""

    def reset(self) -> None:
        return
