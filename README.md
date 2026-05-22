# EITElite

Minimal tical-code runtime — only the code that actually runs.

**4,944 lines · 28 files** (vs 47,481 lines · 97 files in full tical-code)

## What's included

The 17 core runtime files extracted from `unified_worker.py` import chain:

| File | Lines | Role |
|------|-------|------|
| unified_worker.py | 536 | Main loop |
| tool_executor.py | 971 | 23 tools |
| cron_scheduler.py | 702 | Cron engine |
| clarify.py | 621 | Goal clarification |
| memory_store.py | 543 | Memory storage |
| llm_backend.py | 186 | LLM calls (stdlib urllib) |
| channel.py | 160 | Telegram/tical-chat |
| prompt.py | 105 | System prompt |
| response_formatter.py | 79 | Output formatting |
| config.py | 60 | Config loader |
| eite/ | 15 | EITE init |
| modules/ (5 files) | 886 | Session/context/loop/truth/gate |
| vigil/ (11 files) | 767 | Safety monitoring |

## Dependencies

- Python 3.10+
- PyYAML (optional, for vigil config)
- No external HTTP libraries (uses stdlib urllib)

## Usage

```bash
cp config.example.json config.json
# edit config.json with your keys
python run.py
```

## What's NOT included

- plugins/ (browser, xurl, vision, trading, messenger, cloud_device)
- benchmarks/, tests/, research/, docs/
- ~80 unused core modules (self_repair, sandbox, detection, etc.)
