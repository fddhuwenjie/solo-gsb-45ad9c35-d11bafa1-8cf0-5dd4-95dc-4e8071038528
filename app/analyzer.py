"""可达集合相交、遮蔽判定与最短见证路径构造。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import constraints
from .normalization import normalize_path, render
from .templates import Param, ParsedTemplate, Segment, Static
# 防御性上限：见证路径不应异常膨胀。
_MAX_WITNESS_SEGMENTS = 64


@dataclass
class Route:
    index: int  # 在输入数组中的位置（含解析失败路由的空位）
    route_id: str
    method: str
    priority: int
    parsed: ParsedTemplate
    order_in_method: int  # 同方法内注册序号（从 0 起）

    @property
    def fixed(self) -> list[Segment]:
        """通配 ** 之前的固定段。"""
        if self.parsed.wildcard:
            return self.parsed.segments[:-1]
        return list(self.parsed.segments)

    @property
    def fixed_len(self) -> int:
        return len(self.fixed)


@dataclass
class IntersectionResult:
    intersects: bool
    witness: Optional[list[str]] = None  # 规范化段
    unprovable_reason: Optional[str] = None

    @property
    def proven(self) -> bool:
        return self.intersects and self.witness is not None

    @property
    def unprovable(self) -> bool:
        return self.intersects and self.witness is None


# --------------------------------------------------------------------------- #
# 单段层面的相交
# --------------------------------------------------------------------------- #
_DISJOINT = object()
_UNPROVABLE = object()


def _static_matches_constraint(text: str, constraint: constraints.Constraint) -> bool:
    return constraint.matches(text)


def _intersect_positions(a: Segment, b: Segment):
    """同位置两个固定段的相交判定。

    返回 ``(见证字符串,)`` 表示可证实相交；``_DISJOINT`` 表示不相交；
    ``_UNPROVABLE`` 表示约束相交但无可用见证。
    """
    if isinstance(a, Static) and isinstance(b, Static):
        return (a.text,) if a.text == b.text else _DISJOINT
    if isinstance(a, Static) and isinstance(b, Param):
        if _static_matches_constraint(a.text, b.constraint):
            return (a.text,)
        return _DISJOINT
    if isinstance(a, Param) and isinstance(b, Static):
        if _static_matches_constraint(b.text, a.constraint):
            return (b.text,)
        return _DISJOINT
    if isinstance(a, Param) and isinstance(b, Param):
        common = constraints.intersection(a.constraint, b.constraint)
        if common is None:
            return _DISJOINT
        if common is constraints.NO_WITNESS:
            return _UNPROVABLE
        witness = common.witness
        if witness is None:
            return _UNPROVABLE
        if a.constraint.matches(witness) and b.constraint.matches(witness):
            return (witness,)
        return _UNPROVABLE
    return _DISJOINT


# --------------------------------------------------------------------------- #
# 路由级相交
# --------------------------------------------------------------------------- #
def _tail_witness(seg: Segment):
    """较长模板多出的固定段的最短见证；无法构造时返回 None。"""
    if isinstance(seg, Static):
        return [seg.text]
    if isinstance(seg, Param):
        value = seg.constraint.witness
        if value is not None and seg.constraint.matches(value):
            return [value]
    return None  # Wildcard 或无见证约束：不需要/无法生成额外段


def route_intersection(a: Route, b: Route) -> IntersectionResult:
    """计算两条同方法路由可达集合的相交与最短见证。"""
    af, bf = a.fixed, b.fixed
    shared = min(len(af), len(bf))
    witness: list[str] = []

    for i in range(shared):
        seg_result = _intersect_positions(af[i], bf[i])
        if seg_result is _DISJOINT:
            return IntersectionResult(intersects=False)
        if seg_result is _UNPROVABLE:
            return IntersectionResult(
                intersects=True,
                unprovable_reason=(
                    f"parameter constraints intersect at segment {i + 1} "
                    "but no constructible witness exists in their intersection"
                ),
            )
        witness.append(seg_result[0])

    aw, bw = a.parsed.wildcard, b.parsed.wildcard

    # 固定段等长：** 可匹配零段，因此四种通配组合都在固定前缀处相交。
    if len(af) == len(bf):
        return IntersectionResult(intersects=True, witness=witness)

    # 固定段长度不等。
    if len(af) < len(bf):
        shorter, shorter_wild = af, aw
        longer = bf
    else:
        shorter, shorter_wild = bf, bw
        longer = af

    # 固定段较短的一方必须靠 ** 吸收对方多出的固定段。
    if not shorter_wild:
        return IntersectionResult(intersects=False)

    # 多出来的固定段取自较长方，逐段构造最短见证。
    extra_fixed = longer[len(shorter):]
    for seg in extra_fixed:
        extra = _tail_witness(seg)
        if extra is None:
            return IntersectionResult(
                intersects=True,
                unprovable_reason=(
                    "constraints intersect but the longer template requires an "
                    "additional segment that has no constructible witness"
                ),
            )
        witness.extend(extra)

    if len(witness) > _MAX_WITNESS_SEGMENTS:
        return IntersectionResult(
            intersects=True,
            unprovable_reason="witness path exceeds the maximum supported length",
        )

    return IntersectionResult(intersects=True, witness=witness)


# --------------------------------------------------------------------------- #
# 可达集合包含关系（用于区分完全遮蔽 / 部分相交）
# --------------------------------------------------------------------------- #
def _segment_covers(a: Segment, b: Segment) -> bool:
    """a 位置接受的段集合是否包含 b 位置接受的段集合。"""
    if isinstance(a, Static) and isinstance(b, Static):
        return a.text == b.text
    if isinstance(a, Static):
        return False  # 单字面量不可能覆盖参数集合
    if isinstance(b, Static):
        return a.constraint.matches(b.text)
    return constraints.subset(b.constraint, a.constraint)


def route_covers(a: Route, b: Route) -> bool:
    """a 的可达集合是否是 b 的可达集合的超集（a covers b）。"""
    af, bf = a.fixed, b.fixed
    aw, bw = a.parsed.wildcard, b.parsed.wildcard

    if len(af) > len(bf):
        return False
    for i in range(len(af)):
        if not _segment_covers(af[i], bf[i]):
            return False
    if len(af) == len(bf):
        if not aw and not bw:
            return True
        if aw:
            return True
        return False
    # a 固定段更短：必须靠 ** 吸收 b 的尾部。
    if not aw:
        return False
    if not bw:
        # b 的尾部是额外固定段，全部落在 a 的 ** 内。
        return True
    # b 尾部同样是 **：a 的 ** 覆盖 b 的固定尾段加其 **。
    return True


def route_equal_sets(a: Route, b: Route) -> bool:
    return route_covers(a, b) and route_covers(b, a)


# --------------------------------------------------------------------------- #
# 见证验证（完整模拟路由器：规范化后再匹配）
# --------------------------------------------------------------------------- #
def _segment_matches(seg: Segment, value: str) -> bool:
    if isinstance(seg, Static):
        return seg.text == value
    if isinstance(seg, Param):
        return seg.constraint.matches(value)
    return False


def matches(route: Route, path_segments: list[str]) -> bool:
    fixed = route.fixed
    if len(path_segments) < len(fixed):
        return False
    for i, seg in enumerate(fixed):
        if not _segment_matches(seg, path_segments[i]):
            return False
    if not route.parsed.wildcard and len(path_segments) != len(fixed):
        return False
    return True


def verify_witness(route_a: Route, route_b: Route, witness: list[str]) -> bool:
    """构造出的路径在重新规范化后必须仍同时命中双方。"""
    raw = render(witness)
    try:
        re_normalized = normalize_path(raw)
    except ValueError:
        return False
    if re_normalized != witness:
        return False
    return matches(route_a, re_normalized) and matches(route_b, re_normalized)
