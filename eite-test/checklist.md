# EITElite / tical-code 系统测试站

每次系统级修改后，在 Test VPS（YOUR_TEST_VPS_IP）上运行此套检测。

---

## T1 — 语法 & 模块完整性

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| 全部 `.py` 语法检查 | `python3 -m py_compile` | — |
| 核心模块 import | 从 `prompt`、`tool_executor`、`eite`、`modules/*` 全链 import | 删掉 `heartbeat` 后忘了删 import 会炸 |
| `unified_worker.py` 完整解析 | import + 实例化尝试（mock config） | — |
| `build_system_prompt()` 正常生成 | 调一次验证内容完整 | 注入汇报铁律后需确认的确有注入 |

## T2 — 关键内容完整性

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| Reporting Iron Law 5条全在 | `prompt.py` 输出的 system prompt 包含 5个 ### 标题 | 改 prompt 时如果只改了 eitelite 没改 tical-code 会漏 |
| EITE identity marker 注入 | `get_identity_marker()` 返回的内容包含 `Name:`、`Hash:`、`Signature:` | — |
| TruefulReporter 能抓 bare claim | scan_reply('已修复') 在无 tool 记录时返回 ≥1 条 violation | 清理重复 claim 映射时只剩一份要确认仍能工作 |

## T3 — 工具清单正确性

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| 没有断裂工具 | 检查每个 tool 的 dispatch handler 是否可 import | `conv_search` 引用了不存在的 `memory_sense`，工具永久报错但无人发现 |
| 工具数量稳定 | TOOL_SCHEMAS 数量在合理范围内（~37） | 误删或重复加会偏离 |
| 没有死工具别名 | 无对应的 `exec_*` 函数的 dispatch 条目 | — |

## T4 — EITE 验证层

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| `verify_tool_result` 对 file_write 能验证 | 伪造 file_write 结果调用 verify | — |
| `verify_tool_result` 对 bash 能验证 | mock exit_code=0 / ≠ 0 两种 | — |
| 身份绑定不崩 | `init()` → `get_verify()` → `check_identity()` | — |
| 无 scan_reply 重复调用 | unified_worker 处理回复时不调已删除的 `scan_reply` | 清理后还剩调用会崩 |

## T5 — Git 卫生

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| 没有运行时文件被追踪 | `guardian_trace.jsonl`、`*.db*`、`.trust_state.json` 不在 git ls-files 中 | Oracle Worker 首次部署时 `git add -A` 把运行时文件也提交了 |
| git status 全 clean | 无 modified/untracked 文件 | 各 VPS commit 不一致导致 dirty |
| 全 VPS 版本一致 | 所有 eitelite VPS 同一 commit hash | Oracle→Test→kael 各有不同 commit |

## T6 — 死代码回归检测

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| 无孤立 import | `from .identity import IdentityRegistry` 引用了不存在的文件 | `verify.py` 里有但没人发现因为它从未被调用 |
| 无定义但未用的模块级常量 | grep 全部大写常量 → 确认有引用 | `MAX_TOOL_ITERATIONS=8`、`EITE_IMMUTABLE_FLAG`、`DEFAULT_TASK_TIMEOUT` |
| 无整个文件未引用 | 每个 `.py` 文件至少被一个活跃文件 import/引用 | `verify.py` 528 行无人用 |
| 无空函数（只剩 pass/return） | 函数体只有 `pass` 或 `return` 的标记可疑 | `reset()` 方法 |

## T7 — 跨 VPS 同步

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| 所有 eitelite VPS 同一 commit | SSH 进每台 `git log --oneline -1` 对比 | Oracle 的 push 覆盖了 SG 的 |
| prompt.py 内容一致 | 对比 eitelite 和 tical-code 的 prompt 关键段 | taiwan 的 prompt.py 结构不同需要分别注入 |
| tool_executor.py 一致 | 确保 dot→underscore 转换逻辑统一 | unified_worker.py 和 tool_executor.py 各有一套 TOOL_SCHEMAS_CLEAN |

## T8 — 回归（Worker 实际跑）

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| Worker 初始化不崩 | `Worker(cfg)` 在 mock config 下成功 | 删了 heartbeat 后 Worker 构造函数还引用了 HeartbeatManager |
| 一次完整 message 处理 | 模拟一个简单请求走完 poll→LLM→tool→reply | — |
| 5分钟 patrol 不崩 | Vigil patrol 调一次（mock 不写文件） | — |

## T9 — 文件编辑完整性（防 Shell 转义破坏）

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| prompt.py 实际包含汇报铁律 5 条 | 直接读文件内容检查每个 section 标题 | SSH 编辑时 shell 转义了反引号和中文，patch 未生效 |
| eite/verify.py 无 scan_reply 残留 | grep `def scan_reply` | 重复 claim 映射合并后还剩一段 |
| signature.py 无死常量/imports | grep `EITE_IMMUTABLE_FLAG\|import json\|import os` | 定义但无人使用 |
| response_formatter 无 format_error/progress | grep `def format_` | 从未被调用 |
| unified_worker.py 无 heartbeat | grep `heartbeat` | 删了模块但构造函数还引用 |
| tool_executor.py 无死常量 | grep `MAX_TOOL_ITERATIONS` | 定义但实际用 `max_iterations=60` |
| channel.py 无 reply() | grep `def reply(self` | 别名从未使用 |
| clarify.py 无 format_clarify_questions | grep `def format_clarify` | 从未被调用 |
| 死文件已删除 | os.path.exists 检查 verify.py + heartbeat.py | 整个模块无人用 |
| modules/ 无 __future__ | grep `from __future__` | 5个模块都有没用 |
| cron_scheduler 无 DEFAULT_TASK_TIMEOUT | grep `DEFAULT_TASK_TIMEOUT` | 定义但从未引用 |

## T10 — 部署一致性（跨 VPS）

| 检测 | 方法 | 本次事故 |
|------|------|----------|
| Anchor 5台 VPS 信息完整 | 解析 ops-anchor.json 验证每个 VPS 有 ip/ssh_user/ssh_key | 有些 VPS 缺字段导致 SSH 配置失败 |
| eitelite VPS git 版本一致 | 从 Test VPS SSH 到 Oracle/kael 对比 commit hash | oracle push 后覆盖了 SG 的版本 |
| tical-code 独立仓库版本 | 从 SG SSH 到 Taiwan 对比 tical-code commit | 两个仓库各自独立，prompt 注入漏了 taiwan |
