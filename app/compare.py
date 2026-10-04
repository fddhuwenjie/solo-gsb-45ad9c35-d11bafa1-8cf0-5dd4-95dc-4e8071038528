"""样本请求归属预检：固定的活动快照 vs 候选路由集（只读，不修改活动配置）。

流程：

1. 端点入口先固定当前活动快照（版本 + 路由），整个比较都基于这同一份快照；
2. 候选路由集必须通过 /publish 的同款校验（解析错误、导出路径重复、
   full_shadow 均拒绝）；未通过时整批不做归属比较；
3. 每个样本按现有解码/规范化/参数约束/末尾 ``**`` 规则分别在两侧匹配，
   命中取优先级最高者，同级取注册更早者（与审计的胜出规则一致）；
4. 路由身份按（方法、规范化模板、显式 id）判定：同一条路由在候选中移动了
   注册下标不算改投。
"""

from __future__ import annotations

from . import analyzer
from .models import (
    ALLOWED_METHODS,
    AttributionHit,
    PrecheckResponse,
    RouteIn,
    SampleAttribution,
    SampleIn,
)
from .normalization import PathError, normalize_path, render
from .publish import Snapshot, validate_candidate
from .service import parse_route_items


def _group_by_method(
    routes: list[analyzer.Route],
) -> dict[str, list[analyzer.Route]]:
    """同方法分组（保持注册顺序）。"""
    groups: dict[str, list[analyzer.Route]] = {}
    for route in routes:
        groups.setdefault(route.method, []).append(route)
    return groups


def _best_match(
    routes: list[analyzer.Route], segments: list[str]
) -> analyzer.Route | None:
    """优先级降序、同级注册先后的唯一命中；无命中返回 None。"""
    return max(
        (route for route in routes if analyzer.matches(route, segments)),
        key=lambda route: (route.priority, -route.order_in_method),
        default=None,
    )


def _hit(route: analyzer.Route) -> AttributionHit:
    return AttributionHit(
        index=route.index,
        id=route.route_id,
        normalized_template=route.parsed.normalized,
    )


def _identity(route: analyzer.Route, explicit_ids: dict[int, str | None]) -> tuple:
    """归属身份：方法 + 规范化模板 + 显式 id（与注册下标无关）。"""
    return (route.method, route.parsed.normalized, explicit_ids[route.index])


def _compare_sample(
    index: int,
    sample: SampleIn,
    active_by_method: dict[str, list[analyzer.Route]],
    candidate_by_method: dict[str, list[analyzer.Route]],
    active_ids: dict[int, str | None],
    candidate_ids: dict[int, str | None],
) -> SampleAttribution:
    """比较单个样本在两侧的归属；非法方法/路径只影响该样本。"""
    method = sample.method.strip().upper()
    errors: list[str] = []
    if method not in ALLOWED_METHODS:
        errors.append(f"unsupported HTTP method {sample.method!r}")

    segments: list[str] | None = None
    if not sample.path.startswith("/"):
        errors.append(
            f"path must be an absolute path starting with '/': {sample.path!r}"
        )
    else:
        try:
            segments = normalize_path(sample.path)
        except PathError as exc:
            errors.append(str(exc))

    result = SampleAttribution(
        index=index,
        method=method,
        path=sample.path,
        normalized_path=render(segments) if segments is not None else None,
        errors=errors,
    )
    if errors:
        return result

    assert segments is not None
    active_hit = _best_match(active_by_method.get(method, []), segments)
    candidate_hit = _best_match(candidate_by_method.get(method, []), segments)
    result.active = _hit(active_hit) if active_hit is not None else None
    result.candidate = _hit(candidate_hit) if candidate_hit is not None else None

    if active_hit is None or candidate_hit is None:
        # 仅一侧有命中才算改投；两侧都无命中视为归属不变（均为空）。
        result.changed = (active_hit is None) != (candidate_hit is None)
    else:
        result.changed = _identity(active_hit, active_ids) != _identity(
            candidate_hit, candidate_ids
        )
    return result


def compare_attribution(
    snapshot: Snapshot,
    candidate_routes: list[RouteIn],
    samples: list[SampleIn],
) -> PrecheckResponse:
    """基于已固定的活动快照，比较每个样本在两侧的路由归属。"""
    validation = validate_candidate(candidate_routes)
    if not validation.ok:
        return PrecheckResponse(
            status="rejected",
            version=snapshot.version,
            parse_errors=validation.parse_errors,
            conflicts=validation.full_shadows,
        )

    # 活动侧路由来自发布时校验过的快照；按同一套解析规则重建匹配视图。
    active_items = [RouteIn.model_validate(raw) for raw in snapshot.routes]
    _, active_valid = parse_route_items(active_items)
    _, candidate_valid = parse_route_items(candidate_routes)

    active_ids = {r.index: active_items[r.index].id or None for r in active_valid}
    candidate_ids = {
        r.index: candidate_routes[r.index].id or None for r in candidate_valid
    }

    active_by_method = _group_by_method(active_valid)
    candidate_by_method = _group_by_method(candidate_valid)

    results = [
        _compare_sample(
            index,
            sample,
            active_by_method,
            candidate_by_method,
            active_ids,
            candidate_ids,
        )
        for index, sample in enumerate(samples)
    ]
    return PrecheckResponse(
        status="compared", version=snapshot.version, results=results
    )
