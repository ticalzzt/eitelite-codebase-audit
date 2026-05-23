"""Heartbeat stub — EITElite lite version."""

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("tical-code.heartbeat")

class HeartbeatConfig:
    """Minimal heartbeat config."""
    
    def __init__(self, heartbeat_interval: float = 300.0, 
                 timeout: float = 30.0, max_missed: int = 3):
        self.heartbeat_interval = heartbeat_interval
        self.timeout = timeout
        self.max_missed = max_missed


class HeartbeatManager:
    """Minimal heartbeat manager — no-op stub for EITElite."""
    
    def __init__(self, default_config: Optional[HeartbeatConfig] = None):
        self._config = default_config or HeartbeatConfig()
        self._running = False
        self._thread = None
    
    def start(self) -> None:
        """Start heartbeat thread."""
        self._running = True
        logger.info("Heartbeat started (stub)")
    
    def stop(self) -> None:
        """Stop heartbeat thread."""
        self._running = False
        logger.info("Heartbeat stopped (stub)")
    
    def ping(self, source: str = "") -> bool:
        """Send a heartbeat ping."""
        return True
    
    def add_handler(self, handler: Callable) -> None:
        """Register a heartbeat handler."""
        pass
