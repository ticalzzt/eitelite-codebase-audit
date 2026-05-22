"""Channel layer - message send/receive abstraction."""

import json
import os
import time
import logging
import urllib.request, urllib.error
import ssl
from typing import Optional

logger = logging.getLogger("tical-code.channel")

class Message:
    """Unified message format."""
    def __init__(self, sender: str, content: str, source: str = "telegram",
                 chat_id: Optional[str] = None, raw: Optional[dict] = None):
        self.sender = sender
        self.content = content
        self.source = source
        self.chat_id = chat_id
        self.raw = raw or {}

class Response:
    """Unified response format."""
    def __init__(self, content: str, target: str, source: str = "telegram",
                 chat_id: Optional[str] = None, raw: Optional[dict] = None):
        self.content = content
        self.target = target
        self.source = source
        self.chat_id = chat_id
        self.raw = raw or {}

class Channel:
    """Abstract message channel base class."""
    def poll(self) -> list[Message]:
        raise NotImplementedError

    def send(self, response: Response) -> bool:
        raise NotImplementedError

class TelegramChannel(Channel):
    def __init__(self, token: str):
        self._api = f"https://api.telegram.org/bot{token}"
        self._last_update = 0
        logger.info("Telegram channel initialized")

    def poll(self) -> list[Message]:
        import urllib.request
        try:
            url = f"{self._api}/getUpdates?offset={self._last_update + 1}&timeout=5"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read())
            msgs = []
            for u in data.get("result", []):
                self._last_update = u["update_id"]
                msg = u.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip()
                if text and chat_id:
                    msgs.append(Message(sender="user", content=text,
                                        source="telegram", chat_id=chat_id,
                                        raw=msg))
            return msgs
        except Exception as e:
            logger.warning(f"tg_poll error: {e}")
            return []

    def send(self, response: Response) -> bool:
        try:
            data = json.dumps({"chat_id": response.chat_id,
                               "text": response.content[:4000]}).encode()
            req = urllib.request.Request(
                f"{self._api}/sendMessage", data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception as e:
            logger.warning(f"tg_send error: {e}")
            return False

class TicalChatChannel(Channel):
    def __init__(self, base_url: str = "http://localhost:8080",
                 identity: str = "seoul", shared_key: str = os.environ.get("TICAL_CHAT_KEY", ""),
                 api_key: str = None):
        if api_key is not None:
            shared_key = api_key
        self._urls = [u.strip() for u in base_url.split(",") if u.strip()]
        if not self._urls:
            self._urls = ["http://localhost:8080"]
        self._identity = identity
        self._key = shared_key
        self._since = 0.0
        logger.info(f"tical-chat channel initialized: identity={identity} endpoints={self._urls}")

    def poll(self) -> list[Message]:
        for url in self._urls:
            try:
                fetch_url = f"{url.rstrip('/')}/v1/messages?since={self._since}&limit=5"
                req = urllib.request.Request(
                    fetch_url, headers={"X-AI-Identity": self._identity,
                                        "X-AI-Key": self._key})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read())
                msgs = []
                for m in data.get("messages", []):
                    if m.get("timestamp", 0) > self._since:
                        self._since = m["timestamp"]
                    target = m.get("target", "")
                    if target and target != self._identity:
                        continue
                    msgs.append(Message(
                        sender=m.get("sender", "unknown"),
                        content=m.get("content", ""),
                        source="tical-chat",
                        raw=m))
                if msgs:  # Only return if we got messages, otherwise try next endpoint
                    return msgs
                continue  # Empty result, try next endpoint
            except (ConnectionError, ConnectionRefusedError, urllib.error.URLError, TimeoutError) as e:
                logger.warning(f"chat_poll failed on {url}: {e}")
                continue
            except Exception as e:
                logger.warning(f"chat_poll error on {url}: {e}")
                continue
        logger.error("chat_poll: all endpoints failed")
        return []

    def _send(self, response: Response) -> bool:
        for url in self._urls:
            try:
                payload = json.dumps({
                    "sender": self._identity,
                    "target": response.target,
                    "content": response.content,
                }).encode()
                req = urllib.request.Request(
                    f"{url.rstrip('/')}/v1/messages", data=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-AI-Identity": self._identity,
                        "X-AI-Key": self._key,
                    }, method="POST")
                with urllib.request.urlopen(req, timeout=10):
                    return True
            except Exception as e:
                logger.warning(f"chat_send failed on {url}: {e}")
                continue
        logger.error("chat_send: all endpoints failed")
        return False

    def send(self, response: Response) -> bool:
        """Send message to queue - AI-to-AI via POST /v1/messages."""
        return self._send(response)

    def reply(self, response: Response) -> bool:
        """Alias for send(). Used by EITE benchmark tests."""
        result = self._send(response)
        if not result:
            logger.error("chat_reply failed")
        return result
