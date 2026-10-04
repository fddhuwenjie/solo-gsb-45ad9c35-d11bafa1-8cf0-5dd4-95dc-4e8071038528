"""路由模板解析。

模板段类型：

- 静态段：字面文本，注册时按与请求相同的解码/规范化规则处理；
- 参数段：``{name}`` 或 ``{name:type}``，type 取 ``int`` / ``uuid`` / ``slug``；
- 末尾通配：最后一段恰好是 ``**``，匹配零个或多个后续段。

处理顺序与请求路径一致：先做安全百分号解码，再按 RFC 3986 移除点段、
去掉尾随斜杠，然后逐段解析。``**`` 必须位于原始模板末尾，因此在规范化
之前识别。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import constraints
from .normalization import (
    PathError,
    normalize_segments,
    render,
    split_path,
)

_PARAM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TemplateError(ValueError):
    """模板语法或规范化错误。"""


@dataclass(frozen=True)
class Static:
    text: str  # 已规范化的字面文本


@dataclass(frozen=True)
class Param:
    name: str
    constraint: constraints.Constraint


@dataclass(frozen=True)
class Wildcard:
    pass


WILDCARD = Wildcard()

Segment = Static | Param | Wildcard


@dataclass
class ParsedTemplate:
    segments: list[Segment] = field(default_factory=list)
    wildcard: bool = False
    normalized: str = ""
    warnings: list[str] = field(default_factory=list)


def _parse_braced(raw: str) -> Param:
    inner = raw[1:-1]
    if ":" in inner:
        name, type_name = inner.split(":", 1)
        type_name = type_name.strip()
        if not type_name:
            raise TemplateError(f"empty parameter type in {raw!r}")
        if not constraints.is_known(type_name):
            raise TemplateError(
                f"unsupported parameter type {type_name!r} (allowed: int, uuid, slug)"
            )
        constraint = constraints.get(type_name)
    else:
        name = inner
        constraint = constraints.ANY
    name = name.strip()
    if not _PARAM_RE.fullmatch(name):
        raise TemplateError(f"invalid parameter name in {raw!r}")
    return Param(name=name, constraint=constraint)


def parse_template(template: str) -> ParsedTemplate:
    """解析并规范化一个路径模板。"""
    if not isinstance(template, str) or not template.startswith("/"):
        raise TemplateError("path template must start with '/'")
    if any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in template):
        raise TemplateError("path template contains ASCII control character")

    raw = split_path(template)
    trailing_slash = len(raw) > 1 and raw[-1] == ""
    body = raw[1:-1] if trailing_slash else raw[1:]

    warnings: list[str] = []

    # ** 必须是原始（解码前）模板的最后一段。
    wildcard = False
    if body and body[-1] == "**":
        wildcard = True
        body = body[:-1]
    elif any(seg == "**" for seg in body):
        raise TemplateError("'**' is only allowed as the last segment")

    if any(seg == "" for seg in body):
        raise TemplateError("empty path segment (interior '//') is not allowed")

    try:
        normalized_parts = normalize_segments(body)
    except PathError as exc:
        raise TemplateError(str(exc)) from exc

    if any(seg == "" for seg in normalized_parts):
        raise TemplateError("empty path segment (interior '//') is not allowed")

    segments: list[Segment] = []
    seen_names: set[str] = set()
    for part in normalized_parts:
        if "{" in part or "}" in part:
            if not (part.startswith("{") and part.endswith("}")) or len(part) < 3:
                raise TemplateError(
                    f"malformed parameter segment {part!r}: braces must wrap the "
                    "whole segment, e.g. {id:int}"
                )
            param = _parse_braced(part)
            if param.name in seen_names:
                raise TemplateError(f"duplicate parameter name {param.name!r}")
            seen_names.add(param.name)
            segments.append(param)
        elif part == "*":
            raise TemplateError("single '*' is not supported; use trailing '**'")
        else:
            segments.append(Static(part))

    if wildcard:
        segments.append(WILDCARD)

    normalized = render([_segment_text(seg) for seg in segments])

    if normalized != template:
        if trailing_slash:
            warnings.append("trailing slash removed by normalization")
        if any(part in (".", "..") for part in body):
            warnings.append("dot segments (. / ..) removed by normalization")
        if "%" in template:
            warnings.append("percent-escapes normalized (reserved chars stay encoded)")

    return ParsedTemplate(
        segments=segments,
        wildcard=wildcard,
        normalized=normalized,
        warnings=warnings,
    )


def _segment_text(seg: Segment) -> str:
    if isinstance(seg, Static):
        return seg.text
    if isinstance(seg, Param):
        if seg.constraint is constraints.ANY:
            return "{" + seg.name + "}"
        return "{" + seg.name + ":" + seg.constraint.name + "}"
    return "**"
