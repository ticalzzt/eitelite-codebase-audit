"""Compatibility shim: tical_code.seoul -> tical_code.core"""
import sys
import importlib
from pathlib import Path

_map = {
    "tool_executor": "tical_code.core.tool_executor",
    "unified_worker": "tical_code.core.unified_worker",
    "channel": "tical_code.core.channel",
    "llm_backend": "tical_code.core.llm_backend",
    "response_formatter": "tical_code.core.response_formatter",
    "prompt": "tical_code.core.prompt",
    "config": "tical_code.core.config",
    "proactive_gate": "tical_code.core.modules.proposal_gate",
}

# Register all seoul modules in sys.modules
for seoul_name, core_path in _map.items():
    seoul_full = "tical_code.seoul." + seoul_name
    if seoul_full not in sys.modules:
        try:
            sys.modules[seoul_full] = importlib.import_module(core_path)
        except ImportError:
            pass
