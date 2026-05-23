# Z.1 删除 hive.py

**预计：** 5min  
**阶段：** 清理  
**产出：** 1 git commit

## 步骤

1. 确认 `hive.py` 未被任何模块引用：`grep -r "hive" tical_code/ --include="*.py" | grep -v __pycache__`
2. `git rm tical_code/core/hive.py`
3. 如果 `hive.py` 在 `__init__.py` 中有导出，一并删除

## 验证

```bash
python3 -c "import sys; sys.path.insert(0, '.'); from tical_code.core import tool_executor; print('import OK')"
```

## Commit

```
git commit -m "Z.1: remove hive.py (unused module)"
```
