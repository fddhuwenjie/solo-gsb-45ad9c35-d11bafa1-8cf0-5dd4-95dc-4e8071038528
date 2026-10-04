"""把路由配置导出为可嵌入 OpenAPI 3.1 的 ``paths`` 片段。

转换规则：

- 静态段按与审计相同的规范化结果原样保留；
- ``{name}`` / ``{name:type}`` 参数段映射为同名 path 参数，约束映射为
  JSON Schema（OpenAPI 3.1 使用 JSON Schema 2020-12）；
- 末尾 ``**`` 映射为名为 ``wildcard`` 的 path 参数，并附
  ``x-route-wildcard: true``；
- 每个操作获得稳定且唯一的 ``operationId``：优先使用显式 ``id``，缺省时
  由方法 + 规范化路径生成，重名时追加重复序号（``-2``、``-3`` …）；
- 同方法且导出路径相同的重复项记入 ``parse_errors``（保留先注册者），
  绝不静默覆盖；
- 片段按规范化路径、方法排序，序号分配与输入顺序无关。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ALLOWED_METHODS, ExportOpenApiResponse, ParseError, RouteIn
from .templates import Param, Segment, Static, TemplateError, parse_template

WILDCARD_PARAM_NAME = "wildcard"
WILDCARD_EXTENSION = "x-route-wildcard"

# 参数约束 -> JSON Schema。经 register_constraint 注册的自定义约束
# 回退为通用 string。
_CONSTRAINT_SCHEMAS: dict[str, dict] = {
    "any": {"type": "string", "minLength": 1},
    "int": {"type": "integer", "minimum": 0},
    "uuid": {"type": "string", "format": "uuid"},
    "slug": {"type": "string", "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$"},
}
_FALLBACK_SCHEMA: dict = {"type": "string"}

_REPEAT_UNDERSCORE = re.compile(r"_+")


@dataclass
class _Operation:
    index: int  # 在输入数组中的原始下标
    method: str
    path: str  # OpenAPI 路径模板
    parameters: list[dict]
    explicit_id: str | None
    operation_id: str = ""


def _schema_for(constraint_name: str) -> dict:
    return dict(_CONSTRAINT_SCHEMAS.get(constraint_name, _FALLBACK_SCHEMA))


def _convert_segments(segments: list[Segment]) -> tuple[str, list[dict]]:
    """把规范化后的模板段转换为 OpenAPI 路径模板与 path 参数列表。"""
    parts: list[str] = []
    parameters: list[dict] = []
    for seg in segments:
        if isinstance(seg, Static):
            parts.append(seg.text)
        elif isinstance(seg, Param):
            parts.append("{" + seg.name + "}")
            parameters.append(
                {
                    "name": seg.name,
                    "in": "path",
                    "required": True,
                    "schema": _schema_for(seg.constraint.name),
                }
            )
        else:  # 末尾 **
            parts.append("{" + WILDCARD_PARAM_NAME + "}")
            parameters.append(
                {
                    "name": WILDCARD_PARAM_NAME,
                    "in": "path",
                    "required": True,
                    "schema": dict(_FALLBACK_SCHEMA),
                    WILDCARD_EXTENSION: True,
                }
            )
    path = "/" + "/".join(parts) if parts else "/"
    return path, parameters


def _slug_token(text: str) -> str:
    """把静态段文本折叠为 operationId 可用的 ASCII 标识片段。"""
    cleaned = "".join(
        ch if ch.isascii() and ch.isalnum() else "_" for ch in text.lower()
    )
    return _REPEAT_UNDERSCORE.sub("_", cleaned).strip("_")


def _generated_base_id(method: str, path: str) -> str:
    """缺省 operationId 的基础名：方法 + 规范化路径的逐段标识。"""
    tokens: list[str] = []
    for part in path.split("/"):
        if not part:
            continue
        if part.startswith("{") and part.endswith("}"):
            tokens.append(part[1:-1])  # 参数名已是合法标识符
            continue
        token = _slug_token(part)
        tokens.append(token if token else "x")
    if not tokens:
        return f"{method.lower()}_root"
    return "_".join([method.lower(), *tokens])


def _assign_operation_ids(operations: list[_Operation]) -> None:
    """按（已排序的）操作顺序去重分配 operationId，保证稳定且唯一。"""
    used: set[str] = set()
    for op in operations:
        base = op.explicit_id or _generated_base_id(op.method, op.path)
        candidate = base
        ordinal = 2
        while candidate in used:
            candidate = f"{base}-{ordinal}"
            ordinal += 1
        op.operation_id = candidate
        used.add(candidate)


def export_openapi_fragment(items: list[RouteIn]) -> ExportOpenApiResponse:
    """把按注册顺序排列的路由转换为 OpenAPI 3.1 的 paths 片段。"""
    parse_errors: list[ParseError] = []
    operations: list[_Operation] = []
    seen: dict[tuple[str, str], int] = {}  # (方法, 导出路径) -> 首个原始下标

    for index, item in enumerate(items):
        method = item.method.strip().upper()
        if method not in ALLOWED_METHODS:
            parse_errors.append(
                ParseError(
                    index=index,
                    id=item.id,
                    method=item.method,
                    template=item.path,
                    error=f"unsupported HTTP method {item.method!r}",
                )
            )
            continue
        try:
            parsed = parse_template(item.path)
        except TemplateError as exc:
            parse_errors.append(
                ParseError(
                    index=index,
                    id=item.id,
                    method=method,
                    template=item.path,
                    error=str(exc),
                )
            )
            continue

        # 模板内已有名为 wildcard 的参数时，末尾 ** 无法再映射为同名参数。
        if parsed.wildcard and any(
            isinstance(seg, Param) and seg.name == WILDCARD_PARAM_NAME
            for seg in parsed.segments
        ):
            parse_errors.append(
                ParseError(
                    index=index,
                    id=item.id,
                    method=method,
                    template=item.path,
                    error=(
                        f"parameter name {WILDCARD_PARAM_NAME!r} is reserved for "
                        "the trailing '**' wildcard"
                    ),
                )
            )
            continue

        path, parameters = _convert_segments(parsed.segments)
        key = (method, path)
        if key in seen:
            parse_errors.append(
                ParseError(
                    index=index,
                    id=item.id,
                    method=method,
                    template=item.path,
                    error=(
                        f"duplicate route: {method} {path} already exported from "
                        f"index {seen[key]}; keeping the first registration"
                    ),
                )
            )
            continue
        seen[key] = index
        operations.append(
            _Operation(
                index=index,
                method=method,
                path=path,
                parameters=parameters,
                explicit_id=item.id or None,
            )
        )

    # 稳定排序：规范化路径，再方法；operationId 序号也在此顺序上分配。
    operations.sort(key=lambda op: (op.path, op.method))
    _assign_operation_ids(operations)

    paths: dict[str, dict] = {}
    for op in operations:
        operation: dict = {"operationId": op.operation_id}
        if op.parameters:
            operation["parameters"] = op.parameters
        # OpenAPI 3.1 要求每个操作带 responses，给出最小占位保证片段可直接嵌入。
        operation["responses"] = {"default": {"description": "Default response"}}
        paths.setdefault(op.path, {})[op.method.lower()] = operation

    return ExportOpenApiResponse(paths=paths, parse_errors=parse_errors)
