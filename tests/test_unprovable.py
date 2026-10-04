import re

import pytest

from app import constraints
from app.service import analyze_routes
from app.models import RouteIn


@pytest.fixture
def weird_constraint():
    """自定义类型：单字符小写。声明与 slug“相交但无见证”。"""
    weird = constraints.Constraint(
        name="weird",
        pattern=re.compile(r"[a-z]"),
        witness=None,
        rank=5,
    )
    constraints.register_constraint(weird, intersections={"slug": constraints.NO_WITNESS})
    yield weird
    constraints.reset_extensions()


def test_unprovable_pair_is_flagged(weird_constraint):
    routes = [
        RouteIn(method="GET", template="/x/{a:weird}", id="w"),
        RouteIn(method="GET", template="/x/{b:slug}", id="s"),
    ]
    result = analyze_routes(routes)
    assert result.summary.unprovable == 1
    conflict = result.conflicts[0]
    assert conflict.status == "unprovable"
    assert conflict.witness_path is None
    assert conflict.reason
    assert "no constructible witness" in conflict.reason


def test_builtin_constraints_all_provable():
    # 所有内置类型组合要么不相交，要么一定有见证。
    routes = [
        RouteIn(method="GET", template="/x/{a:int}", id="i"),
        RouteIn(method="GET", template="/x/{b:slug}", id="s"),
        RouteIn(method="GET", template="/x/{c}", id="u"),
        RouteIn(method="GET", template="/x/{d:uuid}", id="u2"),
    ]
    result = analyze_routes(routes)
    assert result.summary.unprovable == 0
    # int×slug、int×any、slug×any、uuid×slug、uuid×any 都可证实
    assert result.summary.conflicts == 5
