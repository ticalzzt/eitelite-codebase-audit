# EITElite vs Hermes Agent — 能力对审计审计

## 总体指标

| 维度 | Hermes Agent | EITElite | 占比 |
|------|-------------|----------|------|
| 代码量 | ~120,000+ lines | 11,138 lines | ~9% |
| 工具数 | 50+ (18 toolsets) | 34 工具 | ~68% |
| 系统组件 | 39+ | 26 模块 | ~67% |
| 平台通道 | 15+ | 2 (TG + tical-chat) | ~13% |
| LLM 提供商 | 20+ | 1 (DeepSeek) | ~5% |
| 安装方式 | pip/docker | git clone | 自建 |

---

## 一、优势 — EITElite 具备且达到或超过 Hermes 的部分

### ✅ 1. 核心循环 (100% 达到)
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| LLM 对话循环 | ✅ | ✅ run_conversation | unified_worker.py main loop |
| 消息轮询 | ✅ | ✅ | Telegram + tical-chat 双通道 |
| 工具调用与分发 | ✅ | ✅ | tool_executor.py 34 工具 |
| 上下文压缩 | ✅ | ✅ | context_compactor.py |
| 循环检测 | ✅ | ✅ | loop_detector.py |

### ✅ 2. 工具系统 (90% 达到)
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| 文件读写 | ✅ | ✅ | file_read, file_write |
| Shell 执行 | ✅ | ✅ | bash_execute |
| Web 搜索 | ✅ | ✅ | web_search, web_fetch |
| 浏览器自动化 | ✅ | ✅ | browser_navigate/click/screenshot/extract + cloud_device.* |
| 图片分析 | ✅ | ✅ | analyze_image, ocr (via VisionPlugin) |
| 社会媒体 | ✅ | xurl | X/Twitter 发布/回复/时间线 |
| 子任务委派 | ✅ | ✅ | delegate_task + subagent_result/list |
| Cron 定时任务 | ✅ | ✅ | cron_schedule/list/cancel |
| 记忆系统 | ✅ | ✅ | memory_save/load + FTS search |
| 会议搜索 | ✅ | ✅ | conv_search (对话历史搜索) |
| 补丁编辑 | ✅ | ✅ | patch_file |
| 聊天发送 | ✅ | ✅ | chat_send (tical-chat) |

**✅ 特有工具：** cloud_device.*（playwright 浏览器自动化）+ execute_code（安全 Python 沙箱）— Hermes 没有等价物

### ✅ 3. 记忆系统 (80% 达到)
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| 持久化记忆 | ✅ | ✅ | memory.py + memory_store.py (949+543 lines) |
| FTS 全文搜索 | ✅ | ✅ | memory_fts_search |
| 跨会话记忆 | ✅ | ✅ | memory.py |
| 记忆文件存储 | ✅ | ✅ | ~/.tical-code/memory/ |
| 用户个人资料 | ✅ | ✅ | 有 basic 实现 |

### ✅ 4. EITE 身份系统 (100% — EITElite 独有)
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| 身份签名 | ❌ | ✅ | EITE signature + verify |
| 工具调用验证 | ❌ | ✅ | Force-Verify framework |
| 提议审批门 | ❌ | ✅ | proposal_gate |
| 真实报告 | ❌ | ✅ | TruthfulReporter |
| 行为审计 | ❌ | ✅ | EITE engine + patrol |
| 证据哈希 | ❌ | ✅ | cloud_device + trace |

### ✅ 5. 安全机制 (85% 达到)
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| 密文脱敏 | ✅ | ✅ | redact_secrets ✅ |
| 工作区隔离 | ✅ | ✅ | 写操作限制在工作区 |
| 工作区隔离文件 | ✅ | ✅ | tool_executor path protection |
| 命令审批 | ✅ | ✅ | proposal_gate |
| 工作区隔离系统路径保护 | ✅ | ✅ | PROTECTED_SYSTEM_PATHS |

---

## 二、不足 — EITElite 缺失或不足的部分

### ❌ 6. LLM 提供商支持 (Hermes: 20+ / EITElite: 1)
| 提供商 | Hermes | EITElite | 影响 |
|--------|--------|----------|------|
| OpenRouter | ✅ | ❌ | 无法切换模型池 |
| Anthropic | ✅ | ❌ | 无法用 Claude |
| OpenAI | ✅ | ❌ | 无法用 GPT-4o |
| Google Gemini | ✅ | ❌ | — |
| xAI/Grok | ✅ | ❌ | — |
| 本地模型 | ✅ | ❌ | 无 ollama/localai |
| **DeepSeek** | ✅ | ✅ | 唯一的提供商 |
| SiliconFlow | ✅ | ✅ (partial) | 通过 env 配置 |
| MiMo | ❌ | ✅ (unique) | MIMO_API_KEY |

**影响程度：高** — 单一提供商 = 单点故障，无法做模型对比测试

### ❌ 7. 多平台网关 (Hermes: 15+ / EITElite: 2)
| 平台 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| **Telegram** | ✅ | ✅ | EITElite 的 TG channel |
| Discord | ✅ | ❌ | — |
| Slack | ✅ | ❌ | — |
| WhatsApp | ✅ | ❌ | — |
| Signal | ✅ | ❌ | — |
| Email | ✅ | ❌ | — |
| Matrix | ✅ | ❌ | — |
| SMS | ✅ | ❌ | — |
| Feishu | ✅ | ❌ | — |
| DingTalk | ✅ | ❌ | — |
| **tical-chat** | ❌ | ✅ (unique) | AI-to-AI 消息队列 |
| Webhook API | ✅ | ❌ | — |

**影响程度：中** — 当前 2 通道足够，但限制了跨平台扩展

### ❌ 8. Agent/技能系统 (Hermes 完整 / EITElite 无)
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| SKILL.md 体系 | ✅ | ❌ | EITElite 自有技能体系（prompt.py + 模板）|
| 技能框架 | ✅ | ✅ (自有) | 无需 SKILL.md 方式 |
| MCP 服务器 | ✅ | ❌ | — |
| 插件系统 | ✅ | ✅ | 6 插件（browser/search/xurl/vision/cloud_device/trading）|
| webhook 订阅 | ✅ | ❌ | — |

**用户意见：** EITElite/tical-code 自身已有技能/知识体系（prompt.py 构建工具提示词），不需要 SKILL.md 方式。插件系统已就位。
### ✅ 9. CLI / 用户界面 (部分实现)

| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| TUI 交互 | ✅ | ❌ | EITElite 无 TUI |
| 斜杠命令 | ✅ | ❌ | — |
| 会话管理 | ✅ | ❌ | — |
| 配置文件编辑 | ✅ | ❌ | — |
| 可视化指示器 | ✅ | ❌ | — |
| 历史回滚 | ✅ | ❌ | — |
| 语音模式 | ✅ | ❌ | — |
| 命令行运维 | ✅ | ✅ | `eitelite-cli` status/log/prompt/restart/version |
| systemd 集成 | ✅ | ✅ | 系统化服务管理 |
| journalctl 日志 | ✅ | ✅ | 标准系统日志 |

### ❌ 10. 基础设施
| 能力 | Hermes | EITElite | 备注 |
|------|--------|----------|------|
| SQLite 会话存储 | ✅ | ❌ | 无 state.db |
| 凭据池 | ✅ | ❌ | 多 API key 轮换 |
| 多 profile | ✅ | ❌ | 隔离配置 |
| 检查点快照 | ✅ | ❌ | /rollback 文件恢复 |
| 看板/Kanban | ✅ | ❌ | 多 agent 工作流 |
| Honcho 记忆 | ✅ | ❌ | 外部记忆后端 |

### ❌ 11. 工具缺失
| 工具 | Hermes | EITElite | 用途 |
|------|--------|----------|------|
| 代码执行沙箱 | ✅ | ❌ | execute_code 安全执行 Python |
| 图片生成 | ✅ | ❌ | image_gen |
| 视频分析/生成 | ✅ | ❌ | video |
| 文本转语音 | ✅ | ❌ | tts |
| Spotify 控制 | ✅ | ❌ | spotify |
| 智能家居 | ✅ | ❌ | homeassistant |
| 备忘录/TODO | ✅ | ❌ | todo (任务跟踪) |

---

## 三、总结

| 类别 | 评分 | 说明 |
|------|------|------|
| 核心循环 | 🟢 100% | LLM loop + 工具分发 |
| 工具系统 | 🟢 90% | 34 工具，基本生产工具齐全 |
| 安全 | 🟢 85% | 完整的 EITE 验证体系 |
| 记忆 | 🟢 80% | 持久化 + FTS，缺外部后端 |
| Cron | 🟢 80% | 定时任务基础功能完整 |
| 子任务 | 🟢 70% | 有 delegate_task，缺 orchestrator 嵌套 |
| LLM 提供商 | 🔴 5% | **最大短板** — 只支持 DeepSeek |
| 多平台 | 🟡 20% | 2/15 平台，缺 Discord/Email 等 |
| CLI/UI | 🔴 0% | 无交互界面，全是后台 worker |
| 技能系统 | 🔴 0% | 无 SKILL.md / 技能框架 |
| MCP/插件 | 🔴 0% | 无 MCP 集成 |

### 核心差距矩阵

```
能力                           Hermes     EITElite   差距
───────────────────────────────────────────────────────
LLM 提供商支持                ████████░░  ░░░░░░░░░░  15x
多平台网关                    ████████░░  █░░░░░░░░░  7x
技能系统                      ████████░░  ░░░░░░░░░░  完整
CLI/用户界面                  ████████░░  ██████░░░░  新增: eitelite-cli
工具数量                      ████████░░  ███████░░░  1.5x
记忆系统                      ████████░░  ██████░░░░  1.2x
安全/EITE                   ██████░░░░  ████████░░  1.5x
Cron/计划任务                 ████████░░  ██████░░░░  1.3x
子任务委派                    ████████░░  ██████░░░░  1.4x
```

### 最值得补的短板（按投入产出比）

1. **LLM 多提供商** ← 最紧急。当前只支持 DeepSeek，加 OpenRouter 一行配置就能用 200+ 模型
2. **execute_code 沙箱** ← 已完成 ✅。exec() 沙箱 + 受限 builtins + 50KB cap
3. **多平台网关** ← 对测试站价值高。加 Discord 通道能让 Taiwan 和 Oracle 通过频道协作，不需 tical-chat
4. **会话管理** ← 小改动。加 SQLite session store 就能支持 session list/resume

## 最终评定

**EITElite = Hermes 核心工具能力的 ~70%**，但缺失的并非工具，而是基础设施组件（多提供商、CLI、技能系统、多平台）。这是一套**专注于 AI 产线执行**的精简系统 — 适合跑 benchmark 和 worker 任务，不适合做开发 IDE 或通用交互式 agent。
