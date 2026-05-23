# Z.2 跑 EITE benchmark mock

**预计：** 15min  
**阶段：** 测试  
**产出：** 测试结果截图/log

## 目标

在 eite-benchmark 测试站上跑 mock integrity 测试，验证测试站存活。

## 步骤

1. SSH 到 Taiwan：`ssh ubuntu@REPLACED_TAIWAN_IP`
2. 检查 eite-benchmark server：`curl http://localhost:9876/`
3. 检查 runner：`cd /home/ubuntu/tical-code/eite-benchmark && python3 runner.py --stage integrity`

## 验证

```bash
curl http://localhost:9876/ | grep -i "dashboard"
# 应返回 dashboard HTML
```
