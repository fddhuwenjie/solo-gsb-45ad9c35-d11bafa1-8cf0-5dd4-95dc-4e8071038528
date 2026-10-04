from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def export(routes):
    return client.post("/export-openapi", json={"routes": routes})


def test_empty_routes():
    response = export([])
    assert response.status_code == 200
    assert response.json() == {"paths": {}, "parse_errors": []}


def test_static_and_typed_params():
    routes = [
        {"method": "GET", "path": "/users/{id:int}", "id": "getUser"},
        {"method": "GET", "path": "/users/{uid:uuid}"},
        {"method": "GET", "path": "/posts/{slug:slug}"},
        {"method": "GET", "path": "/files/{name}"},
    ]
    body = export(routes).json()
    assert body["parse_errors"] == []
    paths = body["paths"]

    op = paths["/users/{id}"]["get"]
    assert op["operationId"] == "getUser"
    assert op["parameters"] == [
        {
            "name": "id",
            "in": "path",
            "required": True,
            "schema": {"type": "integer", "minimum": 0},
        }
    ]

    uuid_param = paths["/users/{uid}"]["get"]["parameters"][0]
    assert uuid_param["schema"] == {"type": "string", "format": "uuid"}

    slug_param = paths["/posts/{slug}"]["get"]["parameters"][0]
    assert slug_param["schema"] == {
        "type": "string",
        "pattern": "^[a-z0-9]+(?:-[a-z0-9]+)*$",
    }

    any_param = paths["/files/{name}"]["get"]["parameters"][0]
    assert any_param["schema"] == {"type": "string", "minLength": 1}

    # 缺省 operationId 由方法 + 规范化路径生成
    assert paths["/users/{uid}"]["get"]["operationId"] == "get_users_uid"
    assert paths["/posts/{slug}"]["get"]["operationId"] == "get_posts_slug"
    assert paths["/files/{name}"]["get"]["operationId"] == "get_files_name"


def test_wildcard_mapped_to_named_parameter():
    routes = [{"method": "GET", "path": "/assets/**", "id": "assets"}]
    body = export(routes).json()
    op = body["paths"]["/assets/{wildcard}"]["get"]
    assert op["operationId"] == "assets"
    assert op["parameters"] == [
        {
            "name": "wildcard",
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
            "x-route-wildcard": True,
        }
    ]


def test_root_and_catchall():
    routes = [
        {"method": "GET", "path": "/"},
        {"method": "GET", "path": "/**"},
    ]
    paths = export(routes).json()["paths"]
    assert paths["/"]["get"]["operationId"] == "get_root"
    assert "parameters" not in paths["/"]["get"]
    assert paths["/{wildcard}"]["get"]["operationId"] == "get_wildcard"
    assert paths["/{wildcard}"]["get"]["parameters"][0]["x-route-wildcard"] is True


def test_operations_sorted_by_path_then_method():
    routes = [
        {"method": "POST", "path": "/b"},
        {"method": "GET", "path": "/b"},
        {"method": "GET", "path": "/a"},
    ]
    paths = export(routes).json()["paths"]
    assert list(paths.keys()) == ["/a", "/b"]
    assert list(paths["/b"].keys()) == ["get", "post"]


def test_generated_operation_id_uses_duplicate_ordinal():
    # 两个不同路径折叠出相同的基础 operationId：排序靠后的追加重复序号
    routes = [
        {"method": "GET", "path": "/a_b"},
        {"method": "GET", "path": "/a-b"},
    ]
    paths = export(routes).json()["paths"]
    assert paths["/a-b"]["get"]["operationId"] == "get_a_b"
    assert paths["/a_b"]["get"]["operationId"] == "get_a_b-2"


def test_explicit_id_preferred_and_deduped():
    routes = [
        {"method": "GET", "path": "/b", "id": "same"},
        {"method": "GET", "path": "/a", "id": "same"},
    ]
    paths = export(routes).json()["paths"]
    # 序号在排序后的顺序上分配，与输入顺序无关
    assert paths["/a"]["get"]["operationId"] == "same"
    assert paths["/b"]["get"]["operationId"] == "same-2"


def test_duplicate_same_method_path_returns_parse_error():
    routes = [
        {"method": "GET", "path": "/x/{i:int}", "id": "first"},
        {"method": "GET", "path": "/x/{i:int}/", "id": "second"},  # 规范化后相同
    ]
    body = export(routes).json()
    assert [e["index"] for e in body["parse_errors"]] == [1]
    error = body["parse_errors"][0]
    assert error["id"] == "second"
    assert "index 0" in error["error"]
    # 先注册者保留，不被静默覆盖
    assert body["paths"]["/x/{i}"]["get"]["operationId"] == "first"


def test_constraint_variants_collapse_to_same_path():
    # 约束不进入 OpenAPI 路径：{id:int} 与 {id:uuid} 导出为同一路径
    routes = [
        {"method": "GET", "path": "/x/{id:int}"},
        {"method": "GET", "path": "/x/{id:uuid}"},
    ]
    body = export(routes).json()
    assert [e["index"] for e in body["parse_errors"]] == [1]
    assert list(body["paths"].keys()) == ["/x/{id}"]


def test_same_path_different_methods_not_duplicates():
    routes = [
        {"method": "GET", "path": "/x"},
        {"method": "POST", "path": "/x"},
    ]
    body = export(routes).json()
    assert body["parse_errors"] == []
    assert set(body["paths"]["/x"].keys()) == {"get", "post"}


def test_wildcard_param_name_reserved():
    routes = [{"method": "GET", "path": "/a/{wildcard}/**"}]
    body = export(routes).json()
    assert body["paths"] == {}
    assert len(body["parse_errors"]) == 1
    assert "wildcard" in body["parse_errors"][0]["error"]


def test_parse_errors_do_not_abort_batch():
    routes = [
        {"method": "GET", "path": "/ok"},
        {"method": "GET", "path": "/bad/{n:float}"},
        {"method": "FROG", "path": "/x"},
    ]
    body = export(routes).json()
    assert [e["index"] for e in body["parse_errors"]] == [1, 2]
    assert list(body["paths"].keys()) == ["/ok"]


def test_no_conflict_statistics():
    routes = [
        {"method": "GET", "path": "/a/**"},
        {"method": "GET", "path": "/a/b"},
    ]
    body = export(routes).json()
    assert set(body.keys()) == {"paths", "parse_errors"}


def test_normalized_templates_exported():
    # 点段、尾随斜杠、百分号转义按审计模型规范化后再导出
    routes = [{"method": "GET", "path": "/%41/./b/"}]
    paths = export(routes).json()["paths"]
    assert list(paths.keys()) == ["/A/b"]


def test_invalid_envelope_returns_422():
    response = client.post("/export-openapi", json={"routes": [{"method": "GET"}]})
    assert response.status_code == 422


def test_analyze_still_works():
    response = client.post(
        "/analyze",
        json={"routes": [{"method": "GET", "path": "/a/**"}, {"method": "GET", "path": "/a/b"}]},
    )
    assert response.status_code == 200
    assert len(response.json()["conflicts"]) == 1
