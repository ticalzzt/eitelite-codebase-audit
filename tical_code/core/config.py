"""Unified config loader - single source of truth for all modules."""
import json
import os
from pathlib import Path


def load_config() -> dict:
    """Load worker config from TICOBOT_DIR/config.json, worker_config.json, env."""
    base = os.environ.get("TICOBOT_DIR", "")
    if not base:
        for loc in [Path.home() / "tical-code", Path("/root/tical-code")]:
            try:
                if loc.exists():
                    base = str(loc)
                    break
            except PermissionError:
                continue

    cfg = {
        "workspace": base or str(Path.home()),
        "tg_token": os.environ.get("TG_BOT_TOKEN", ""),
        "chat_url": os.environ.get("TICAL_CHAT_URL", ""),
        "chat_key": os.environ.get("TICAL_CHAT_KEY", ""),
    }

    # worker_config.json (legacy - has tg_token + name)
    cfg["name"] = os.environ.get("WORKER_NAME", "seoul")
    # tg_token 仅从环境变量 TG_BOT_TOKEN 读取（不从文件）
    wc_path = Path(base) / "worker_config.json"
    if wc_path.exists():
        try:
            wc = json.loads(wc_path.read_text())
            if wc.get("name"):
                cfg["name"] = wc["name"]
        except Exception:
            pass

    # Also try cwd (systemd WorkingDirectory)
    if not base:
        cwd = os.getcwd()
        if Path(cwd).exists():
            base = cwd

    # config.json (AI settings)
    config_path = Path(base) / "config.json"
    if not config_path.exists():
        cwd_cfg = Path(os.getcwd()) / "config.json"
        if cwd_cfg.exists():
            config_path = cwd_cfg
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text())
            if file_cfg.get("ai_endpoint"):
                cfg["ai_endpoint"] = file_cfg["ai_endpoint"]
            if file_cfg.get("ai_key"):
                cfg["ai_key"] = file_cfg["ai_key"]
            if file_cfg.get("ai_model"):
                cfg["ai_model"] = file_cfg["ai_model"]
        except Exception:
            pass

    # data_collection from config.json
    try:
        if "data_collection" in file_cfg:
            cfg["data_collection"] = file_cfg["data_collection"]
    except (NameError, Exception):
        pass
    except Exception:
        pass

    # Env overrides (highest priority)
    env_name = os.environ.get("WORKER_NAME", "")
    if env_name:
        cfg["name"] = env_name
    env_key = os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    if env_key:
        cfg["ai_key"] = env_key
    env_base = os.environ.get("OPENAI_BASE_URL", "") or os.environ.get("DEEPSEEK_BASE_URL", "")
    if env_base:
        cfg["ai_endpoint"] = env_base

    return cfg


def get_data_collection_config(cfg: dict) -> dict:
    """Return data_collection settings with defaults."""
    return {
        "enabled": cfg.get("data_collection", {}).get("enabled", False),
        "target_url": cfg.get("data_collection", {}).get("target_url", "https://bench.ticalasi.com/api/trace"),
        "batch_size": int(cfg.get("data_collection", {}).get("batch_size", 10)),
    }
