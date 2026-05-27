"""通道层 — 消息收发抽象。"""

import os
import json
import logging
import urllib.request, urllib.error
import ssl
import tempfile
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tical-code.channel")


class Message:
    """统一消息格式。"""
    def __init__(self, sender: str, content: str, source: str = "telegram",
                 chat_id: Optional[str] = None, raw: Optional[dict] = None,
                 media_data: Optional[list] = None):
        self.sender = sender
        self.content = content
        self.source = source
        self.chat_id = chat_id
        self.raw = raw or {}
        self.media_data = media_data or []  # [{"type":"image","mime":"image/png","data":"base64..."}, ...]


class Response:
    """统一响应格式。"""
    def __init__(self, content: str, target: str, source: str = "telegram",
                 chat_id: Optional[str] = None, raw: Optional[dict] = None):
        self.content = content
        self.target = target
        self.source = source
        self.chat_id = chat_id
        self.raw = raw or {}


class Channel:
    """消息通道抽象基类。"""
    def poll(self) -> list[Message]:
        raise NotImplementedError

    def send(self, response: Response) -> bool:
        raise NotImplementedError


class TelegramChannel(Channel):
    def __init__(self, token: str):
        self._api = f"https://api.telegram.org/bot{token}"
        self._last_update = 0
        self._telegram_file_api = f"https://api.telegram.org/file/bot{token}"
        logger.info("Telegram channel initialized")
        # Lazy-load STT model on first voice message
        self._stt_model = None

    def _transcribe_ogg(self, ogg_path: str) -> str:
        """Transcribe OGG audio file to text using faster-whisper."""
        import faster_whisper
        try:
            if self._stt_model is None:
                self._stt_model = faster_whisper.WhisperModel("tiny", device="cpu", compute_type="int8")
                logger.info("STT model loaded (tiny)")
            segments, _ = self._stt_model.transcribe(ogg_path, beam_size=5, language="zh")
            text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
            logger.info(f"STT transcribe: {len(text)} chars")
            return text
        except Exception as e:
            logger.warning(f"STT error: {e}")
            return ""

    def _download_media(self, msg: dict) -> list:
        """Download photo/document from Telegram, return [{"type":"image","mime":"...","data":"base64..."}]"""
        import base64, urllib.request
        media_list = []
        try:
            # Photo: take largest size (last in array)
            photo = msg.get("photo")
            if photo:
                file_id = photo[-1]["file_id"]
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    img_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=15).read()
                    b64 = base64.b64encode(img_data).decode()
                    mime = "image/jpeg"
                    if file_path.endswith(".png"): mime = "image/png"
                    elif file_path.endswith(".gif"): mime = "image/gif"
                    elif file_path.endswith(".webp"): mime = "image/webp"
                    media_list.append({"type": "image", "mime": mime, "data": b64})
                    logger.info(f"tg media: downloaded photo ({len(img_data)} bytes)")

            # Voice: download and transcribe
            voice = msg.get("voice")
            if voice:
                file_id = voice["file_id"]
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    ogg_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=30).read()
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                        f.write(ogg_data)
                        tmp_path = f.name
                    try:
                        transcript = self._transcribe_ogg(tmp_path)
                        if transcript:
                            media_list.append({"type": "transcript", "text": transcript})
                            logger.info(f"tg media: transcribed voice ({len(transcript)} chars)")
                    finally:
                        os.unlink(tmp_path)

            # Audio (music file): same as voice
            audio = msg.get("audio")
            if audio and not voice:
                file_id = audio["file_id"]
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    audio_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=30).read()
                    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
                        f.write(audio_data)
                        tmp_path = f.name
                    try:
                        transcript = self._transcribe_ogg(tmp_path)
                        if transcript:
                            media_list.append({"type": "transcript", "text": transcript})
                            logger.info(f"tg media: transcribed audio ({len(transcript)} chars)")
                    finally:
                        os.unlink(tmp_path)

            # Document (text files): download and read
            doc = msg.get("document")
            if doc:
                file_id = doc["file_id"]
                file_info = json.loads(urllib.request.urlopen(
                    f"{self._api}/getFile?file_id={file_id}", timeout=10).read())
                file_path = file_info.get("result", {}).get("file_path", "")
                if file_path:
                    fname = file_path.lower()
                    doc_data = urllib.request.urlopen(
                        f"{self._telegram_file_api}/{file_path}", timeout=30).read()
                    if doc_data:
                        if fname.endswith((".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log", ".py", ".js", ".html", ".css", ".sh")):
                            text = doc_data.decode("utf-8", errors="replace")
                            media_list.append({"type": "document_text", "text": text[:10000], "filename": file_path.split("/")[-1]})
                            logger.info(f"tg media: read doc ({len(text)} chars, capped to 10k)")
                        else:
                            media_list.append({"type": "document", "filename": file_path.split("/")[-1], "note": "binary file, cannot read"})
        except Exception as e:
            logger.warning(f"tg_media_download error: {e}")
        return media_list

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
                if not chat_id:
                    continue
                text = msg.get("text") or msg.get("caption") or ""
                text = text.strip()
                # Download media for supported types (photo only for now)
                media_data = self._download_media(msg) if (msg.get("photo") or msg.get("voice") or msg.get("audio") or msg.get("document")) else []
                # Build media annotation for text
                media_types = []
                has_actual_media = bool(media_data)
                if has_actual_media:
                    for md in media_data:
                        if md["type"] == "image": media_types.append("图片")
                        elif md["type"] == "transcript": media_types.append("语音（已转录）")
                        elif md["type"] == "document_text": media_types.append("文件内容（已读取）")
                        elif md["type"] == "document": media_types.append("文件（二进制）")
                        else: media_types.append("媒体")
                if msg.get("video"): media_types.append("视频")
                if media_types:
                    if has_actual_media:
                        note = "（用户发送了" + "、".join(media_types) + "，已加载可查看）"
                    else:
                        note = "（用户发送了" + "、".join(media_types) + "，无法查看）"
                    if text:
                        text = text + " " + note
                    else:
                        text = note
                if text and chat_id:
                    msgs.append(Message(sender="user", content=text,
                                        source="telegram", chat_id=chat_id,
                                        raw=msg, media_data=media_data))
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
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                if not body.get("ok"):
                    logger.warning(f"tg_send api_error: {body.get('description','?')} chat_id={response.chat_id}")
                    return False
                return True
        except Exception as e:
            logger.warning(f"tg_send error: {e} chat_id={response.chat_id}")
            return False


class TicalChatChannel(Channel):
    def __init__(self, base_url: str = "http://localhost:8080",
                 identity: str = "seoul", shared_key: str = os.environ.get("TICAL_CHAT_KEY", ""),
                 api_key: str = None):
        if api_key is not None:
            shared_key = api_key
        self._url = base_url.rstrip("/")
        self._identity = identity
        self._key = shared_key
        self._since = 0.0
        logger.info(f"tical-chat channel initialized: identity={identity} on {base_url}")

    def poll(self) -> list[Message]:
        try:
            url = f"{self._url}/v1/messages?since={self._since}&limit=5"
            req = urllib.request.Request(
                url, headers={"X-AI-Identity": self._identity,
                              "X-AI-Key": self._key})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            msgs = []
            for m in data.get("messages", []):
                if m.get("timestamp", 0) > self._since:
                    self._since = m["timestamp"]
                msgs.append(Message(
                    sender=m.get("sender", "unknown"),
                    content=m.get("content", ""),
                    source="tical-chat",
                    raw=m))
            return msgs
        except (ConnectionError, ConnectionRefusedError, urllib.error.URLError, TimeoutError) as e:
            logger.error(f"chat_poll error: {e}")
            return []
        except Exception as e:
            logger.error(f"chat_poll error: {e}")
            return []

    def send(self, response: Response) -> bool:
        """直接发消息队列，不走LLM推理。AI间消息用 POST /v1/messages。"""
        return self._send(response)

    def reply(self, response: Response) -> bool:
        """Alias for send(). Used by EITE-benchmark tests. Logs errors on failure."""
        result = self._send(response)
        if not result:
            logger.error("chat_reply failed")
        return result

    def _send(self, response: Response) -> bool:
        try:
            payload = json.dumps({
                "sender": self._identity,
                "target": response.target,
                "content": response.content,
            }).encode()
            req = urllib.request.Request(
                f"{self._url}/v1/messages", data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-AI-Identity": self._identity,
                    "X-AI-Key": self._key,
                }, method="POST")
            with urllib.request.urlopen(req, timeout=10):
                return True
        except Exception as e:
            logger.error(f"chat_send error: {e}")
            return False

    def reconnect(self, max_retries: int = 3, backoff: float = 2.0) -> bool:
        """Reconnect with exponential backoff. Retries on connection failure."""
        import time
        for attempt in range(max_retries):
            try:
                # Test connection by polling
                url = f"{self._url}/v1/messages?since=0&limit=1"
                req = urllib.request.Request(
                    url, headers={"X-AI-Identity": self._identity, "X-AI-Key": self._key})
                urllib.request.urlopen(req, timeout=5)
                logger.info("reconnect successful")
                return True
            except Exception as e:
                wait = backoff ** attempt
                logger.warning(f"retry {attempt+1}/{max_retries} in {wait}s: {e}")
                if attempt < max_retries - 1:
                    time.sleep(wait)
        logger.error("reconnect failed after all retries")
        return False
