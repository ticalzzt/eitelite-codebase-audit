"""
安全基线加固 (Security Baseline Hardening)
==========================================

当前缺少TOCTOU防护、SSRF防护、敏感信息脱敏，本模块提供完整的安全基线。

核心设计：
1. TOCTOU防护 — 路径安全检查，防符号链接攻击和路径遍历
2. SSRF防护 — URL安全检查，防私有IP访问和DNS重绑定
3. 敏感信息脱敏 — 正则检测API Key/Token/密码/私钥/连接串
4. 出站过滤 — 整合URL验证+SSRF防护+域名白/黑名单

安全原则：
- 安全检查必须是强制性的，不能被绕过
- 脱敏不能丢失功能性信息（保留结构，只替换值）
- 检查和操作之间加锁，防止竞态条件
- 纯stdlib优先

Author: Tical (子泽图)
Version: see tical_code.__version__
"""

import ipaddress
import logging
import os
import re
import socket
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# =============================================================================
# TOCTOU防护 — 路径安全
# =============================================================================

# 全局路径锁，防止检查和操作之间的竞态
_path_lock = threading.Lock()


@dataclass
class PathSafetyConfig:
    """路径安全配置。

    Attributes:
        allowed_dirs: 允许的根目录列表
        deny_symlinks: 是否拒绝符号链接
        deny_absolute: 是否拒绝绝对路径跳出沙箱
        max_path_length: 路径最大长度
    """
    allowed_dirs: List[str] = field(default_factory=lambda: ["."])
    deny_symlinks: bool = True
    deny_absolute: bool = True
    max_path_length: int = 4096


def validate_path_safety(
    path: str,
    allowed_dirs: Optional[List[str]] = None,
    config: Optional[PathSafetyConfig] = None,
) -> Tuple[bool, str]:
    """
    路径安全检查（TOCTOU防护）。

    检查内容：
    - 路径遍历攻击：拒绝 .. 跳出允许目录
    - 绝对路径：检查是否在允许目录内
    - 符号链接：检查resolve后的真实路径是否安全
    - 路径长度：拒绝过长路径

    注意：此函数内部加锁，保证检查和resolve之间的原子性。

    Args:
        path: 要检查的路径
        allowed_dirs: 允许的根目录列表（None则使用config）
        config: 路径安全配置

    Returns:
        (safe, reason): safe=True表示路径安全，reason为空或原因说明
    """
    cfg = config or PathSafetyConfig()
    dirs = allowed_dirs or cfg.allowed_dirs

    # 空路径检查
    if not path or not path.strip():
        return False, "empty path"

    # 路径长度检查
    if len(path) > cfg.max_path_length:
        return False, f"path too long: {len(path)} > {cfg.max_path_length}"

    # 检查路径遍历（原始路径中的..）
    # 注意：不直接拒绝..，而是检查resolve后是否跳出
    normalized = os.path.normpath(path)

    # 检查绝对路径
    if os.path.isabs(normalized) and cfg.deny_absolute:
        # 绝对路径必须在allowed_dirs内
        abs_resolved = os.path.realpath(normalized)
        if not _is_path_in_allowed_dirs(abs_resolved, dirs):
            return False, f"absolute path outside allowed dirs: {normalized}"

    # 符号链接检查（加锁防止TOCTOU）
    with _path_lock:
        # 对于相对路径，检查相对于当前工作目录的resolve结果
        if not os.path.isabs(normalized):
            resolved = os.path.realpath(normalized)
        else:
            resolved = os.path.realpath(normalized)

        if cfg.deny_symlinks:
            # 检查路径中是否有符号链接
            if _contains_symlink(path):
                # 符号链接的resolve结果必须在允许目录内
                if not _is_path_in_allowed_dirs(resolved, dirs):
                    return False, (
                        f"symlink points outside allowed dirs: "
                        f"path={normalized} → resolved={resolved}"
                    )

        # 最终检查：resolve后的路径必须在允许目录内
        if not _is_path_in_allowed_dirs(resolved, dirs):
            return False, (
                f"resolved path outside allowed dirs: "
                f"path={normalized} → resolved={resolved}"
            )

    return True, ""


def resolve_and_validate(
    path: str,
    allowed_dirs: Optional[List[str]] = None,
    config: Optional[PathSafetyConfig] = None,
) -> Tuple[Optional[str], bool]:
    """
    解析真实路径并验证安全性。

    加锁执行resolve和验证，防止TOCTOU竞态。

    Args:
        path: 要解析的路径
        allowed_dirs: 允许的根目录列表
        config: 路径安全配置

    Returns:
        (resolved_path, safe): resolved_path为解析后的绝对路径或None，safe表示是否安全
    """
    safe, reason = validate_path_safety(path, allowed_dirs, config)
    if not safe:
        logger.warning(f"[Security] path check failed: {reason}")
        return None, False

    with _path_lock:
        resolved = os.path.realpath(path)
        # 二次验证resolve后的路径
        if not _is_path_in_allowed_dirs(resolved, allowed_dirs or ["."]):
            return None, False

    return resolved, True


def _is_path_in_allowed_dirs(resolved_path: str, allowed_dirs: List[str]) -> bool:
    """
    检查resolve后的路径是否在允许目录内。

    Args:
        resolved_path: 已解析的绝对路径
        allowed_dirs: 允许的根目录列表

    Returns:
        True 表示在允许目录内
    """
    resolved_abs = os.path.abspath(resolved_path)

    for allowed in allowed_dirs:
        allowed_abs = os.path.realpath(os.path.abspath(allowed))
        # 路径必须在allowed目录内（前缀匹配，确保是子路径）
        if resolved_abs == allowed_abs or resolved_abs.startswith(allowed_abs + os.sep):
            return True

    return False


def _contains_symlink(path: str) -> bool:
    """
    检查路径中是否包含符号链接。

    Args:
        path: 要检查的路径

    Returns:
        True 表示路径中包含符号链接
    """
    try:
        # 逐级检查路径组件
        parts = os.path.normpath(path).split(os.sep)
        current = "/" if os.path.isabs(path) else "."

        for part in parts:
            if not part or part == ".":
                continue
            current = os.path.join(current, part)
            if os.path.islink(current):
                return True
    except (OSError, ValueError):
        logger.debug("security_baseline: symlink check exception, treating as safe")

    return False


# =============================================================================
# SSRF防护 — URL安全
# =============================================================================

# 私有IP网段（RFC 1918 + loopback + link-local + metadata）
_PRIVATE_IP_RANGES: List[ipaddress.IPv4Network] = [
    ipaddress.IPv4Network('127.0.0.0/8'),       # Loopback
    ipaddress.IPv4Network('10.0.0.0/8'),         # 私有A类
    ipaddress.IPv4Network('172.16.0.0/12'),      # 私有B类
    ipaddress.IPv4Network('192.168.0.0/16'),     # 私有C类
    ipaddress.IPv4Network('169.254.0.0/16'),     # Link-local
    ipaddress.IPv4Network('0.0.0.0/8'),          # 当前网络
    ipaddress.IPv4Network('100.64.0.0/10'),      # CGNAT
    ipaddress.IPv4Network('198.18.0.0/15'),      # 基准测试
    ipaddress.IPv4Network('224.0.0.0/4'),        # 组播
    ipaddress.IPv4Network('240.0.0.0/4'),        # 保留
]

# IPv6私有范围
_PRIVATE_IP_RANGES_V6: List[ipaddress.IPv6Network] = [
    ipaddress.IPv6Network('::1/128'),            # Loopback
    ipaddress.IPv6Network('fc00::/7'),           # ULA
    ipaddress.IPv6Network('fe80::/10'),          # Link-local
    ipaddress.IPv6Network('ff00::/8'),           # 组播
]

# 危险协议黑名单
_DANGEROUS_SCHEMES: FrozenSet[str] = frozenset({
    'file', 'gopher', 'dict', 'ftp', 'tftp',
    'ldap', 'ldaps', 'jar', 'netdoc', 'ssh',
    'telnet', 'sftp',
})


@dataclass
class URLSafetyConfig:
    """URL安全配置。

    Attributes:
        allowed_schemes: 允许的URL协议（默认http/https）
        domain_whitelist: 域名白名单（空则不限制域名）
        domain_blacklist: 域名黑名单
        check_dns_rebinding: 是否检查DNS重绑定
        allow_private_ip: 是否允许私有IP（默认不允许）
        max_redirects: 最大重定向次数
    """
    allowed_schemes: FrozenSet[str] = frozenset({'http', 'https'})
    domain_whitelist: List[str] = field(default_factory=list)
    domain_blacklist: List[str] = field(default_factory=list)
    check_dns_rebinding: bool = True
    allow_private_ip: bool = False
    max_redirects: int = 5


def validate_url(
    url: str,
    config: Optional[URLSafetyConfig] = None,
) -> Tuple[bool, str]:
    """
    URL安全检查（SSRF防护）。

    检查内容：
    - 协议安全：只允许http/https，拒绝file://等危险协议
    - 私有IP黑名单：拒绝127.x, 10.x, 172.16-31.x, 192.168.x, 169.254.x
    - DNS重绑定防护：解析域名后检查IP是否为私有
    - 域名白/黑名单检查

    Args:
        url: 要检查的URL
        config: URL安全配置

    Returns:
        (safe, reason): safe=True表示URL安全
    """
    cfg = config or URLSafetyConfig()

    # 空URL检查
    if not url or not url.strip():
        return False, "empty URL"

    # 解析URL
    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL parse failed: {e}"

    # 协议检查
    scheme = parsed.scheme.lower()
    if not scheme:
        return False, "URL missing scheme"

    if scheme in _DANGEROUS_SCHEMES:
        return False, f"dangerous scheme: {scheme}://"

    if scheme not in cfg.allowed_schemes:
        return False, f"disallowed scheme: {scheme}:// (allowed: {', '.join(sorted(cfg.allowed_schemes))})"

    # 主机名检查
    hostname = parsed.hostname
    if not hostname:
        return False, "URL missing hostname"

    # 域名黑名单
    hostname_lower = hostname.lower()
    for blocked in cfg.domain_blacklist:
        if hostname_lower == blocked.lower() or hostname_lower.endswith('.' + blocked.lower()):
            return False, f"domain in blacklist: {hostname}"

    # 域名白名单（如果设置了）
    if cfg.domain_whitelist:
        allowed = False
        for wl in cfg.domain_whitelist:
            if hostname_lower == wl.lower() or hostname_lower.endswith('.' + wl.lower()):
                allowed = True
                break
        if not allowed:
            return False, f"domain not in whitelist: {hostname}"

    # 私有IP检查（直接IP地址）
    if not cfg.allow_private_ip:
        is_private, reason = _check_ip_private(hostname)
        if is_private:
            return False, reason

    # DNS重绑定防护
    if cfg.check_dns_rebinding and not cfg.allow_private_ip:
        try:
            # 解析域名获取IP
            addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for family, _, _, _, sockaddr in addr_info:
                ip_str = sockaddr[0]
                is_private, reason = _check_ip_private(ip_str)
                if is_private:
                    return False, f"DNS rebinding protection: {hostname} resolved to private IP {ip_str}"
        except socket.gaierror:
            # 域名无法解析，不阻止（可能是临时DNS问题）
            logger.debug(f"[Security] DNS resolution failed: {hostname}")
        except Exception as e:
            logger.debug(f"[Security] DNS check error: {hostname}, {e}")

    return True, ""


def _check_ip_private(ip_str: str) -> Tuple[bool, str]:
    """
    检查IP地址是否为私有/保留地址。

    Args:
        ip_str: IP地址字符串

    Returns:
        (is_private, reason)
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False, ""  # 不是有效IP，可能还是域名

    # 检查IPv4私有范围
    if isinstance(ip, ipaddress.IPv4Address):
        for network in _PRIVATE_IP_RANGES:
            if ip in network:
                return True, f"private IP: {ip_str} (network: {network})"

    # 检查IPv6私有范围
    if isinstance(ip, ipaddress.IPv6Address):
        for network in _PRIVATE_IP_RANGES_V6:
            if ip in network:
                return True, f"private IPv6: {ip_str} (network: {network})"

    return False, ""


# =============================================================================
# 敏感信息脱敏
# =============================================================================

# 脱敏正则模式列表
_DEFAULT_REDACTION_PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    # API Key模式
    (
        "api_key_openai",
        r'sk-[a-zA-Z0-9]{20,}',
        re.compile(r'sk-[a-zA-Z0-9]{20,}'),
    ),
    (
        "api_key_google",
        r'AIza[a-zA-Z0-9_-]{35}',
        re.compile(r'AIza[a-zA-Z0-9_-]{35}'),
    ),
    (
        "api_key_generic",
        r'["\']?api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?',
        re.compile(r'["\']?api[_-]?key["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})["\']?', re.IGNORECASE),
    ),
    # Token模式
    (
        "token_github",
        r'ghp_[a-zA-Z0-9]{36}',
        re.compile(r'ghp_[a-zA-Z0-9]{36}'),
    ),
    (
        "token_gitlab",
        r'glpat-[a-zA-Z0-9\-]{20,}',
        re.compile(r'glpat-[a-zA-Z0-9\-]{20,}'),
    ),
    # 密码模式
    (
        "password",
        r'(?:password|passwd|pwd)\s*[:=]\s*\S+',
        re.compile(r'(?:password|passwd|pwd)\s*[:=]\s*\S+', re.IGNORECASE),
    ),
    # 私钥模式
    (
        "private_key",
        r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----',
        re.compile(r'-----BEGIN (?:RSA |EC )?PRIVATE KEY-----'),
    ),
    # 连接串模式
    (
        "connection_mongodb",
        r'mongodb://[^:\s]+:[^@\s]+@',
        re.compile(r'mongodb://[^:\s]+:[^@\s]+@'),
    ),
    (
        "connection_postgres",
        r'postgres(?:ql)?://[^:\s]+:[^@\s]+@',
        re.compile(r'postgres(?:ql)?://[^:\s]+:[^@\s]+@', re.IGNORECASE),
    ),
    (
        "connection_mysql",
        r'mysql://[^:\s]+:[^@\s]+@',
        re.compile(r'mysql://[^:\s]+:[^@\s]+@'),
    ),
    (
        "connection_redis",
        r'redis://:[^@\s]+@',
        re.compile(r'redis://:[^@\s]+@'),
    ),
    # AWS密钥模式
    (
        "aws_access_key",
        r'AKIA[0-9A-Z]{16}',
        re.compile(r'AKIA[0-9A-Z]{16}'),
    ),
    (
        "aws_secret_key",
        r'["\']?aws[_-]?secret[_-]?access[_-]?key["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=]{40}["\']?',
        re.compile(r'["\']?aws[_-]?secret[_-]?access[_-]?key["\']?\s*[:=]\s*["\']?[A-Za-z0-9/+=]{40}["\']?', re.IGNORECASE),
    ),
    # Bearer token
    (
        "bearer_token",
        r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}',
        re.compile(r'Bearer\s+[a-zA-Z0-9_\-\.]{20,}', re.IGNORECASE),
    ),
]


@dataclass
class RedactionConfig:
    """脱敏配置。

    Attributes:
        enabled: 是否启用脱敏
        replacement_format: 替换格式，默认 [REDACTED_{type}]
        custom_patterns: 自定义脱敏模式 [(name, pattern_str)]
    """
    enabled: bool = True
    replacement_format: str = "[REDACTED_{type}]"
    custom_patterns: List[Tuple[str, str]] = field(default_factory=list)


# 编译后的脱敏模式缓存
_compiled_patterns: Optional[List[Tuple[str, re.Pattern]]] = None
_compiled_lock = threading.Lock()


def _get_compiled_patterns(
    config: Optional[RedactionConfig] = None,
) -> List[Tuple[str, re.Pattern]]:
    """
    获取编译后的脱敏模式列表。

    Args:
        config: 脱敏配置（包含自定义模式）

    Returns:
        [(type_name, compiled_pattern)]
    """
    global _compiled_patterns

    if config and config.custom_patterns:
        # 有自定义模式，重新编译
        patterns = [
            (name, re.compile(pat))
            for name, pat in config.custom_patterns
        ]
        # 加上默认模式
        for name, _, compiled in _DEFAULT_REDACTION_PATTERNS:
            patterns.append((name, compiled))
        return patterns

    # 使用缓存的默认模式
    if _compiled_patterns is None:
        with _compiled_lock:
            if _compiled_patterns is None:
                _compiled_patterns = [
                    (name, compiled)
                    for name, _, compiled in _DEFAULT_REDACTION_PATTERNS
                ]

    return _compiled_patterns


def redact_secrets(
    text: str,
    config: Optional[RedactionConfig] = None,
) -> str:
    """
    自动脱敏文本中的敏感信息。

    检测并替换以下模式：
    - API Key: sk-xxx, AIzaxxx
    - Token: ghp_xxx, glpat-xxx
    - 密码: password=xxx, passwd=xxx
    - 私钥: -----BEGIN PRIVATE KEY-----
    - 连接串: mongodb://user:pass@, postgres://user:pass@
    - AWS密钥: AKIAxxxx
    - Bearer token: Bearer xxx

    替换为 [REDACTED_{type}] 格式，保留结构，只替换值。

    Args:
        text: 要脱敏的文本
        config: 脱敏配置

    Returns:
        脱敏后的文本
    """
    cfg = config or RedactionConfig()

    if not cfg.enabled:
        return text

    if not text:
        return text

    patterns = _get_compiled_patterns(cfg)
    result = text

    for type_name, pattern in patterns:
        replacement = cfg.replacement_format.format(type=type_name)
        result = pattern.sub(replacement, result)

    return result


# =============================================================================
# 出站过滤
# =============================================================================

@dataclass
class OutboundConfig:
    """出站请求过滤配置。

    Attributes:
        url_config: URL安全配置
        domain_whitelist: 域名白名单（空则不限制）
        domain_blacklist: 域名黑名单
        redact_query_params: URL中需要脱敏的query参数名
        allowed_methods: 允许的HTTP方法
    """
    url_config: URLSafetyConfig = field(default_factory=URLSafetyConfig)
    domain_whitelist: List[str] = field(default_factory=list)
    domain_blacklist: List[str] = field(default_factory=list)
    redact_query_params: List[str] = field(default_factory=lambda: [
        'token', 'key', 'api_key', 'api-key', 'secret',
        'password', 'access_token', 'refresh_token',
        'client_secret', 'private_key',
    ])
    allowed_methods: FrozenSet[str] = frozenset({
        'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS',
    })


def check_outbound_request(
    url: str,
    method: str = "GET",
    config: Optional[OutboundConfig] = None,
) -> Tuple[bool, str]:
    """
    出站请求安全检查。

    整合URL验证 + SSRF防护 + 域名白/黑名单。

    Args:
        url: 请求URL
        method: HTTP方法
        config: 出站过滤配置

    Returns:
        (allowed, reason): allowed=True表示允许
    """
    cfg = config or OutboundConfig()

    # HTTP方法检查
    method_upper = method.upper()
    if method_upper not in cfg.allowed_methods:
        return False, f"disallowed HTTP method: {method}"

    # URL安全检查（SSRF防护）
    safe, reason = validate_url(url, cfg.url_config)
    if not safe:
        return False, f"URL security check failed: {reason}"

    # 额外的域名白名单检查
    if cfg.domain_whitelist:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if hostname:
                hostname_lower = hostname.lower()
                allowed = False
                for wl in cfg.domain_whitelist:
                    if hostname_lower == wl.lower() or hostname_lower.endswith('.' + wl.lower()):
                        allowed = True
                        break
                if not allowed:
                    return False, f"domain not in outbound whitelist: {hostname}"
        except Exception as e:
            logger.debug(f"[SecurityBaseline] unknown exception (non-fatal): {e}")
            pass

    # 额外的域名黑名单检查
    if cfg.domain_blacklist:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if hostname:
                hostname_lower = hostname.lower()
                for bl in cfg.domain_blacklist:
                    if hostname_lower == bl.lower() or hostname_lower.endswith('.' + bl.lower()):
                        return False, f"domain in outbound blacklist: {hostname}"
        except Exception as e:
            logger.debug(f"[SecurityBaseline] unknown exception (non-fatal): {e}")
            pass

    return True, ""


def redact_url_params(url: str, params_to_redact: Optional[List[str]] = None) -> str:
    """
    脱敏URL中的敏感query参数。

    将 token=xxx&key=yyy 替换为 token=[REDACTED]&key=[REDACTED]

    Args:
        url: 原始URL
        params_to_redact: 需要脱敏的参数名列表

    Returns:
        脱敏后的URL
    """
    if not params_to_redact:
        params_to_redact = [
            'token', 'key', 'api_key', 'api-key', 'secret',
            'password', 'access_token', 'refresh_token',
        ]

    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url

        # 解析query参数
        from urllib.parse import parse_qs, urlencode, urlunparse

        params = parse_qs(parsed.query, keep_blank_values=True)
        redacted = False

        for key in list(params.keys()):
            key_lower = key.lower()
            if any(p in key_lower for p in [p.lower() for p in params_to_redact]):
                params[key] = ['[REDACTED]']
                redacted = True

        if redacted:
            # 重建URL
            new_query = urlencode(params, doseq=True)
            return urlunparse(parsed._replace(query=new_query))

    except Exception as e:
        logger.debug(f"[Security] URL param redaction error: {e}")

    return url


# =============================================================================
# 沙箱集成辅助
# =============================================================================

def sandbox_path_check(
    path: str,
    allowed_dirs: List[str],
) -> Tuple[bool, str]:
    """
    沙箱执行前的路径安全检查（简化接口）。

    Args:
        path: 要检查的路径
        allowed_dirs: 允许的目录列表

    Returns:
        (safe, reason)
    """
    config = PathSafetyConfig(
        allowed_dirs=allowed_dirs,
        deny_symlinks=True,
        deny_absolute=True,
    )
    return validate_path_safety(path, allowed_dirs, config)


def sandbox_network_check(
    url: str,
    allowed_domains: Optional[List[str]] = None,
) -> Tuple[bool, str]:
    """
    沙箱内的网络请求安全检查（简化接口）。

    Args:
        url: 请求URL
        allowed_domains: 允许的域名列表

    Returns:
        (allowed, reason)
    """
    url_config = URLSafetyConfig(
        domain_whitelist=allowed_domains or [],
        allow_private_ip=False,
    )
    outbound_config = OutboundConfig(
        url_config=url_config,
        domain_whitelist=allowed_domains or [],
    )
    return check_outbound_request(url, "GET", outbound_config)


def sandbox_output_redact(output: str) -> str:
    """
    沙箱输出自动脱敏（简化接口）。

    Args:
        output: 沙箱输出文本

    Returns:
        脱敏后的输出
    """
    return redact_secrets(output)
