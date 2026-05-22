#!/usr/bin/env python3
"""EITElite — minimal tical-code worker runner.

Usage:
    python run.py                    # reads config.json
    python run.py --config my.json   # custom config
"""
import json
import sys
import os

def main():
    config_path = "config.json"
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        config_path = sys.argv[idx + 1]

    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found")
        sys.exit(1)

    with open(config_path) as f:
        cfg = json.load(f)

    from tical_code.core.unified_worker import Worker
    worker = Worker(cfg)
    worker.run()

if __name__ == "__main__":
    main()
