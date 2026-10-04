import pytest

from app.templates import Param, Wildcard, TemplateError, parse_template


def types(segments):
    return [(type(s).__name__, getattr(s, "text", getattr(s, "name", None))) for s in segments]


def test_basic_static():
    t = parse_template("/users/profile")
    assert t.normalized == "/users/profile"
    assert t.wildcard is False
    assert types(t.segments) == [("Static", "users"), ("Static", "profile")]


def test_params():
    t = parse_template("/users/{id:int}/items/{slug:slug}")
    assert isinstance(t.segments[1], Param)
    assert t.segments[1].name == "id"
    assert t.segments[1].constraint.name == "int"
    assert t.segments[3].constraint.name == "slug"


def test_untyped_param_is_any():
    t = parse_template("/x/{name}")
    assert t.segments[1].constraint.name == "any"


def test_trailing_wildcard():
    t = parse_template("/assets/**")
    assert t.wildcard is True
    assert isinstance(t.segments[-1], Wildcard)
    assert t.normalized == "/assets/**"


def test_root_wildcard():
    t = parse_template("/**")
    assert t.wildcard is True
    assert t.normalized == "/**"
    assert len(t.segments) == 1


def test_wildcard_must_be_last():
    with pytest.raises(TemplateError):
        parse_template("/**/a")


def test_star_alone_rejected():
    with pytest.raises(TemplateError):
        parse_template("/a/*/b")


def test_unknown_type():
    with pytest.raises(TemplateError):
        parse_template("/x/{n:float}")


def test_invalid_param_name():
    with pytest.raises(TemplateError):
        parse_template("/x/{1n}")
    with pytest.raises(TemplateError):
        parse_template("/x/{a-b}")


def test_braces_must_wrap_segment():
    with pytest.raises(TemplateError):
        parse_template("/x/a{b}")
    with pytest.raises(TemplateError):
        parse_template("/x/{")


def test_duplicate_param_name():
    with pytest.raises(TemplateError):
        parse_template("/x/{a}/y/{a}")


def test_must_start_with_slash():
    with pytest.raises(TemplateError):
        parse_template("a/b")


def test_interior_empty_segment_rejected():
    with pytest.raises(TemplateError):
        parse_template("/a//b")


def test_trailing_slash_normalized_with_warning():
    t = parse_template("/users/{id:int}/")
    assert t.normalized == "/users/{id:int}"
    assert "trailing slash removed by normalization" in t.warnings


def test_dot_segments_normalized():
    t = parse_template("/a/./b/../c")
    assert t.normalized == "/a/c"


def test_percent_escape_normalized():
    t = parse_template("/%41/%2F/x")
    assert t.normalized == "/A/%2F/x"
    assert any("percent-escapes normalized" in w for w in t.warnings)


def test_malformed_escape_in_template():
    with pytest.raises(TemplateError):
        parse_template("/%zz")


def test_empty_type_rejected():
    with pytest.raises(TemplateError):
        parse_template("/x/{n:}")
