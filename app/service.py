"""分析编排：解析、分组、成对相交判定、排序与建议顺序。"""

from __future__ import annotations

from dataclasses import dataclass

from . import analyzer
from .models import (
    ALLOWED_METHODS,
    AnalyzeResponse,
    Conflict,
    ParseError,
    RouteIn,
    RouteOut,
    SuggestedOrder,
    SuggestedRoute,
    Summary,
)
from .templates import Param, Static, TemplateError, parse_template


@dataclass
class _ConflictRecord:
    method: str
    winner: analyzer.Route
    loser: analyzer.Route
    kind: str  # duplicate | full_shadow | partial_overlap
    status: str  # proven | unprovable
    witness: list[str] | None = None
    reason: str | None = None


def _specificity_key(route: analyzer.Route) -> tuple:
    """更具体的模板排在前面；同级时稳定回退到注册下标。"""
    fixed = route.fixed
    static_count = sum(1 for seg in fixed if isinstance(seg, Static))
    # 靠前的静态/参数区分度更高：位置权重递减。
    specificity = 0
    for position, seg in enumerate(fixed):
        if isinstance(seg, Static):
            specificity += 1000 >> position
        elif isinstance(seg, Param):
            specificity += 100 >> position
    return (
        1 if route.parsed.wildcard else 0,  # 无通配优先
        -len(fixed),                        # 固定段更长优先
        -static_count,
        -specificity,
        route.order_in_method,
    )


def _classify_pair(a: analyzer.Route, b: analyzer.Route) -> _ConflictRecord | None:
    """对一对同方法路由判定冲突；不相交返回 None。"""
    result = analyzer.route_intersection(a, b)
    if not result.intersects:
        return None

    # 胜出规则：优先级高者胜；同级时注册更早者胜。
    if (a.priority, -a.order_in_method) >= (b.priority, -b.order_in_method):
        winner, loser = a, b
    else:
        winner, loser = b, a

    if result.unprovable:
        return _ConflictRecord(
            method=a.method,
            winner=winner,
            loser=loser,
            kind="partial_overlap",
            status="unprovable",
            reason=result.unprovable_reason,
        )

    witness = result.witness
    assert witness is not None
    if not analyzer.verify_witness(a, b, witness):
        return _ConflictRecord(
            method=a.method,
            winner=winner,
            loser=loser,
            kind="partial_overlap",
            status="unprovable",
            reason=(
                "constraints intersect structurally, but the generated witness does "
                "not survive full request normalization"
            ),
        )

    if analyzer.route_equal_sets(a, b):
        kind = "duplicate"
    elif analyzer.route_covers(winner, loser):
        kind = "full_shadow"
    else:
        kind = "partial_overlap"

    return _ConflictRecord(
        method=a.method,
        winner=winner,
        loser=loser,
        kind=kind,
        status="proven",
        witness=witness,
    )


def _render_witness(segments: list[str]) -> str:
    from .normalization import render

    return render(segments)


def parse_route_items(
    items: list[RouteIn],
) -> tuple[list[ParseError], list[analyzer.Route]]:
    """校验方法并解析模板；返回（解析错误，有效路由），下标均对应输入数组。"""
    parse_errors: list[ParseError] = []
    valid: list[analyzer.Route] = []

    method_counters: dict[str, int] = {}

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

        order = method_counters.get(method, 0)
        method_counters[method] = order + 1
        route_id = item.id if item.id else f"route-{index}"
        valid.append(
            analyzer.Route(
                index=index,
                route_id=route_id,
                method=method,
                priority=item.priority,
                parsed=parsed,
                order_in_method=order,
            )
        )

    return parse_errors, valid


def analyze_routes(items: list[RouteIn]) -> AnalyzeResponse:
    parse_errors, valid = parse_route_items(items)

    # 同方法分组（保持注册顺序）。
    groups: dict[str, list[analyzer.Route]] = {}
    for route in valid:
        groups.setdefault(route.method, []).append(route)

    records: list[_ConflictRecord] = []
    for method, routes in groups.items():
        for i in range(len(routes)):
            for j in range(i + 1, len(routes)):
                record = _classify_pair(routes[i], routes[j])
                if record is not None:
                    records.append(record)

    # 稳定排序：胜方优先级降序、方法、胜方规范化路径、双方原始下标。
    records.sort(
        key=lambda r: (
            -r.winner.priority,
            r.method,
            r.winner.parsed.normalized,
            r.winner.index,
            r.loser.index,
        )
    )

    conflicts = [
        Conflict(
            method=r.method,
            winner_index=r.winner.index,
            winner_id=r.winner.route_id,
            loser_index=r.loser.index,
            loser_id=r.loser.route_id,
            winner_template=r.winner.parsed.normalized,
            loser_template=r.loser.parsed.normalized,
            winner_priority=r.winner.priority,
            loser_priority=r.loser.priority,
            kind=r.kind,
            status=r.status,
            witness_path=_render_witness(r.witness) if r.witness is not None else None,
            reason=r.reason,
        )
        for r in records
    ]

    suggested: list[SuggestedOrder] = []
    for method in sorted(groups):
        routes = groups[method]
        ordered = sorted(routes, key=_specificity_key)
        entries: list[SuggestedRoute] = []
        for new_pos, route in enumerate(ordered, start=1):
            old_pos = route.order_in_method + 1
            entries.append(
                SuggestedRoute(
                    index=route.index,
                    id=route.route_id,
                    template=route.parsed.normalized,
                    priority=route.priority,
                    original_position=old_pos,
                    suggested_position=new_pos,
                    move=old_pos - new_pos,
                )
            )
        suggested.append(SuggestedOrder(method=method, order=entries))

    routes_out = [
        RouteOut(
            index=r.index,
            id=r.route_id,
            method=r.method,
            template=items[r.index].path,
            normalized_template=r.parsed.normalized,
            priority=r.priority,
            fixed_segments=r.fixed_len,
            wildcard=r.parsed.wildcard,
            warnings=r.parsed.warnings,
        )
        for r in valid
    ]

    shadowed = {
        r.loser.index
        for r in records
        if r.status == "proven" and r.kind in ("duplicate", "full_shadow")
    }

    summary = Summary(
        total=len(items),
        valid=len(valid),
        parse_errors=len(parse_errors),
        conflicts=sum(1 for r in records if r.status == "proven"),
        full_shadows=sum(
            1 for r in records if r.status == "proven" and r.kind == "full_shadow"
        ),
        duplicates=sum(
            1 for r in records if r.status == "proven" and r.kind == "duplicate"
        ),
        partial_overlaps=sum(
            1 for r in records if r.status == "proven" and r.kind == "partial_overlap"
        ),
        unprovable=sum(1 for r in records if r.status == "unprovable"),
        shadowed_routes=len(shadowed),
    )

    return AnalyzeResponse(
        routes=routes_out,
        parse_errors=parse_errors,
        conflicts=conflicts,
        suggested_order=suggested,
        summary=summary,
    )
