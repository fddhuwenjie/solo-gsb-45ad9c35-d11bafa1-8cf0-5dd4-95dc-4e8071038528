import pytest

from app.normalization import PathError, normalize_path, render


def test_root():
    assert normalize_path("/") == []
    assert render([]) == "/"


def test_trailing_slash_removed():
    assert normalize_path("/a/b/") == ["a", "b"]
    assert normalize_path("/a///") == ["a"]


def test_dot_segments_rfc3986():
    assert normalize_path("/a/./b") == ["a", "b"]
    assert normalize_path("/a/../b") == ["b"]
    assert normalize_path("/../a") == ["a"]  # 不可越过根
    assert normalize_path("/a/b/./../c/") == ["a", "c"]


def test_interior_empty_segments_kept():
    assert normalize_path("/a//b") == ["a", "", "b"]


def test_unreserved_percent_decoded():
    assert normalize_path("/%41%42%43") == ["ABC"]
    assert normalize_path("/%7e") == ["~"]  # ~ 是非保留字符


def test_reserved_percent_stays_encoded_and_uppercased():
    assert normalize_path("/a%2fb") == ["a%2Fb"]
    assert normalize_path("/%3f") == ["%3F"]
    assert normalize_path("/a%2Fb") != normalize_path("/a/b")


def test_idempotent():
    once = normalize_path("/a%2e/%2e%2e/b/")
    raw = render(once)
    assert normalize_path(raw) == once


def test_malformed_escape_rejected():
    with pytest.raises(PathError):
        normalize_path("/%zz")
    with pytest.raises(PathError):
        normalize_path("/%2")
    with pytest.raises(PathError):
        normalize_path("/%")


def test_control_character_rejected():
    with pytest.raises(PathError):
        normalize_path("/%00")
    with pytest.raises(PathError):
        normalize_path("/a\x01b")
