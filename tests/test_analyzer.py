from app import analyzer
from app.templates import parse_template


def mk(template, index=0, order=0, priority=0, method="GET"):
    return analyzer.Route(
        index=index,
        route_id=f"r{index}",
        method=method,
        priority=priority,
        parsed=parse_template(template),
        order_in_method=order,
    )


def witness(a, b):
    result = analyzer.route_intersection(mk(a), mk(b))
    if not result.proven:
        return None
    assert analyzer.verify_witness(mk(a), mk(b), result.witness)
    return result.witness


def assert_intersect(a, b, expected_path):
    from app.normalization import render

    assert witness(a, b) is not None
    assert render(witness(a, b)) == expected_path


def assert_disjoint(a, b):
    result = analyzer.route_intersection(mk(a), mk(b))
    assert result.intersects is False


def test_static_vs_int():
    # /users/1 同时命中
    assert_intersect("/users/{id:int}", "/users/1", "/users/1")
    # /users/me 不命中 int
    assert_disjoint("/users/{id:int}", "/users/me")


def test_int_uuid_disjoint():
    assert_disjoint("/u/{id:int}", "/u/{id:uuid}")


def test_int_subset_of_slug():
    assert_intersect("/f/{s:slug}", "/f/{i:int}", "/f/1")
    a, b = mk("/f/{s:slug}"), mk("/f/{i:int}")
    assert analyzer.route_covers(a, b) is True
    assert analyzer.route_covers(b, a) is False


def test_wildcard_matches_zero_segments():
    assert_intersect("/x/{n:int}", "/x/{n:int}/**", "/x/1")
    assert_intersect("/", "/**", "/")


def test_wildcard_covers_static_subpath():
    assert_intersect("/static/**", "/static/a/b", "/static/a/b")
    a, b = mk("/static/**"), mk("/static/a/b")
    assert analyzer.route_covers(a, b) is True
    assert analyzer.route_covers(b, a) is False


def test_wildcard_prefix_does_not_swallow_sibling():
    assert_disjoint("/static/**", "/")
    assert_disjoint("/a/**", "/b/c")


def test_wildcard_prefixes_overlap():
    assert_intersect("/a/**", "/a/b/**", "/a/b")


def test_encoded_slash_does_not_cross_segments():
    # %2F 不解码为分隔符
    assert_disjoint("/a%2Fb", "/a/b")
    assert_intersect("/a%2Fb", "/{x}", "/a%2Fb")


def test_statics_disjoint_by_value():
    assert_disjoint("/a/b", "/a/c")


def test_duplicate_templates_equal_sets():
    a, b = mk("/x/{i:int}"), mk("/x/{j:int}")
    assert analyzer.route_equal_sets(a, b)


def test_partial_overlap_neither_covers():
    # /a/{x}/c 与 /a/b/{y} 在 /a/b/c 处相交，但互不包含
    a, b = mk("/a/{x}/c"), mk("/a/b/{y}")
    assert_intersect("/a/{x}/c", "/a/b/{y}", "/a/b/c")
    assert analyzer.route_covers(a, b) is False
    assert analyzer.route_covers(b, a) is False


def test_root_and_static_disjoint():
    assert_disjoint("/", "/a")


def test_shortest_witness_uses_canonical_values():
    # uuid witness 固定为全零 UUID
    assert_intersect("/u/{id:uuid}", "/u/{id:uuid}",
                     "/u/00000000-0000-0000-0000-000000000000")


def test_normalization_creates_duplicate():
    a, b = mk("/a/./b"), mk("/a/b")
    assert analyzer.route_equal_sets(a, b)
