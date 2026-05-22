"""config module."""
import json
import os
from typing import Any, Dict

_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".eite", "eite_config.json")
_OVERRIDE_PATH = os.path.join(os.path.dirname(__file__), "eite_override.json")
_config: Dict[str, Any] = {}

def load_config() -> Dict[str, Any]:
    global _config
    try:
        with open(_CONFIG_PATH, "r") as f:
            _config = json.load(f)
    except FileNotFoundError:
        _config = {}
    # Allow
    if os.path.exists(_OVERRIDE_PATH):
        with open(_OVERRIDE_PATH, "r") as f:
            override = json.load(f)
        _config.update(override)
    return _config

def get(key: str, default=None):
    return _config.get(key, default)

def set(key: str, value):
    _config[key] = value

def save():
    with open(_CONFIG_PATH, "w") as f:
        json.dump(_config, f, indent=2)

load_config()
