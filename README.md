# EITElite

Minimal tical-code runtime — only the code that actually runs.

**34 files · 5,920 lines** (vs 100+ files · 47,481 lines in full tical-code)

## ⚠️ 唯一的坑

EITElite 和 tical-code 的 Python 包名都叫 `tical_code`。
**同一台机器上不能同时安装两个**，会互相覆盖。

每个 VPS 只装其中一个，就没问题。

## 安装

```bash
git clone https://github.com/ticalzzt/eitelite.git
cd eitelite
pip install -e .
```

## 运行

```bash
# 方式1：直接运行
python tical_code/core/unified_worker.py

# 方式2：通过 run.py
cp config.example.json config.json
python run.py
```

环境变量（优先级最高）：
- `WORKER_NAME` — worker 名称
- `OPENAI_API_KEY` / `DEEPSEEK_API_KEY` — API key
- `OPENAI_BASE_URL` / `DEEPSEEK_BASE_URL` — API endpoint
- `TICAL_CHAT_URL` — tical-chat 地址
- `TICAL_CHAT_KEY` — tical-chat 密钥

## 包含什么

从 `unified_worker.py` 入口 trace 出的完整 import 链：

| 模块 | 文件数 | 行数 | 职责 |
|------|--------|------|------|
| core/ | 16 | 5,138 | 主循环 + 工具 + LLM + 通道 + 记忆 |
| core/modules/ | 5 | 886 | 会话/上下文/循环检测/真实报告/提案门禁 |
| core/eite/ | 5 | 305 | 执行完整性验证 |
| vigil/ | 11 | 767 | 安全监控 |
| **总计** | **34** | **5,920** | |

24 个运行时工具：bash, file_read/write, memory_save/load, conv_search, state_save, chat_send, web_fetch, analyze_image, ocr, patch_file, browser_navigate/click/screenshot/extract, delegate_task, subagent_result/list, clarify_goal, cron_schedule/list/cancel, memory_fts_search

零外部依赖（纯 stdlib）。

## 不包含

- plugins/（browser, xurl, vision, trading, messenger, cloud_device）
- benchmarks/, tests/, research/, docs/
- ~50 个未接线的 core 模块

详见 [MANIFEST.md](MANIFEST.md)

## 与 tical-code 的关系

| | EITElite | tical-code |
|---|---|---|
| 仓库 | ticalzzt/eitelite | ticalzzt/tical-code |
| 包名 | tical_code（同名） | tical_code（同名） |
| 规模 | 34 文件 / 5,920 行 | 100+ 文件 / 47,481 行 |
| 外部依赖 | 0 | 多 |
| 场景 | 小VPS、最小部署 | 开发、完整功能 |
