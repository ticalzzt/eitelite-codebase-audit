"""Tests for channel message types."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

def test_message_creation():
    from tical_code.core.channel import Message
    m = Message(sender="test", content="hello", source="test")
    assert m.sender == "test"
    assert m.content == "hello"
    assert m.source == "test"

def test_response_creation():
    from tical_code.core.channel import Response
    r = Response(content="reply", target="user", source="test", chat_id="123")
    assert r.content == "reply"
    assert r.target == "user"
    assert r.chat_id == "123"
