"""路径参数约束：内置 int / uuid / slug 类型与默认的通用类型。

约束是对*单个已规范化路径段*的匹配规则。每种约束带有：

- ``pattern``：用于判断段是否匹配的正则（``re.fullmatch``）；
- ``witness``：一个稳定的最短代表值，用于构造示例路径；
- ``rank``：包含关系的层级编号，编号越大集合越小
  （``int`` 与 ``uuid`` 互不包含，都包含于 ``slug``，而 ``slug`` 包含于通用类型）。

两种内置约束的交集取 rank 更大者；``int`` 与 ``uuid`` 交集为空。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 稳定的示例值：刻意选用不随枚举顺序变化的固定字面量。
INT_WITNESS = "1"
UUID_WITNESS = "00000000-0000-0000-0000-000000000000"
SLUG_WITNESS = "a"
ANY_WITNESS = "a"

_INT_RE = re.compile(r"0|[1-9][0-9]*")
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_SLUG_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ANY_RE = re.compile(r".+")


@dataclass(frozen=True)
class Constraint:
    name: str
    pattern: re.Pattern[str]
    witness: str | None
    rank: int

    def matches(self, segment: str) -> bool:
        return self.pattern.fullmatch(segment) is not None


ANY = Constraint("any", _ANY_RE, ANY_WITNESS, 0)
SLUG = Constraint("slug", _SLUG_RE, SLUG_WITNESS, 1)
INT = Constraint("int", _INT_RE, INT_WITNESS, 2)
UUID = Constraint("uuid", _UUID_RE, UUID_WITNESS, 3)

_BUILTIN: dict[str, Constraint] = {
    "int": INT,
    "uuid": UUID,
    "slug": SLUG,
}


# “相交但无见证”的哨兵。内置类型的交集总有确定见证；该哨兵只可能通过
# register_constraint 注册的扩展交集出现，分析器据此标记 unprovable。
class _NoWitness:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅调试展示
        return "NO_WITNESS"


NO_WITNESS = _NoWitness()

_extra_intersections: dict[tuple[str, str], "Constraint | _NoWitness"] = {}
_extra: dict[str, Constraint] = {}


def get(name: str | None) -> Constraint:
    """按模板中的类型名取约束；缺省（``{name}``）为通用类型。"""
    if name is None:
        return ANY
    return _BUILTIN[name.lower()]


def is_known(name: str) -> bool:
    return name.lower() in _BUILTIN


def intersection(a: Constraint, b: Constraint):
    """返回两种约束的交集约束；不相交返回 ``None``；

    返回 ``NO_WITNESS`` 表示二者理论相交，但交集内无法构造见证
    （仅扩展约束可能触发）。
    """
    if a == b:
        return a
    key = (a.name, b.name)
    if key in _extra_intersections:
        return _extra_intersections[key]
    # rank 相同且不是同一约束（当前即 int 与 uuid）-> 不相交。
    if a.rank == b.rank:
        return None
    smaller = a if a.rank > b.rank else b
    # 内置层级关系：int/uuid 都落在 slug 字符集内，slug 落在通用类型内。
    if smaller is INT or smaller is UUID:
        larger = b if smaller is a else a
        if larger is not SLUG and larger is not ANY:
            return None
    return smaller


def subset(small: Constraint, large: Constraint) -> bool:
    """``small`` 接受的每个段是否也被 ``large`` 接受（内置层级）。"""
    if small == large:
        return True
    if small.rank <= large.rank:
        return False
    if small is INT or small is UUID:
        return large is SLUG or large is ANY
    if small is SLUG:
        return large is ANY
    return False


def register_constraint(
    constraint: Constraint,
    intersections: dict[str, "Constraint | _NoWitness"] | None = None,
) -> None:
    """（测试/高级用途）注册自定义参数约束。

    ``intersections`` 声明它与其它约束的交集：值为约束（须带有效 witness）
    表示可证实的交集；值为 ``NO_WITNESS`` 表示理论相交但无法构造见证，
    分析器将把相关冲突标为 unprovable。对称键自动补全。
    """
    _extra[constraint.name] = constraint
    _BUILTIN[constraint.name] = constraint
    for other, result in (intersections or {}).items():
        _extra_intersections[(constraint.name, other)] = result
        _extra_intersections[(other, constraint.name)] = result


def reset_extensions() -> None:
    """清除所有自定义约束（测试隔离用）。"""
    for name in list(_extra):
        _BUILTIN.pop(name, None)
    _extra.clear()
    _extra_intersections.clear()
