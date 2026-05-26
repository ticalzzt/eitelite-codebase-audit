"""EITE检查模块 - 检测请求是否违法"""
import re
from .learn import list_rules

def check(request: str) -> dict:
    """检查请求是否符合规则。返回 {'action': 'allow'|'block'|'warn', 'reason': str}"""
    for rule in list_rules():
        pattern = rule.get("pattern", "")
        action = rule.get("action", "allow")
        if re.search(pattern, request):
            return {"action": action, "reason": f"Matched rule: {pattern}"}
    return {"action": "allow", "reason": "No matching rule"}
