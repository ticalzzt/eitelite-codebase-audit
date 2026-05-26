"""EITE学习模块 - 记录规则"""
import json
import os

_RULES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".eite", "rules")
os.makedirs(_RULES_DIR, exist_ok=True)

def learn_rule(pattern: str, action: str) -> bool:
    """学习一条规则（pattern, action）"""
    filepath = os.path.join(_RULES_DIR, f"{hash(pattern)}.json")
    rule = {"pattern": pattern, "action": action}
    with open(filepath, "w") as f:
        json.dump(rule, f)
    return True

def list_rules() -> list:
    rules = []
    if not os.path.isdir(_RULES_DIR):
        return rules
    for fname in os.listdir(_RULES_DIR):
        if fname.endswith(".json"):
            with open(os.path.join(_RULES_DIR, fname)) as f:
                rules.append(json.load(f))
    return rules


# 兼容导入
def learn(*args, **kwargs):
    """Stub for backward compatibility."""
    return True
