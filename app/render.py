"""按路由模板反向生成可调用 URL（canonical path）。

请求提交一条带参路由（模板 + 参数值 + 可选的末尾 ``**`` 段数组），本模块：

1. 用与审计器相同的词法/规范化规则解析模板；
2. 校验参数：缺少、额外逐项报告，取值必须满足 ``int`` / ``uuid`` / ``slug``
   （或通用类型）的单段约束；
3. 对每个参数值按 RFC 3986 做**单段**百分号编码——``/`` 编码为 ``%2F``，
   因而一个参数永远只占一段；
4. 末尾 ``**`` 接收可为空的段数组，逐段做同样的单段编码；
5. 做一次完整的规范化往返校验，确认生成路径规范化后段数、段值不变，
   才输出 canonical path。

单条路由的错误只影响该条：所有错误收集进结果的 ``errors``，批处理不中止，
结果顺序与输入一致。
"""

from __future__ import annotations

from urllib.parse import quote

from .models import (
    RenderPathError,
    RenderPathResult,
    RenderPathRouteIn,
    RenderPathsResponse,
)
from .normalization import normalize_path, render
from .templates import Param, Static, TemplateError, Wildcard, parse_template

#: RFC 3986 单段编码：unreserved 之外的所有字节一律转义（``safe=""``），
#: 因而 ``/`` -> ``%2F``，参数值不可能引入额外路径分隔符。
def _encode_segment(value: str) -> str:
    return quote(value, safe="")


def _has_control_chars(text: str) -> bool:
    return any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in text)


def _error(code: str, message: str, field: str | None = None) -> RenderPathError:
    return RenderPathError(code=code, field=field, message=message)


def _as_path_scalar(value: object) -> str | None:
    """JSON 标量 -> 待校验的段字符串。

    接受字符串，以及可精确表示为整数的 JSON 数字（方便 int 参数直接传数字）；
    布尔、浮点（如 1.5、1e3）、null、数组、对象一律拒绝。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    return str(value)


def _check_segment_value(
    field_name: str | None,
    raw: str,
    param: Param | None,
    errors: list[RenderPathError],
) -> bool:
    """校验一个参数/通配段取值，问题追加进 errors；返回是否可用。

    ``param`` 为 None 时按通配段处理（只做通用单段校验），``field_name``
    为 None 同样表示通配段；否则 ``field_name`` 即参数名。
    """
    code = "invalid_parameter" if param is not None else "invalid_wildcard"
    if raw == "":
        errors.append(
            _error(code, "path segment must not be empty", field_name)
        )
        return False
    if _has_control_chars(raw):
        errors.append(
            _error(code, "path segment contains ASCII control character", field_name)
        )
        return False
    if raw in (".", ".."):
        # 点段会被规范化移除，无法生成稳定的单段；编码后才出现 %2E 不受影响。
        errors.append(
            _error(
                code,
                "path segment '.' / '..' is removed by normalization and cannot "
                "be a literal segment",
                field_name,
            )
        )
        return False
    if param is not None and not param.constraint.matches(raw):
        type_name = (
            "any non-empty string"
            if param.constraint.name == "any"
            else param.constraint.name
        )
        errors.append(
            _error(
                "invalid_parameter",
                f"value for {param.name!r} does not satisfy {type_name} constraint",
                field_name,
            )
        )
        return False
    return True


def _render_one(index: int, item: RenderPathRouteIn) -> RenderPathResult:
    result = RenderPathResult(index=index, id=item.id, template=item.path)
    errors: list[RenderPathError] = []

    try:
        parsed = parse_template(item.path)
    except TemplateError as exc:
        result.errors = [_error("template_invalid", str(exc))]
        return result

    result.normalized_template = parsed.normalized
    expected = [seg for seg in parsed.segments if isinstance(seg, Param)]
    expected_names = [p.name for p in expected]
    provided = item.parameters or {}

    # 缺少的参数：按模板中的参数位置报告，保证错误顺序稳定。
    missing = [name for name in expected_names if name not in provided]
    for name in missing:
        errors.append(
            _error("missing_parameter", f"missing required parameter {name!r}", name)
        )

    # 额外的参数：按参数名排序后逐项报告，与 JSON 对象键序无关。
    extra = sorted(name for name in provided if name not in expected_names)
    for name in extra:
        errors.append(
            _error(
                "extra_parameter",
                f"unexpected parameter {name!r}: not present in the template",
                name,
            )
        )

    # 已提供参数的取值校验（缺少的参数跳过，不产生重复错误）。
    raw_values: dict[str, str] = {}
    for param in expected:
        if param.name not in provided:
            continue
        raw = _as_path_scalar(provided[param.name])
        if raw is None:
            errors.append(
                _error(
                    "invalid_parameter",
                    f"value for {param.name!r} must be a string (integers are "
                    "accepted as exact integers)",
                    param.name,
                )
            )
            continue
        if _check_segment_value(param.name, raw, param, errors):
            raw_values[param.name] = raw

    # 末尾 **：缺省/空数组匹配零段；模板不含 ** 时提供非空数组属额外输入。
    tail_raw: list[str] = []
    wildcard_entries = item.wildcard
    if wildcard_entries is not None and wildcard_entries and not parsed.wildcard:
        errors.append(
            _error(
                "wildcard_not_allowed",
                "template has no trailing '**' wildcard; do not supply wildcard "
                "segments",
                "wildcard",
            )
        )
    elif wildcard_entries:
        for pos, entry in enumerate(wildcard_entries):
            field_name = f"wildcard[{pos}]"
            raw = _as_path_scalar(entry)
            if raw is None:
                errors.append(
                    _error(
                        "invalid_wildcard",
                        f"wildcard segment {pos} must be a string", field_name
                    )
                )
                continue
            if _check_segment_value(field_name, raw, None, errors):
                tail_raw.append(raw)

    if errors:
        result.errors = errors
        return result

    # 按模板顺序拼装：静态段用规范化字面文本，参数/通配段做单段百分号编码。
    segments: list[str] = []
    for seg in parsed.segments:
        if isinstance(seg, Static):
            segments.append(seg.text)
        elif isinstance(seg, Param):
            segments.append(_encode_segment(raw_values[seg.name]))
        elif isinstance(seg, Wildcard):
            segments.extend(_encode_segment(raw) for raw in tail_raw)

    canonical = render(segments)

    # 规范化往返校验：重新规范化后段数、段值必须完全一致，否则不是 canonical。
    re_normalized = normalize_path(canonical)
    if re_normalized != segments or render(re_normalized) != canonical:
        result.errors = [
            _error(
                "not_canonical",
                "rendered path does not survive the full normalization round-trip",
            )
        ]
        return result

    result.canonical_path = canonical
    return result


def render_paths(items: list[RenderPathRouteIn]) -> RenderPathsResponse:
    """逐条反向渲染；单项失败不中止批处理，结果按输入顺序返回。"""
    results = [_render_one(i, item) for i, item in enumerate(items)]
    return RenderPathsResponse(results=results)
