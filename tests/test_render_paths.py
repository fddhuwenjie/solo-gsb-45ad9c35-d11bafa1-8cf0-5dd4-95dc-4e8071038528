"""POST /render-paths：按模板反向生成 canonical path。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def render(routes):
    return client.post("/render-paths", json={"routes": routes})


def result(routes):
    body = render(routes).json()
    return body["results"]


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
def test_static_and_typed_params():
    routes = [
        {
            "id": "r1",
            "template": "/users/{id:int}/posts/{slug:slug}",
            "parameters": {"id": "42", "slug": "hello-world"},
        }
    ]
    res = result(routes)[0]
    assert res["canonical_path"] == "/users/42/posts/hello-world"
    assert res["normalized_template"] == "/users/{id:int}/posts/{slug:slug}"
    assert res["errors"] == []
    assert res["id"] == "r1"


def test_integer_value_accepted_as_number():
    res = result(
        [{"template": "/users/{id:int}", "parameters": {"id": 7}}]
    )[0]
    assert res["canonical_path"] == "/users/7"


def test_uuid_accepts_upper_and_lower_hex():
    res = result(
        [
            {
                "template": "/u/{id:uuid}",
                "parameters": {"id": "AABBCCDD-aaaa-BBBB-cccc-0123456789ab"},
            }
        ]
    )[0]
    assert (
        res["canonical_path"]
        == "/u/AABBCCDD-aaaa-BBBB-cccc-0123456789ab"
    )


def test_untyped_param_accepts_any_nonempty_segment():
    res = result([{"template": "/files/{name}", "parameters": {"name": "r e port.pdf"}}])[0]
    assert res["canonical_path"] == "/files/r%20e%20port.pdf"


def test_root_template_renders_root():
    res = result([{"template": "/"}])[0]
    assert res["canonical_path"] == "/"
    assert res["normalized_template"] == "/"


def test_template_normalization_reflected():
    res = result([{"template": "/a/./b/{x}/", "parameters": {"x": "1"}}])[0]
    assert res["normalized_template"] == "/a/b/{x}"
    assert res["canonical_path"] == "/a/b/1"


# --------------------------------------------------------------------------- #
# RFC 3986 单段百分号编码
# --------------------------------------------------------------------------- #
def test_slash_inside_param_is_percent_encoded():
    # 一个参数永远只占一段：/ 不会引入额外分隔符。
    res = result([{"template": "/f/{name}", "parameters": {"name": "a/b"}}])[0]
    assert res["canonical_path"] == "/f/a%2Fb"


def test_reserved_and_non_ascii_encoding():
    res = result(
        [{"template": "/f/{name}", "parameters": {"name": "a?b#c d/e%f+g~h"}}]
    )[0]
    # ~ 是 unreserved 不编码；其余全部百分号编码（空格 %20）。
    assert res["canonical_path"] == "/f/a%3Fb%23c%20d%2Fe%25f%2Bg~h"


def test_utf8_percent_encoded():
    res = result([{"template": "/f/{name}", "parameters": {"name": "你好"}}])[0]
    assert res["canonical_path"] == "/f/%E4%BD%A0%E5%A5%BD"


def test_encoded_output_is_normalization_stable():
    # 原始值里带 % 与点号片段：编码后重新规范化必须得到同一段。
    res = result(
        [{"template": "/f/{name}", "parameters": {"name": "%41.txt"}}]
    )[0]
    assert res["canonical_path"] == "/f/%2541.txt"


# --------------------------------------------------------------------------- #
# 末尾 ** 通配
# --------------------------------------------------------------------------- #
def test_wildcard_multiple_segments_encoded_individually():
    res = result(
        [{"template": "/assets/**", "wildcard": ["img/a", "lo go", "x?y"]}]
    )[0]
    assert res["canonical_path"] == "/assets/img%2Fa/lo%20go/x%3Fy"


def test_wildcard_empty_or_absent_matches_zero_segments():
    for wc in (None, [], {}):
        routes = [{"template": "/assets/**", "wildcard": wc}] if wc != {} else [
            {"template": "/assets/**"}
        ]
        res = result(routes)[0]
        assert res["canonical_path"] == "/assets"


def test_root_wildcard_with_empty_tail_is_root():
    res = result([{"template": "/**", "wildcard": []}])[0]
    assert res["canonical_path"] == "/"


def test_wildcard_dot_segments_rejected():
    res = result([{"template": "/a/**", "wildcard": ["ok", "..", ".", ""]}])[0]
    assert res["canonical_path"] is None
    fields = [(e["code"], e["field"]) for e in res["errors"]]
    assert ("invalid_wildcard", "wildcard[1]") in fields
    assert ("invalid_wildcard", "wildcard[2]") in fields
    assert ("invalid_wildcard", "wildcard[3]") in fields
    assert all(f != "wildcard[0]" for _, f in fields)


def test_wildcard_bad_entry_type():
    # 整数条目与参数一样接受（"1" 是合法段）；布尔/null/数组/对象拒绝。
    res = result(
        [{"template": "/a/**", "wildcard": [1, True, None, ["x"], {"k": 1}]}]
    )[0]
    assert res["canonical_path"] is None
    assert [e["field"] for e in res["errors"]] == [
        "wildcard[1]",
        "wildcard[2]",
        "wildcard[3]",
        "wildcard[4]",
    ]
    assert all(e["code"] == "invalid_wildcard" for e in res["errors"])


def test_wildcard_not_allowed_for_non_wildcard_template():
    # 非空数组对非通配模板是额外输入；空数组/null 无害，允许。
    res = result([{"template": "/a/{x}", "parameters": {"x": "v"}, "wildcard": ["z"]}])[0]
    assert [e["code"] for e in res["errors"]] == ["wildcard_not_allowed"]
    assert res["canonical_path"] is None

    ok = result(
        [{"template": "/a/{x}", "parameters": {"x": "v"}, "wildcard": []}]
    )[0]
    assert ok["canonical_path"] == "/a/v"


# --------------------------------------------------------------------------- #
# 参数校验：缺少 / 额外 / 不合规
# --------------------------------------------------------------------------- #
def test_missing_parameters_reported_in_template_order():
    res = result(
        [{"template": "/a/{x}/b/{y:int}/c/{z:uuid}"}]
    )[0]
    assert [e["field"] for e in res["errors"]] == ["x", "y", "z"]
    assert all(e["code"] == "missing_parameter" for e in res["errors"])
    assert res["canonical_path"] is None


def test_extra_parameters_reported_sorted_by_name():
    res = result(
        [{"template": "/a/{x}", "parameters": {"x": "v", "zeta": 1, "alpha": 2}}]
    )[0]
    assert [e["field"] for e in res["errors"]] == ["alpha", "zeta"]
    assert all(e["code"] == "extra_parameter" for e in res["errors"])


def test_int_constraint_violations():
    for bad in ("abc", "-1", "01", "1.0", ""):
        res = result([{"template": "/u/{id:int}", "parameters": {"id": bad}}])[0]
        assert res["canonical_path"] is None, bad
        assert res["errors"][0]["code"] == "invalid_parameter"
        assert res["errors"][0]["field"] == "id"


def test_uuid_constraint_violation():
    res = result(
        [{"template": "/u/{id:uuid}", "parameters": {"id": "not-a-uuid"}}]
    )[0]
    assert res["errors"][0]["code"] == "invalid_parameter"


def test_slug_constraint_violation():
    for bad in ("UPPER", "a b", "-a", "a--b-"):
        res = result([{"template": "/p/{s:slug}", "parameters": {"s": bad}}])[0]
        assert res["errors"][0]["code"] == "invalid_parameter", bad


def test_dot_segments_as_param_value_rejected():
    for bad in (".", ".."):
        res = result([{"template": "/d/{x}", "parameters": {"x": bad}}])[0]
        assert res["canonical_path"] is None
        assert res["errors"][0]["code"] == "invalid_parameter"


def test_invalid_scalar_types():
    # 布尔、浮点、null、数组、对象都不是合法单段值。
    for bad in (True, False, 1.5, None, ["a"], {"k": "v"}):
        res = result([{"template": "/x/{y}", "parameters": {"y": bad}}])[0]
        assert res["errors"][0]["code"] == "invalid_parameter", bad
        assert res["canonical_path"] is None


def test_null_is_invalid_not_missing():
    res = result([{"template": "/x/{y}", "parameters": {"y": None}}])[0]
    assert res["errors"][0]["code"] == "invalid_parameter"


def test_multiple_errors_collected_together():
    res = result(
        [
            {
                "template": "/a/{x:int}/{y}",
                "parameters": {"x": "nope", "extra1": 1, "extra2": 2},
            }
        ]
    )[0]
    codes = [(e["code"], e["field"]) for e in res["errors"]]
    assert ("missing_parameter", "y") in codes
    assert ("invalid_parameter", "x") in codes
    assert ("extra_parameter", "extra1") in codes
    assert ("extra_parameter", "extra2") in codes
    assert res["canonical_path"] is None


# --------------------------------------------------------------------------- #
# 模板错误、批处理与信封
# --------------------------------------------------------------------------- #
def test_invalid_template_reported_per_item():
    res = result(
        [
            {"id": "ok", "template": "/a/{x}", "parameters": {"x": "v"}},
            {"id": "bad", "template": "no-leading-slash"},
            {"id": "bad2", "template": "/p/{n:float}"},
        ]
    )
    assert res[0]["canonical_path"] == "/a/v"
    assert res[1]["errors"][0]["code"] == "template_invalid"
    assert res[1]["normalized_template"] is None
    assert res[2]["errors"][0]["code"] == "template_invalid"


def test_batch_continues_after_failure_and_keeps_input_order():
    routes = [
        {"id": f"r{i}", "template": t, "parameters": {"x": "v"}}
        for i, t in enumerate(["/a/{x}", "/b/{x}", "/bad {x}", "/c/{x}"])
    ]
    body = render(routes).json()
    results = body["results"]
    assert len(results) == 4
    assert [r["index"] for r in results] == [0, 1, 2, 3]
    assert [r["id"] for r in results] == ["r0", "r1", "r2", "r3"]
    assert results[0]["canonical_path"] == "/a/v"
    assert results[1]["canonical_path"] == "/b/v"
    assert results[2]["canonical_path"] is None
    assert results[3]["canonical_path"] == "/c/v"


def test_empty_batch():
    body = render([]).json()
    assert body == {"results": []}


def test_results_are_stable_across_calls():
    routes = [
        {"template": "/z/{x}", "parameters": {"x": "1", "m": 1, "a": 2}},
        {"template": "/a/{x}", "parameters": {"x": "1"}},
    ]
    assert render(routes).json() == render(routes).json()


def test_id_null_when_absent():
    res = result([{"template": "/a/{x}", "parameters": {"x": "v"}}])[0]
    assert res["id"] is None
    assert res["index"] == 0
    assert res["template"] == "/a/{x}"


def test_envelope_422_when_template_missing():
    response = render([{"id": "x", "parameters": {}}])
    assert response.status_code == 422


def test_envelope_422_when_routes_missing():
    response = client.post("/render-paths", json={})
    assert response.status_code == 422


def test_extra_envelope_fields_ignored():
    response = render([{"template": "/a", "unknown": 123}])
    assert response.status_code == 200
    assert response.json()["results"][0]["canonical_path"] == "/a"
