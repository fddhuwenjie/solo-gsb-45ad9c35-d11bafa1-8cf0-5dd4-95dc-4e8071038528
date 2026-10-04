from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(routes):
    return client.post("/analyze", json={"routes": routes})


def test_healthz():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_empty_routes():
    response = post([])
    assert response.status_code == 200
    body = response.json()
    assert body["routes"] == []
    assert body["conflicts"] == []
    assert body["summary"]["total"] == 0


def test_full_shadow_with_winner():
    # catch-all 先注册、高优先级；静态后注册——静态被遮蔽
    routes = [
        {"method": "get", "path": "/files/{name}", "priority": 5, "id": "all"},
        {"method": "GET", "path": "/files/report.pdf", "priority": 0, "id": "file"},
    ]
    body = post(routes).json()
    conflicts = body["conflicts"]
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["kind"] == "full_shadow"
    assert c["status"] == "proven"
    assert c["winner_id"] == "all"
    assert c["loser_id"] == "file"
    assert c["witness_path"] == "/files/report.pdf"


def test_registration_order_tiebreak():
    # 同优先级：注册更早者胜
    routes = [
        {"method": "GET", "path": "/a/**"},
        {"method": "GET", "path": "/a/b"},
    ]
    c = post(routes).json()["conflicts"][0]
    assert c["winner_index"] == 0
    assert c["loser_index"] == 1


def test_partial_overlap_classified():
    routes = [
        {"method": "GET", "path": "/a/{x}/c"},
        {"method": "GET", "path": "/a/b/{y}"},
    ]
    c = post(routes).json()["conflicts"][0]
    assert c["kind"] == "partial_overlap"
    assert c["witness_path"] == "/a/b/c"


def test_duplicate_templates():
    routes = [
        {"method": "GET", "path": "/x/{i:int}", "id": "first"},
        {"method": "GET", "path": "/x/{j:int}/", "id": "second"},
    ]
    body = post(routes).json()
    c = body["conflicts"][0]
    assert c["kind"] == "duplicate"
    # 两条完全重复的路由中，负方（注册更晚）被遮蔽；胜方是生效路由
    assert body["summary"]["shadowed_routes"] == 1


def test_method_scoping():
    routes = [
        {"method": "GET", "path": "/x"},
        {"method": "POST", "path": "/x"},
    ]
    body = post(routes).json()
    assert body["conflicts"] == []
    assert {o["method"] for o in body["suggested_order"]} == {"GET", "POST"}


def test_parse_errors_do_not_abort_batch():
    routes = [
        {"method": "GET", "path": "/ok"},
        {"method": "GET", "path": "/bad/{n:float}"},
        {"method": "FROG", "path": "/x"},
        {"method": "GET", "path": "also-ok"},  # 缺少前导斜杠，同样是解析错误
    ]
    body = post(routes).json()
    assert [e["index"] for e in body["parse_errors"]] == [1, 2, 3]
    assert body["summary"]["valid"] == 1


def test_witness_for_encoded_unreserved():
    routes = [
        {"method": "GET", "path": "/%41", "id": "enc"},
        {"method": "GET", "path": "/A", "id": "lit"},
    ]
    c = post(routes).json()["conflicts"][0]
    assert c["kind"] == "duplicate"
    assert c["witness_path"] == "/A"


def test_encoded_slash_no_conflict():
    routes = [
        {"method": "GET", "path": "/a%2Fb"},
        {"method": "GET", "path": "/a/b"},
    ]
    assert post(routes).json()["conflicts"] == []


def test_conflicts_sorted_by_priority_then_path():
    routes = [
        {"method": "GET", "path": "/z/**", "priority": 0},
        {"method": "GET", "path": "/z/x", "priority": 0},
        {"method": "GET", "path": "/a/**", "priority": 9},
        {"method": "GET", "path": "/a/x", "priority": 0},
    ]
    conflicts = post(routes).json()["conflicts"]
    assert conflicts[0]["winner_priority"] == 9
    assert conflicts[0]["winner_template"] == "/a/**"


def test_suggested_order_moves_more_specific_first():
    routes = [
        {"method": "GET", "path": "/**"},
        {"method": "GET", "path": "/users/{id}"},
        {"method": "GET", "path": "/users/me"},
    ]
    order = post(routes).json()["suggested_order"][0]["order"]
    templates = [e["template"] for e in order]
    assert templates == ["/users/me", "/users/{id}", "/**"]
    # move = 原位置 - 新位置：正数前移，负数后移
    assert [e["id"] for e in order] == ["route-2", "route-1", "route-0"]
    assert [e["move"] for e in order] == [2, 0, -2]


def test_default_id_uses_index():
    body = post([{"method": "GET", "path": "/a"}]).json()
    assert body["routes"][0]["id"] == "route-0"


def test_warnings_reported():
    body = post([{"method": "GET", "path": "/a/./b/"}]).json()
    route = body["routes"][0]
    assert route["normalized_template"] == "/a/b"
    assert any("dot segments" in w for w in route["warnings"])
    assert any("trailing slash" in w for w in route["warnings"])


def test_invalid_envelope_returns_422():
    response = client.post("/analyze", json={"routes": [{"method": "GET"}]})
    assert response.status_code == 422


def test_extra_fields_ignored():
    response = post([{"method": "GET", "path": "/x", "unknown_field": 123}])
    assert response.status_code == 200
