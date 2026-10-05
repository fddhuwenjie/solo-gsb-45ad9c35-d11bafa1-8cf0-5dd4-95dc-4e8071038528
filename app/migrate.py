"""已发布路由的旧链接迁移清单（只读，不修改活动配置）。

对一批旧链接（方法 + 绝对路径），把它们在**固定活动快照**上命中的旧路由
映射到目标路由，并生成在目标路由下可直接调用的 canonical path：

1. 端点入口先固定当前活动快照（版本 + 路由）；
2. 目标路由集必须通过与 /publish **完全相同**的校验（解析错误、导出路径
   重复、full_shadow），否则整批拒绝（``results`` 为空）；
3. 逐链接在活动路由上按既有优先级与注册顺序选出唯一旧命中；
4. 旧命中与目标路由的对应关系以**同方法下唯一的非空显式 id** 建立——
   id 缺失或在任一侧同方法路由中不唯一，该链接都无法迁移；
5. 从旧模板提取命名参数的实际段值与末尾 ``**`` 吸收的各段，按**同名**
   填入目标模板：目标参数必须全部能从旧参数取得、目标不含 ``**`` 时旧
   尾部必须为空，且各段须满足目标声明的约束；
6. 生成路径再按**整套目标路由**重新匹配，只有胜方恰好是对应目标路由时
   才返回，避免新路径被另一条目标路由截获；
7. 段值始终保持**单段百分号编码**语义：``%2F`` 是段内字面量，不拆段、
   不重复编码。

任一条链接失败只在其结果的 ``errors`` 中逐项说明，不影响其它链接。
"""

from __future__ import annotations

from urllib.parse import unquote_to_bytes

from . import analyzer
from .models import (
    ALLOWED_METHODS,
    AttributionHit,
    LinkMigration,
    MigrateLinksResponse,
    MigrationError,
    RouteIn,
    SampleIn,
)
from .normalization import PathError, normalize_path, render
from .publish import Snapshot, validate_candidate
from .service import parse_route_items
from .templates import Param, Static, Wildcard

#: 与 render.py 的单段编码一致：unreserved 之外的字节一律转义。
_UNRESERVED_BYTES = set(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)


def _error(code: str, message: str, field: str | None = None) -> MigrationError:
    return MigrationError(code=code, field=field, message=message)


def _canonical_segment(normalized_text: str) -> str:
    """把已规范化段文本恢复为逻辑值后再做单段编码。

    规范化段里保留的 ``%2F`` 等转义先按字节解码，得到调用方的原始段值，
    再用与 /render-paths 相同的单段规则重新编码——既不会把 ``%2F`` 拆成
    段分隔符，也不会对已转义形式重复编码（``%2F`` 往返后仍是 ``%2F``，
    ``%252F`` 往返后仍是 ``%252F``）。
    """
    raw_bytes = unquote_to_bytes(normalized_text)
    out: list[str] = []
    for byte in raw_bytes:
        if byte in _UNRESERVED_BYTES:
            out.append(chr(byte))
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def _extract_values(
    route: analyzer.Route, segments: list[str]
) -> tuple[dict[str, str], list[str]]:
    """从命中路径中按旧模板提取命名参数值与末尾 ** 各段（均为规范化文本）。"""
    values: dict[str, str] = {}
    fixed = route.fixed
    for position, seg in enumerate(fixed):
        if isinstance(seg, Param):
            values[seg.name] = segments[position]
    tail = segments[len(fixed):] if route.parsed.wildcard else []
    return values, tail


def _hit(route: analyzer.Route) -> AttributionHit:
    return AttributionHit(
        index=route.index,
        id=route.route_id,
        normalized_template=route.parsed.normalized,
    )


def _validate_link_sample(
    link: SampleIn,
) -> tuple[str | None, list[str] | None, list[MigrationError]]:
    """校验方法与绝对路径；返回（大写方法, 规范化段, 错误）。"""
    method = link.method.strip().upper()
    errors: list[MigrationError] = []
    if method not in ALLOWED_METHODS:
        errors.append(_error("invalid_method", f"unsupported HTTP method {link.method!r}"))
    if not link.path.startswith("/"):
        errors.append(
            _error(
                "invalid_path",
                f"path must be an absolute path starting with '/': {link.path!r}",
            )
        )
        return method, None, errors
    try:
        segments = normalize_path(link.path)
    except PathError as exc:
        errors.append(_error("invalid_path", str(exc)))
        return method, None, errors
    return method, segments, errors


def _migrate_one(
    index: int,
    link: SampleIn,
    active_by_method: dict[str, list[analyzer.Route]],
    candidate_by_method: dict[str, list[analyzer.Route]],
    active_ids: dict[int, str | None],
    candidate_ids: dict[int, str | None],
    active_version: int,
) -> LinkMigration:
    """迁移单个旧链接；任何失败只记录在该项的 errors 中。"""
    method, segments, errors = _validate_link_sample(link)
    result = LinkMigration(
        index=index,
        method=method or link.method,
        path=link.path,
        active_version=active_version,
        normalized_path=render(segments) if segments is not None else None,
    )
    if errors:
        result.errors = errors
        return result

    assert method is not None and segments is not None

    # 1) 在活动快照上按既有优先级、同级注册顺序找唯一旧命中。
    active_routes = active_by_method.get(method, [])
    old = max(
        (route for route in active_routes if analyzer.matches(route, segments)),
        key=lambda route: (route.priority, -route.order_in_method),
        default=None,
    )
    if old is None:
        result.errors = [
            _error("no_active_match", "no active route matches the old link")
        ]
        return result
    result.active = _hit(old)

    old_explicit_id = active_ids.get(old.index)
    if not old_explicit_id:
        result.errors = [
            _error(
                "active_id_missing",
                f"active route at index {old.index} has no non-empty explicit id",
            )
        ]
        return result

    # 2) 显式 id 必须在两侧的同方法路由中都唯一，才能无歧义地对应。
    active_same_id = [
        route
        for route in active_routes
        if active_ids.get(route.index) == old_explicit_id
    ]
    candidate_routes = candidate_by_method.get(method, [])
    candidate_same_id = [
        route
        for route in candidate_routes
        if candidate_ids.get(route.index) == old_explicit_id
    ]
    if len(active_same_id) != 1 or len(candidate_same_id) > 1:
        result.errors = [
            _error(
                "ambiguous_id",
                f"explicit id {old_explicit_id!r} is not unique among {method} "
                "routes in the active or target configuration",
            )
        ]
        return result
    if not candidate_same_id:
        result.errors = [
            _error(
                "target_missing",
                f"no target {method} route carries explicit id {old_explicit_id!r}",
            )
        ]
        return result

    target = candidate_same_id[0]
    result.target = _hit(target)

    # 3) 从旧命中提取命名参数段与末尾 ** 各段。
    old_values, old_tail = _extract_values(old, segments)

    # 4) 填入目标模板：目标命名参数只能由旧的**同名命名参数**提供；旧 ** 的
    #    各段只能整体填入目标末尾 **（绝不按位置/名字顶替目标参数）。
    target_fixed = target.fixed
    build_errors: list[MigrationError] = []
    canonical_values: dict[str, str] = {}
    for seg in target_fixed:
        if not isinstance(seg, Param):
            continue
        raw = old_values.get(seg.name)
        if raw is None:
            build_errors.append(
                _error(
                    "parameter_mismatch",
                    f"target template requires parameter {seg.name!r} that the old "
                    "route does not provide",
                    seg.name,
                )
            )
            continue
        if not seg.constraint.matches(raw):
            type_name = (
                "any non-empty string"
                if seg.constraint.name == "any"
                else seg.constraint.name
            )
            build_errors.append(
                _error(
                    "constraint_violated",
                    f"extracted value for {seg.name!r} does not satisfy {type_name} "
                    "constraint of the target route",
                    seg.name,
                )
            )
            continue
        canonical_values[seg.name] = _canonical_segment(raw)

    canonical_tail: list[str] = []
    if target.parsed.wildcard:
        for pos, raw in enumerate(old_tail):
            field_name = f"wildcard[{pos}]"
            # 内部空段只有 ** 能吸收；它无法作为可渲染的 canonical 段。
            if raw == "":
                build_errors.append(
                    _error(
                        "constraint_violated",
                        "extracted wildcard segment is empty and cannot render as a "
                        "path segment",
                        field_name,
                    )
                )
                continue
            canonical_tail.append(_canonical_segment(raw))
    elif old_tail:
        build_errors.append(
            _error(
                "parameter_mismatch",
                f"old route matched {len(old_tail)} trailing segment(s) via '**' but "
                "the target template has no trailing '**' to receive them",
                "wildcard",
            )
        )

    if build_errors:
        result.errors = build_errors
        return result

    canonical_segments: list[str] = []
    for seg in target.parsed.segments:
        if isinstance(seg, Static):
            canonical_segments.append(seg.text)
        elif isinstance(seg, Param):
            canonical_segments.append(canonical_values[seg.name])
        elif isinstance(seg, Wildcard):
            canonical_segments.extend(canonical_tail)

    canonical_path = render(canonical_segments)

    # 5) canonical 往返校验：规范化后段列表与渲染结果必须完全一致。
    try:
        re_normalized = normalize_path(canonical_path)
    except PathError:
        result.errors = [
            _error(
                "not_canonical",
                "rendered path does not survive the full normalization round-trip",
            )
        ]
        return result
    if re_normalized != canonical_segments or render(re_normalized) != canonical_path:
        result.errors = [
            _error(
                "not_canonical",
                "rendered path does not survive the full normalization round-trip",
            )
        ]
        return result

    # 6) 在整套目标路由上重新选胜方：只有胜方恰是对应目标路由才返回路径。
    winner = max(
        (
            route
            for route in candidate_routes
            if analyzer.matches(route, re_normalized)
        ),
        key=lambda route: (route.priority, -route.order_in_method),
        default=None,
    )
    if winner is not target:
        winner_desc = (
            f"index {winner.index} ({winner.route_id!r})"
            if winner is not None
            else "no route"
        )
        result.errors = [
            _error(
                "target_not_winner",
                f"rendered path is captured by another target route: {winner_desc}; "
                "the corresponding target route is not the winner",
            )
        ]
        return result

    result.canonical_path = canonical_path
    return result


def migrate_links(
    snapshot: Snapshot,
    target_routes: list[RouteIn],
    links: list[SampleIn],
) -> MigrateLinksResponse:
    """基于已固定的活动快照，逐链接生成旧路由到目标路由的 canonical path。"""
    validation = validate_candidate(target_routes)
    if not validation.ok:
        return MigrateLinksResponse(
            status="rejected",
            version=snapshot.version,
            parse_errors=validation.parse_errors,
            conflicts=validation.full_shadows,
        )

    # 活动侧路由来自发布时校验过的快照；按同一套解析规则重建匹配视图。
    active_items = [RouteIn.model_validate(raw) for raw in snapshot.routes]
    _, active_valid = parse_route_items(active_items)
    _, target_valid = parse_route_items(target_routes)

    active_ids = {r.index: active_items[r.index].id or None for r in active_valid}
    target_ids = {r.index: target_routes[r.index].id or None for r in target_valid}

    active_by_method: dict[str, list[analyzer.Route]] = {}
    for route in active_valid:
        active_by_method.setdefault(route.method, []).append(route)
    target_by_method: dict[str, list[analyzer.Route]] = {}
    for route in target_valid:
        target_by_method.setdefault(route.method, []).append(route)

    results = [
        _migrate_one(
            index,
            link,
            active_by_method,
            target_by_method,
            active_ids,
            target_ids,
            snapshot.version,
        )
        for index, link in enumerate(links)
    ]
    return MigrateLinksResponse(
        status="migrated", version=snapshot.version, results=results
    )
