import pytest
from fastapi.testclient import TestClient

from app import main
from app.main import app
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


def precheck(client, routes, samples):
    return client.post("/precheck", json={"routes": routes, "samples": samples})


ACTIVE_ROUTES = [
    {"method": "GET", "path": "/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/users/me", "priority": 10, "id": "me"},
    {"method": "GET", "path": "/assets/**", "priority": 0, "id": "assets"},
]


@pytest.fixture()
def published(client):
    """发布一个活动版本（版本 1）并返回其路由。"""
    response = publish(client, ACTIVE_ROUTES, 0)
    assert response.json()["status"] == "published"
    return ACTIVE_ROUTES


def test_rejected_when_candidate_has_parse_error(client, published):
    routes = [
        {"method": "GET", "path": "/ok"},
        {"method": "GET", "path": "/bad/{n:float}"},
        {"method": "FROG", "path": "/x"},
    ]
    body = precheck(client, routes, [{"method": "GET", "path": "/ok"}]).json()
    assert body["status"] == "rejected"
    assert body["version"] == 1
    assert [e["index"] for e in body["parse_errors"]] == [1, 2]
    # 整批不做归属比较
    assert body["results"] == []
    # 活动配置未被预检修改
    assert client.get("/active").json()["version"] == 1


def test_rejected_when_candidate_has_duplicate_export_path(client, published):
    routes = [
        {"method": "GET", "path": "/x/{id:int}"},
        {"method": "GET", "path": "/x/{id:uuid}"},
    ]
    body = precheck(client, routes, [{"method": "GET", "path": "/x/1"}]).json()
    assert body["status"] == "rejected"
    assert [e["index"] for e in body["parse_errors"]] == [1]
    assert "duplicate route" in body["parse_errors"][0]["error"]
    assert body["results"] == []


def test_rejected_when_candidate_has_full_shadow(client, published):
    routes = [
        {"method": "GET", "path": "/files/{name}", "priority": 5, "id": "all"},
        {"method": "GET", "path": "/files/report.pdf", "priority": 0, "id": "file"},
    ]
    body = precheck(client, routes, [{"method": "GET", "path": "/files/a"}]).json()
    assert body["status"] == "rejected"
    assert body["parse_errors"] == []
    assert len(body["conflicts"]) == 1
    conflict = body["conflicts"][0]
    assert conflict["kind"] == "full_shadow"
    assert conflict["winner_index"] == 0
    assert conflict["loser_index"] == 1
    assert body["results"] == []


def test_same_route_at_new_index_is_not_changed(client, published):
    # 候选只是调整了注册顺序：归属身份不变，不得因下标移动误报改投
    candidate = [published[1], published[2], published[0]]
    samples = [
        {"method": "GET", "path": "/users/42"},
        {"method": "GET", "path": "/users/me"},
        {"method": "GET", "path": "/assets/img/logo.png"},
    ]
    body = precheck(client, candidate, samples).json()
    assert body["status"] == "compared"
    assert body["version"] == 1
    assert len(body["results"]) == 3

    first, second, third = body["results"]
    assert first["normalized_path"] == "/users/42"
    assert first["active"] == {
        "index": 0,
        "id": "user",
        "normalized_template": "/users/{id:int}",
    }
    # 候选侧同一条路由移动到了下标 2
    assert first["candidate"] == {
        "index": 2,
        "id": "user",
        "normalized_template": "/users/{id:int}",
    }
    assert first["changed"] is False

    assert second["active"]["index"] == 1
    assert second["candidate"]["index"] == 0
    assert second["changed"] is False

    assert third["active"]["id"] == "assets"
    assert third["candidate"]["index"] == 1
    assert third["changed"] is False


def test_new_more_specific_route_causes_change(client, published):
    candidate = published + [
        {"method": "GET", "path": "/assets/logo.png", "priority": 10, "id": "logo"}
    ]
    samples = [
        {"method": "GET", "path": "/assets/logo.png"},
        {"method": "GET", "path": "/assets/other.png"},
    ]
    body = precheck(client, candidate, samples).json()
    assert body["status"] == "compared"

    moved, kept = body["results"]
    assert moved["active"] == {
        "index": 2,
        "id": "assets",
        "normalized_template": "/assets/**",
    }
    assert moved["candidate"] == {
        "index": 3,
        "id": "logo",
        "normalized_template": "/assets/logo.png",
    }
    assert moved["changed"] is True

    assert kept["candidate"]["id"] == "assets"
    assert kept["changed"] is False


def test_no_hit_recorded_as_null(client, published):
    candidate = published + [
        {"method": "POST", "path": "/users/{id:int}", "id": "create-user"}
    ]
    samples = [
        {"method": "POST", "path": "/users/42"},  # 仅候选侧有 POST 路由
        {"method": "DELETE", "path": "/users/42"},  # 两侧都无命中
    ]
    body = precheck(client, candidate, samples).json()
    assert body["status"] == "compared"

    gained, none = body["results"]
    assert gained["active"] is None
    assert gained["candidate"] == {
        "index": 3,
        "id": "create-user",
        "normalized_template": "/users/{id:int}",
    }
    assert gained["changed"] is True

    assert none["active"] is None
    assert none["candidate"] is None
    assert none["changed"] is False


def test_invalid_samples_reported_per_item_others_continue(client, published):
    samples = [
        {"method": "GET", "path": "/users/42"},
        {"method": "FROG", "path": "/users/42"},
        {"method": "GET", "path": "no-slash"},
        {"method": "GET", "path": "/bad/%zz"},
    ]
    body = precheck(client, published, samples).json()
    assert body["status"] == "compared"
    results = body["results"]
    assert len(results) == 4

    ok, bad_method, relative, bad_escape = results
    assert ok["errors"] == []
    assert ok["active"]["id"] == "user"

    assert bad_method["errors"]
    assert "unsupported HTTP method" in bad_method["errors"][0]
    assert bad_method["active"] is None
    assert bad_method["candidate"] is None
    assert bad_method["changed"] is None

    assert relative["errors"]
    assert "absolute path" in relative["errors"][0]
    assert relative["normalized_path"] is None
    assert relative["changed"] is None

    assert bad_escape["errors"]
    assert "percent-escape" in bad_escape["errors"][0]
    assert bad_escape["normalized_path"] is None


def test_percent_encoded_slash_stays_single_segment(client):
    routes = [{"method": "GET", "path": "/files/{name}", "id": "file"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    samples = [
        {"method": "GET", "path": "/files/a%2Fb"},  # 编码斜杠仍是单段
        {"method": "GET", "path": "/files/a%2fb"},  # 十六进制规范为大写
        {"method": "GET", "path": "/files/a/b"},  # 真分隔符：两段，不命中
    ]
    body = precheck(client, routes, samples).json()
    encoded, lower, split = body["results"]

    assert encoded["normalized_path"] == "/files/a%2Fb"
    assert encoded["active"]["id"] == "file"
    assert encoded["candidate"]["id"] == "file"
    assert encoded["changed"] is False

    assert lower["normalized_path"] == "/files/a%2Fb"
    assert lower["active"]["id"] == "file"

    assert split["normalized_path"] == "/files/a/b"
    assert split["active"] is None
    assert split["candidate"] is None
    assert split["changed"] is False


def test_sample_path_is_normalized_before_matching(client, published):
    samples = [
        {"method": "get", "path": "/users/./42/"},
        {"method": "GET", "path": "/%75sers/42"},
    ]
    body = precheck(client, published, samples).json()
    dotted, encoded = body["results"]
    assert dotted["method"] == "GET"
    assert dotted["normalized_path"] == "/users/42"
    assert dotted["active"]["id"] == "user"
    assert encoded["normalized_path"] == "/users/42"
    assert encoded["active"]["id"] == "user"


def test_priority_then_registration_order_pick_unique_hit(client):
    # 活动为空（版本 0），只观察候选侧的决胜规则
    tied = [
        {"method": "GET", "path": "/x/{a}", "id": "first"},
        {"method": "GET", "path": "/x/{b}", "id": "second"},
    ]
    body = precheck(client, tied, [{"method": "GET", "path": "/x/1"}]).json()
    assert body["status"] == "compared"
    hit = body["results"][0]["candidate"]
    assert hit["id"] == "first"  # 同级注册更早者胜

    prioritized = [
        {"method": "GET", "path": "/x/{a}", "priority": 0, "id": "first"},
        {"method": "GET", "path": "/x/{b}", "priority": 5, "id": "second"},
    ]
    body = precheck(client, prioritized, [{"method": "GET", "path": "/x/1"}]).json()
    hit = body["results"][0]["candidate"]
    assert hit["id"] == "second"  # 优先级高者胜


def test_wildcard_matches_root_and_interior_empty_segments(client):
    routes = [{"method": "GET", "path": "/**", "id": "catchall"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    samples = [
        {"method": "GET", "path": "/"},
        {"method": "GET", "path": "/a//b"},
    ]
    body = precheck(client, routes, samples).json()
    root, empty_seg = body["results"]
    assert root["normalized_path"] == "/"
    assert root["active"]["id"] == "catchall"
    assert root["changed"] is False
    # 内部空段保留，只有 ** 能吸收
    assert empty_seg["normalized_path"] == "/a//b"
    assert empty_seg["active"]["id"] == "catchall"


def test_version_pinned_and_publish_rules_unchanged(client):
    # 未发布时预检：版本 0，活动侧一律无命中
    body = precheck(
        client, ACTIVE_ROUTES, [{"method": "GET", "path": "/users/42"}]
    ).json()
    assert body["version"] == 0
    assert body["results"][0]["active"] is None
    assert body["results"][0]["candidate"]["id"] == "user"
    assert body["results"][0]["changed"] is True

    assert publish(client, ACTIVE_ROUTES, 0).json()["status"] == "published"
    body = precheck(
        client, ACTIVE_ROUTES, [{"method": "GET", "path": "/users/42"}]
    ).json()
    assert body["version"] == 1
    assert body["results"][0]["changed"] is False

    # 预检不修改活动配置
    active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == ["user", "me", "assets"]

    # 过期预期版本仍按原规则拒绝
    stale = publish(client, [{"method": "GET", "path": "/other"}], 0).json()
    assert stale["status"] == "version_conflict"
    assert stale["version"] == 1
    assert client.get("/active").json()["version"] == 1


def test_empty_samples_and_empty_candidate(client, published):
    body = precheck(client, [], []).json()
    assert body["status"] == "compared"
    assert body["version"] == 1
    assert body["results"] == []

    body = precheck(client, [], [{"method": "GET", "path": "/users/42"}]).json()
    result = body["results"][0]
    assert result["active"]["id"] == "user"
    assert result["candidate"] is None
    assert result["changed"] is True


def test_missing_envelope_fields_return_422(client):
    assert client.post("/precheck", json={"routes": []}).status_code == 422
    assert client.post("/precheck", json={"samples": []}).status_code == 422
    response = client.post(
        "/precheck", json={"routes": [], "samples": [{"method": "GET"}]}
    )
    assert response.status_code == 422
