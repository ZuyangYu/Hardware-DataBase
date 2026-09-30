"""Input validation and egress policy for external MCP servers.

Registration validates once; the runtime re-validates before every connection
so a DNS answer that changed after registration cannot turn a public hostname
into a private target (DNS rebinding).  The HTTP client built by ``client.py``
also disables environment proxies and redirects, so this module is one layer of
the egress boundary rather than the only one.
"""

from __future__ import annotations

import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from src.agents.mcp.models import VALID_DEPARTMENT_SCOPES, VALID_TRANSPORTS


SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,49}$")
MAX_HEADERS = 32
MAX_HEADER_KEY_CHARS = 128
MAX_HEADER_VALUE_CHARS = 4096
MAX_HEADERS_TOTAL_CHARS = 8192


class McpValidationError(ValueError):
    """Rejected MCP configuration input (transport/url/headers/timeouts)."""


def normalize_slug(raw: Any) -> str:
    """Validate and return the stable server slug."""

    slug = str(raw or "").strip().lower()
    if not SLUG_PATTERN.match(slug):
        raise McpValidationError(
            "slug 必须匹配 ^[a-z0-9][a-z0-9_-]{1,49}$（小写字母、数字、下划线、连字符）"
        )
    return slug


def validate_transport(transport: Any) -> str:
    """Remote transports only: stdio would run local commands on the server."""

    value = str(transport or "").strip().lower()
    if value == "stdio":
        raise McpValidationError(
            "不允许 stdio：管理接口不得在服务器上启动本地进程；请使用 streamable_http 或 sse"
        )
    if value not in VALID_TRANSPORTS:
        raise McpValidationError(
            f"transport 必须是 {', '.join(VALID_TRANSPORTS)} 之一，当前值: {value!r}"
        )
    return value


def validate_name(name: Any) -> str:
    value = str(name or "").strip()
    if not value:
        raise McpValidationError("名称不能为空")
    if len(value) > 200:
        raise McpValidationError("名称长度不能超过 200 字符")
    return value


def validate_headers(headers: Any) -> dict[str, str]:
    """Normalize a header map and bound its size (values are secrets)."""

    if headers in (None, ""):
        return {}
    if not isinstance(headers, dict):
        raise McpValidationError("headers 必须是键值对象")
    if len(headers) > MAX_HEADERS:
        raise McpValidationError(f"headers 数量不能超过 {MAX_HEADERS} 个")
    normalized: dict[str, str] = {}
    total = 0
    for raw_key, raw_value in headers.items():
        key = str(raw_key or "").strip()
        value = str(raw_value if raw_value is not None else "")
        if not key:
            raise McpValidationError("header 名称不能为空")
        if len(key) > MAX_HEADER_KEY_CHARS:
            raise McpValidationError(f"header 名称过长: {key[:32]}...")
        if len(value) > MAX_HEADER_VALUE_CHARS:
            raise McpValidationError(f"header 值过长: {key}")
        total += len(key) + len(value)
        normalized[key] = value
    if total > MAX_HEADERS_TOTAL_CHARS:
        raise McpValidationError(f"headers 总体积不能超过 {MAX_HEADERS_TOTAL_CHARS} 字符")
    return normalized


def validate_timeout(value: Any, *, field: str, maximum: int = 3600) -> int | None:
    """Validate an optional per-server timeout in seconds."""

    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise McpValidationError(f"{field} 必须是整数秒") from exc
    if number < 1 or number > maximum:
        raise McpValidationError(f"{field} 必须在 1-{maximum} 秒之间")
    return number


def validate_department_scope(scope: Any, department_ids: Any) -> tuple[str, list[int]]:
    """Validate the department visibility triple (none/departments/all)."""

    value = str(scope or "none").strip().lower()
    if value not in VALID_DEPARTMENT_SCOPES:
        raise McpValidationError(
            f"department_scope 必须是 {', '.join(VALID_DEPARTMENT_SCOPES)} 之一"
        )
    ids: list[int] = []
    if department_ids not in (None, ""):
        if not isinstance(department_ids, (list, tuple, set)):
            raise McpValidationError("department_ids 必须是整数数组")
        for item in department_ids:
            try:
                number = int(item)
            except (TypeError, ValueError) as exc:
                raise McpValidationError("department_ids 必须是整数数组") from exc
            if number not in ids:
                ids.append(number)
    if value == "departments" and not ids:
        raise McpValidationError("department_scope=departments 时 department_ids 不能为空")
    if value != "departments":
        ids = []
    return value, ids


def _resolve_host(host: str, port: int | None) -> list[str]:
    """Resolve every A/AAAA answer for the hostname."""

    infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    addresses: list[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in addresses:
            addresses.append(address)
    return addresses


def _blocked_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def validate_url(
    url: Any,
    *,
    allow_insecure_http: bool = False,
    allow_private_hosts: bool = False,
) -> str:
    """Validate one MCP endpoint URL against the egress policy.

    Raises :class:`McpValidationError` for non-http(s) schemes, embedded
    credentials, missing hosts, unresolvable names and non-global addresses.
    """

    value = str(url or "").strip()
    if not value:
        raise McpValidationError("url 不能为空")
    try:
        parts = urlsplit(value)
    except ValueError as exc:
        raise McpValidationError("url 解析失败") from exc

    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"}:
        raise McpValidationError(f"url scheme 仅支持 https（或显式开启的 http），当前: {scheme!r}")
    if scheme == "http" and not allow_insecure_http:
        raise McpValidationError("不允许 http://；如确需内网明文，请显式开启 MCP_ALLOW_INSECURE_HTTP")
    if parts.username or parts.password:
        raise McpValidationError("url 不允许携带用户名或密码，请改用 headers 传递凭据")
    host = parts.hostname
    if not host:
        raise McpValidationError("url 缺少主机名")
    try:
        port = parts.port
    except ValueError as exc:
        raise McpValidationError("url 端口非法") from exc

    if allow_private_hosts:
        return value

    literal: str | None = None
    try:
        ipaddress.ip_address(host)
        literal = host
    except ValueError:
        literal = None
    try:
        addresses = [literal] if literal else _resolve_host(host, port)
    except OSError as exc:
        raise McpValidationError(f"无法解析主机地址: {host}") from exc
    if not addresses:
        raise McpValidationError("无法解析主机地址")
    if any(_blocked_address(address) for address in addresses):
        raise McpValidationError(
            "目标地址属于私网/环回/链路本地或保留网段；如确需内网服务，"
            "请显式开启 MCP_ALLOW_PRIVATE_HOSTS 并配合网络层出口限制"
        )
    return value


def redact_url(url: Any) -> str:
    """Strip userinfo, query and fragment before a URL reaches logs/errors."""

    value = str(url or "").strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "<invalid-url>"
    host = parts.hostname
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


__all__ = [
    "MAX_HEADERS",
    "McpValidationError",
    "SLUG_PATTERN",
    "normalize_slug",
    "redact_url",
    "validate_department_scope",
    "validate_headers",
    "validate_name",
    "validate_timeout",
    "validate_transport",
    "validate_url",
]
