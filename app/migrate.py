"""旧链接迁移清单：把命中活动路由的旧链接重写到目标路由的 canonical path（只读）。

流程：

1. 端点入口先固定当前活动快照（版本 + 路由），整个迁移都基于这同一份快照；
2. 目标路由集必须通过 /publish 的同款校验（解析错误、导出路径重复、
   full_shadow 均拒绝）；未通过时整批拒绝，不生成任何迁移项；
3. 每条旧链接在活动路由中按「优先级降序、同级注册先后」找出唯一旧命中；
   命中路由须带非空显式 id，且该 id 在同方法下于活动与目标两侧都唯一，
   以此对应到唯一的目标路由；
4. 按旧模板逐段提取命名参数与末尾 ** 的各段（均为已规范化的单段文本，
   %2F 等保留字符保持原编码——不拆段、不重复编码），按名填入目标模板：
   目标参数全部有着落、旧提取值全部被目标模板接收、取值满足目标约束、
   旧 ** 尾段在目标侧有归宿时，才生成 canonical path；
5. 生成路径按整套目标路由重新选出胜方，胜方必须是该目标路由本身，
   否则说明新路径会被其它路由截获，该项不可迁移；
6. 单项失败不影响其它项；整个过程只读，不修改活动配置。
"""

from __future__ import annotations

from . import analyzer
from .compare import _best_match, _group_by_method, _hit
from .models import (
    ALLOWED_METHODS,
    MigrateError,
    MigrateResponse,
    MigrateResult,
    RouteIn,
    SampleIn,
)
from .normalization import PathError, normalize_path, render
from .publish import Snapshot, validate_candidate
from .service import parse_route_items
from .templates import Param, Static


def _explicit_id_map(
    items: list[RouteIn], routes: list[analyzer.Route]
) -> dict[tuple[str, str], list[analyzer.Route]]:
    """(方法, 非空显式 id) -> 路由列表；用于跨配置的唯一 id 对应。"""
    by_key: dict[tuple[str, str], list[analyzer.Route]] = {}
    for route in routes:
        explicit = items[route.index].id or None
        if explicit is None:
            continue
        by_key.setdefault((route.method, explicit), []).append(route)
    return by_key


def _extract_values(
    route: analyzer.Route, segments: list[str]
) -> tuple[dict[str, str], list[str]]:
    """按旧模板位置提取命名参数值与末尾 ** 各段（已规范化的单段文本）。"""
    values: dict[str, str] = {}
    for position, seg in enumerate(route.fixed):
        if isinstance(seg, Param):
            values[seg.name] = segments[position]
    tail = segments[route.fixed_len:] if route.parsed.wildcard else []
    return values, tail


def _fill_target(
    route: analyzer.Route, values: dict[str, str], tail: list[str]
) -> tuple[list[str], list[MigrateError]]:
    """把旧提取值按名填入目标模板；返回（目标路径段, 逐项错误）。

    对应必须是完整的：目标参数都要在旧提取值中有同名来源，旧提取值也都要
    被目标模板接收；旧 ** 尾段只能交给目标模板的末尾 **。
    """
    errors: list[MigrateError] = []
    built: list[str] = []
    consumed: set[str] = set()
    for seg in route.parsed.segments:
        if isinstance(seg, Static):
            built.append(seg.text)
        elif isinstance(seg, Param):
            if seg.name not in values:
                errors.append(
                    MigrateError(
                        code="parameter_unmapped",
                        message=(
                            f"target parameter {seg.name!r} has no counterpart "
                            "in the matched legacy route"
                        ),
                    )
                )
                continue
            consumed.add(seg.name)
            value = values[seg.name]
            if not seg.constraint.matches(value):
                errors.append(
                    MigrateError(
                        code="constraint_violation",
                        message=(
                            f"value {value!r} for parameter {seg.name!r} does "
                            f"not satisfy the {seg.constraint.name} constraint "
                            "of the target route"
                        ),
                    )
                )
                continue
            built.append(value)
        else:  # 末尾 **：旧通配尾段逐段搬入（可为零段）
            built.extend(tail)
    for name in sorted(set(values) - consumed):
        errors.append(
            MigrateError(
                code="parameter_unmapped",
                message=(
                    f"legacy parameter {name!r} has no counterpart in the "
                    "target route"
                ),
            )
        )
    if tail and not route.parsed.wildcard:
        errors.append(
            MigrateError(
                code="parameter_unmapped",
                message=(
                    "legacy wildcard segments have no counterpart: target "
                    "template has no trailing '**'"
                ),
            )
        )
    return built, errors


def _migrate_link(
    index: int,
    link: SampleIn,
    version: int,
    active_by_method: dict[str, list[analyzer.Route]],
    active_ids: dict[tuple[str, str], list[analyzer.Route]],
    active_explicit: dict[int, str | None],
    target_by_method: dict[str, list[analyzer.Route]],
    target_ids: dict[tuple[str, str], list[analyzer.Route]],
) -> MigrateResult:
    """迁移单条旧链接；非法样本与单项失败只影响该项。"""
    method = link.method.strip().upper()
    errors: list[MigrateError] = []
    if method not in ALLOWED_METHODS:
        errors.append(
            MigrateError(
                code="unsupported_method",
                message=f"unsupported HTTP method {link.method!r}",
            )
        )

    segments: list[str] | None = None
    if not link.path.startswith("/"):
        errors.append(
            MigrateError(
                code="invalid_path",
                message=(
                    f"path must be an absolute path starting with '/': "
                    f"{link.path!r}"
                ),
            )
        )
    else:
        try:
            segments = normalize_path(link.path)
        except PathError as exc:
            errors.append(MigrateError(code="invalid_path", message=str(exc)))

    result = MigrateResult(
        index=index,
        version=version,
        method=method,
        path=link.path,
        normalized_path=render(segments) if segments is not None else None,
        errors=errors,
    )
    if errors:
        return result
    assert segments is not None

    # 1) 旧命中：优先级降序、同级注册先后（与审计的胜出规则一致）。
    hit = _best_match(active_by_method.get(method, []), segments)
    if hit is None:
        result.errors.append(
            MigrateError(
                code="no_active_match",
                message="no active route matches this link",
            )
        )
        return result
    result.active = _hit(hit)

    # 2) 显式 id 对应：同方法下，活动与目标两侧都必须唯一。
    explicit = active_explicit[hit.index]
    if explicit is None:
        result.errors.append(
            MigrateError(
                code="missing_route_id",
                message=(
                    "matched legacy route has no explicit id; cannot pair it "
                    "with a target route"
                ),
            )
        )
        return result
    if len(active_ids[(method, explicit)]) > 1:
        result.errors.append(
            MigrateError(
                code="ambiguous_route_id",
                message=(
                    f"explicit id {explicit!r} is used by multiple active "
                    f"{method} routes"
                ),
            )
        )
        return result
    targets = target_ids.get((method, explicit), [])
    if not targets:
        result.errors.append(
            MigrateError(
                code="target_missing",
                message=f"no target {method} route carries explicit id {explicit!r}",
            )
        )
        return result
    if len(targets) > 1:
        result.errors.append(
            MigrateError(
                code="ambiguous_route_id",
                message=(
                    f"explicit id {explicit!r} is used by multiple target "
                    f"{method} routes"
                ),
            )
        )
        return result
    target = targets[0]
    result.target = _hit(target)

    # 3) 提取旧模板的命名参数与末尾 ** 各段，按名填入目标模板。
    values, tail = _extract_values(hit, segments)
    built, fill_errors = _fill_target(target, values, tail)
    if fill_errors:
        result.errors.extend(fill_errors)
        return result

    # 4) 规范化往返校验：生成结果必须稳定（单段编码语义不变）。
    canonical = render(built)
    try:
        re_normalized = normalize_path(canonical)
    except PathError:
        re_normalized = None
    if re_normalized is None or re_normalized != built:
        result.errors.append(
            MigrateError(
                code="not_canonical",
                message=(
                    "rendered path does not survive the full normalization "
                    "round-trip"
                ),
            )
        )
        return result

    # 5) 生成路径按整套目标路由重新选胜方，防止被其它路由截获。
    winner = _best_match(target_by_method.get(method, []), re_normalized)
    if winner is None or winner.index != target.index:
        detail = (
            "no target route matches it"
            if winner is None
            else f"it is intercepted by route {winner.route_id!r} "
            f"(index {winner.index})"
        )
        result.errors.append(
            MigrateError(
                code="intercepted",
                message=(
                    "generated path does not resolve to the paired target "
                    f"route under the full target route set: {detail}"
                ),
            )
        )
        return result

    result.canonical_path = canonical
    return result


def migrate_links(
    snapshot: Snapshot,
    target_routes: list[RouteIn],
    links: list[SampleIn],
) -> MigrateResponse:
    """基于已固定的活动快照，为每条旧链接生成迁移结果（只读）。"""
    validation = validate_candidate(target_routes)
    if not validation.ok:
        return MigrateResponse(
            status="rejected",
            version=snapshot.version,
            parse_errors=validation.parse_errors,
            conflicts=validation.full_shadows,
        )

    # 活动侧路由来自发布时校验过的快照；按同一套解析规则重建匹配视图。
    active_items = [RouteIn.model_validate(raw) for raw in snapshot.routes]
    _, active_valid = parse_route_items(active_items)
    _, target_valid = parse_route_items(target_routes)

    active_by_method = _group_by_method(active_valid)
    target_by_method = _group_by_method(target_valid)
    active_ids = _explicit_id_map(active_items, active_valid)
    target_ids = _explicit_id_map(target_routes, target_valid)
    active_explicit = {
        r.index: active_items[r.index].id or None for r in active_valid
    }

    results = [
        _migrate_link(
            index,
            link,
            snapshot.version,
            active_by_method,
            active_ids,
            active_explicit,
            target_by_method,
            target_ids,
        )
        for index, link in enumerate(links)
    ]
    return MigrateResponse(
        status="generated", version=snapshot.version, results=results
    )
