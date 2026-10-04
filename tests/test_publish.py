import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
from app.models import RouteIn
from app.publish import PublishStore


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立的状态文件，互不影响。"""
    store = PublishStore(tmp_path / "active.json")
    monkeypatch.setattr(main, "store", store)
    return TestClient(app)


def publish(client, routes, expected_version):
    return client.post(
        "/publish", json={"routes": routes, "expected_version": expected_version}
    )


VALID_ROUTES = [
    {"method": "GET", "path": "/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/users/me", "priority": 10, "id": "me"},
]


def test_initial_active_version_is_empty(client):
    body = client.get("/active").json()
    assert body["version"] == 0
    assert body["routes"] == []
    assert body["audit"]["summary"]["total"] == 0
    assert body["openapi_paths"] == {}


def test_publish_success_increments_version(client):
    response = publish(client, VALID_ROUTES, expected_version=0)
    assert response.status_code == 200
    assert response.json() == {
        "status": "published",
        "version": 1,
        "parse_errors": [],
        "conflicts": [],
    }

    body = client.get("/active").json()
    assert body["version"] == 1
    assert [r["id"] for r in body["routes"]] == ["user", "me"]
    # 审计与导出针对同一份已发布路由
    assert body["audit"]["summary"]["total"] == 2
    assert body["audit"]["summary"]["valid"] == 2
    assert set(body["openapi_paths"]) == {"/users/me", "/users/{id}"}
    assert body["openapi_paths"]["/users/{id}"]["get"]["operationId"] == "user"


def test_publish_again_requires_new_expected_version(client):
    assert publish(client, VALID_ROUTES, 0).json()["status"] == "published"
    second = publish(client, VALID_ROUTES, 1).json()
    assert second["status"] == "published"
    assert second["version"] == 2
    assert client.get("/active").json()["version"] == 2


def test_publish_with_parse_error_rejected(client):
    routes = [
        {"method": "GET", "path": "/ok"},
        {"method": "GET", "path": "/bad/{n:float}"},
        {"method": "FROG", "path": "/x"},
    ]
    body = publish(client, routes, 0).json()
    assert body["status"] == "rejected"
    assert body["version"] == 0
    assert [e["index"] for e in body["parse_errors"]] == [1, 2]
    # 活动版本保持初始状态
    active = client.get("/active").json()
    assert active["version"] == 0
    assert active["routes"] == []


def test_publish_with_duplicate_export_path_rejected(client):
    # int 与 uuid 约束不同，但导出路径同为 /x/{id}
    routes = [
        {"method": "GET", "path": "/x/{id:int}"},
        {"method": "GET", "path": "/x/{id:uuid}"},
    ]
    body = publish(client, routes, 0).json()
    assert body["status"] == "rejected"
    assert body["version"] == 0
    assert [e["index"] for e in body["parse_errors"]] == [1]
    assert "duplicate route" in body["parse_errors"][0]["error"]


def test_publish_with_full_shadow_rejected(client):
    routes = [
        {"method": "GET", "path": "/files/{name}", "priority": 5, "id": "all"},
        {"method": "GET", "path": "/files/report.pdf", "priority": 0, "id": "file"},
    ]
    body = publish(client, routes, 0).json()
    assert body["status"] == "rejected"
    assert body["version"] == 0
    assert body["parse_errors"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "full_shadow"
    # 返回原始下标定位冲突双方
    assert conflict["winner_index"] == 0
    assert conflict["loser_index"] == 1
    assert client.get("/active").json()["version"] == 0


def test_partial_overlap_does_not_block_publish(client):
    routes = [
        {"method": "GET", "path": "/a/{x}/c"},
        {"method": "GET", "path": "/a/b/{y}"},
    ]
    body = publish(client, routes, 0).json()
    assert body["status"] == "published"
    active = client.get("/active").json()
    assert active["audit"]["conflicts"][0]["kind"] == "partial_overlap"


def test_stale_expected_version_returns_current_and_keeps_content(client):
    assert publish(client, VALID_ROUTES, 0).json()["status"] == "published"

    other_routes = [{"method": "GET", "path": "/other"}]
    body = publish(client, other_routes, 0).json()  # 当前版本已是 1
    assert body["status"] == "version_conflict"
    assert body["version"] == 1

    # 已发布内容未被覆盖
    active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == ["user", "me"]


def test_active_snapshot_is_consistent(client):
    routes = [
        {"method": "GET", "path": "/api/**", "id": "catchall"},
        {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    ]
    assert publish(client, routes, 0).json()["status"] == "published"
    # 一次失败的发布不得影响已发布快照
    rejected = publish(client, [{"method": "GET", "path": "no-slash"}], 1).json()
    assert rejected["status"] == "rejected"

    active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == ["catchall", "user"]
    assert active["audit"]["summary"]["total"] == 2
    assert set(active["openapi_paths"]) == {"/api/{wildcard}", "/api/users/{id}"}


def test_missing_expected_version_returns_422(client):
    response = client.post("/publish", json={"routes": VALID_ROUTES})
    assert response.status_code == 422


def test_concurrent_publish_same_expected_version_only_one_wins(tmp_path):
    store = PublishStore(tmp_path / "active.json")
    routes = [RouteIn(method="GET", path="/a")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.publish(routes, 0), range(8)))

    statuses = [r.status for r in results]
    assert statuses.count("published") == 1
    assert statuses.count("version_conflict") == 7
    # 每个失败响应都返回当时的当前版本
    for r in results:
        if r.status == "version_conflict":
            assert r.version == 1
    assert store.get().version == 1


def test_state_survives_restart(tmp_path):
    path = tmp_path / "active.json"
    first = PublishStore(path)
    result = first.publish([RouteIn(method="GET", path="/a", id="keep")], 0)
    assert result.status == "published"

    # 模拟容器重启：新实例从同一状态文件恢复
    restored = PublishStore(path)
    snapshot = restored.get()
    assert snapshot.version == 1
    assert snapshot.routes[0]["id"] == "keep"
    assert snapshot.audit["summary"]["total"] == 1
    assert set(snapshot.openapi_paths) == {"/a"}

    # 恢复后版本号继续递增，不会与已发布版本冲突
    again = restored.publish([RouteIn(method="GET", path="/b")], 1)
    assert again.status == "published"
    assert again.version == 2


def test_state_file_is_valid_json_with_full_snapshot(tmp_path):
    path = tmp_path / "active.json"
    store = PublishStore(path)
    store.publish([RouteIn(method="GET", path="/a")], 0)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["routes"][0]["path"] == "/a"
    assert "conflicts" in data["audit"]
    assert "/a" in data["openapi_paths"]
