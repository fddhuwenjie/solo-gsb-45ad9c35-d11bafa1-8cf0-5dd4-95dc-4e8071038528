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


def migrate(client, routes, links):
    return client.post("/migrate-links", json={"routes": routes, "links": links})


ACTIVE_ROUTES = [
    {"method": "GET", "path": "/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/users/me", "priority": 10, "id": "me"},
    {"method": "GET", "path": "/assets/**", "id": "assets"},
]

# 与活动配置同 id、换了路径前缀的目标配置。
TARGET_ROUTES = [
    {"method": "GET", "path": "/v2/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/v2/users/me", "priority": 10, "id": "me"},
    {"method": "GET", "path": "/v2/assets/**", "id": "assets"},
]


@pytest.fixture()
def published(client):
    """发布一个活动版本（版本 1）并返回其路由。"""
    response = publish(client, ACTIVE_ROUTES, 0)
    assert response.json()["status"] == "published"
    return ACTIVE_ROUTES


def test_basic_migration_rewrites_prefix_and_params(client, published):
    links = [
        {"method": "GET", "path": "/users/42"},
        {"method": "GET", "path": "/users/me"},
        {"method": "GET", "path": "/assets/css/site.css"},
        {"method": "GET", "path": "/assets"},
    ]
    body = migrate(client, TARGET_ROUTES, links).json()
    assert body["status"] == "generated"
    assert body["version"] == 1
    assert body["parse_errors"] == [] and body["conflicts"] == []
    assert len(body["results"]) == 4

    user, me, asset, asset_root = body["results"]
    assert user["version"] == 1
    assert user["active"] == {
        "index": 0,
        "id": "user",
        "normalized_template": "/users/{id:int}",
    }
    assert user["target"] == {
        "index": 0,
        "id": "user",
        "normalized_template": "/v2/users/{id:int}",
    }
    assert user["canonical_path"] == "/v2/users/42"
    assert user["errors"] == []

    assert me["canonical_path"] == "/v2/users/me"
    # 末尾 ** 各段逐段搬入；零段时只剩新前缀
    assert asset["canonical_path"] == "/v2/assets/css/site.css"
    assert asset_root["canonical_path"] == "/v2/assets"


def test_results_follow_input_order_and_normalize_samples(client, published):
    links = [
        {"method": "get", "path": "/users/./42/"},
        {"method": "GET", "path": "/%75sers/me"},
    ]
    body = migrate(client, TARGET_ROUTES, links).json()
    first, second = body["results"]
    assert first["index"] == 0 and first["method"] == "GET"
    assert first["normalized_path"] == "/users/42"
    assert first["canonical_path"] == "/v2/users/42"
    assert second["normalized_path"] == "/users/me"
    assert second["canonical_path"] == "/v2/users/me"


def test_rejected_when_target_fails_publish_validation(client, published):
    routes = [
        {"method": "GET", "path": "/ok/{id:int}", "id": "a"},
        {"method": "GET", "path": "/bad/{n:float}"},
        {"method": "GET", "path": "/ok/{id:uuid}", "id": "b"},  # 与 a 重复导出路径
    ]
    body = migrate(client, routes, [{"method": "GET", "path": "/users/42"}]).json()
    assert body["status"] == "rejected"
    assert body["version"] == 1
    assert [e["index"] for e in body["parse_errors"]] == [1, 2]
    assert body["results"] == []

    # full_shadow 同样整批拒绝
    shadowed = [
        {"method": "GET", "path": "/files/{name}", "priority": 5, "id": "all"},
        {"method": "GET", "path": "/files/report.pdf", "id": "file"},
    ]
    body = migrate(client, shadowed, []).json()
    assert body["status"] == "rejected"
    assert body["conflicts"][0]["kind"] == "full_shadow"
    assert body["results"] == []


def test_no_active_match(client, published):
    links = [
        {"method": "POST", "path": "/users/42"},  # 活动侧没有 POST 路由
        {"method": "GET", "path": "/nowhere"},
    ]
    body = migrate(client, TARGET_ROUTES, links).json()
    assert body["status"] == "generated"
    for result in body["results"]:
        assert result["active"] is None
        assert result["target"] is None
        assert result["canonical_path"] is None
        assert [e["code"] for e in result["errors"]] == ["no_active_match"]


def test_missing_route_id(client):
    routes = [{"method": "GET", "path": "/users/{id:int}"}]  # 无显式 id
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/v2/users/{id:int}", "id": "user"}]
    body = migrate(client, target, [{"method": "GET", "path": "/users/42"}]).json()
    result = body["results"][0]
    assert result["active"]["id"] == "route-0"  # 生成的回退 id 不算显式 id
    assert result["target"] is None
    assert result["canonical_path"] is None
    assert [e["code"] for e in result["errors"]] == ["missing_route_id"]


def test_ambiguous_route_id_on_either_side(client):
    active = [
        {"method": "GET", "path": "/a/{x}", "id": "dup"},
        {"method": "GET", "path": "/b/{y}", "id": "dup"},
        {"method": "GET", "path": "/c/{z}", "id": "solo"},
    ]
    assert publish(client, active, 0).json()["status"] == "published"
    target = [
        {"method": "GET", "path": "/v2/a/{x}", "id": "dup"},
        {"method": "GET", "path": "/v2/c1/{z}", "id": "solo"},
        {"method": "GET", "path": "/v2/c2/{z}", "id": "solo"},
    ]
    links = [
        {"method": "GET", "path": "/a/1"},  # 活动侧 id 重复
        {"method": "GET", "path": "/c/1"},  # 目标侧 id 重复
    ]
    body = migrate(client, target, links).json()
    active_dup, target_dup = body["results"]
    assert [e["code"] for e in active_dup["errors"]] == ["ambiguous_route_id"]
    assert active_dup["canonical_path"] is None
    assert [e["code"] for e in target_dup["errors"]] == ["ambiguous_route_id"]
    assert target_dup["canonical_path"] is None


def test_target_missing(client, published):
    target = [
        {"method": "GET", "path": "/v2/users/{id:int}", "id": "user"},
        {"method": "POST", "path": "/v2/assets/**", "id": "assets"},  # 方法不同
    ]
    body = migrate(
        client, target, [{"method": "GET", "path": "/assets/logo.png"}]
    ).json()
    result = body["results"][0]
    assert result["active"]["id"] == "assets"
    assert result["target"] is None
    assert [e["code"] for e in result["errors"]] == ["target_missing"]


def test_parameter_mapping_must_be_complete(client, published):
    # 目标多出旧模板没有的参数；目标丢掉了旧模板的参数
    target = [
        {"method": "GET", "path": "/v2/users/{id:int}/{tab}", "id": "user"},
        {"method": "GET", "path": "/v2/me", "id": "me"},
        {"method": "GET", "path": "/v2/assets", "id": "assets"},
    ]
    links = [
        {"method": "GET", "path": "/users/42"},
        {"method": "GET", "path": "/assets/css/site.css"},
    ]
    body = migrate(client, target, links).json()
    missing, stray_tail = body["results"]
    assert [e["code"] for e in missing["errors"]] == ["parameter_unmapped"]
    assert missing["canonical_path"] is None
    # 旧 ** 尾段无处安放（目标模板没有末尾 **）
    assert [e["code"] for e in stray_tail["errors"]] == ["parameter_unmapped"]
    assert stray_tail["canonical_path"] is None


def test_unused_legacy_parameter_reported(client):
    active = [
        {"method": "GET", "path": "/users/{id:int}/posts/{slug}", "id": "post"}
    ]
    assert publish(client, active, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/v2/posts/{slug}", "id": "post"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/users/42/posts/hello"}]
    ).json()
    result = body["results"][0]
    assert [e["code"] for e in result["errors"]] == ["parameter_unmapped"]
    assert result["canonical_path"] is None


def test_constraint_violation_against_target(client):
    active = [{"method": "GET", "path": "/files/{name}", "id": "file"}]
    assert publish(client, active, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/v2/files/{name:int}", "id": "file"}]
    links = [
        {"method": "GET", "path": "/files/abc"},  # 不满足目标 int 约束
        {"method": "GET", "path": "/files/42"},
    ]
    body = migrate(client, target, links).json()
    bad, good = body["results"]
    assert [e["code"] for e in bad["errors"]] == ["constraint_violation"]
    assert bad["canonical_path"] is None
    assert good["canonical_path"] == "/v2/files/42"


def test_generated_path_must_win_the_full_target_set(client, published):
    # 目标集中另有一条更优先的静态路由会截获生成路径（仅部分相交，校验放行）
    target = [
        {"method": "GET", "path": "/v2/users/{id:int}", "id": "user"},
        {"method": "GET", "path": "/v2/users/42", "priority": 10, "id": "answer"},
        {"method": "GET", "path": "/v2/users/me", "priority": 10, "id": "me"},
        {"method": "GET", "path": "/v2/assets/**", "id": "assets"},
    ]
    links = [
        {"method": "GET", "path": "/users/42"},  # 生成 /v2/users/42 被 answer 截获
        {"method": "GET", "path": "/users/7"},   # 胜方仍是对应目标路由
    ]
    body = migrate(client, target, links).json()
    intercepted, fine = body["results"]
    assert intercepted["target"]["id"] == "user"
    assert [e["code"] for e in intercepted["errors"]] == ["intercepted"]
    assert intercepted["canonical_path"] is None
    assert fine["canonical_path"] == "/v2/users/7"


def test_percent_2f_stays_single_segment_without_reencoding(client):
    active = [{"method": "GET", "path": "/files/{name}", "id": "file"}]
    assert publish(client, active, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/v2/files/{name}", "id": "file"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/files/a%2Fb"}]
    ).json()
    result = body["results"][0]
    assert result["normalized_path"] == "/files/a%2Fb"
    # %2F 不拆段、不重复编码：既不是 /v2/files/a/b 也不是 %252F
    assert result["canonical_path"] == "/v2/files/a%2Fb"


def test_invalid_samples_do_not_affect_others(client, published):
    links = [
        {"method": "GET", "path": "/users/42"},
        {"method": "FROG", "path": "/users/42"},
        {"method": "GET", "path": "no-slash"},
        {"method": "GET", "path": "/bad/%zz"},
    ]
    body = migrate(client, TARGET_ROUTES, links).json()
    assert body["status"] == "generated"
    ok, bad_method, relative, bad_escape = body["results"]
    assert ok["canonical_path"] == "/v2/users/42"
    assert [e["code"] for e in bad_method["errors"]] == ["unsupported_method"]
    assert [e["code"] for e in relative["errors"]] == ["invalid_path"]
    assert relative["normalized_path"] is None
    assert [e["code"] for e in bad_escape["errors"]] == ["invalid_path"]
    for result in (bad_method, relative, bad_escape):
        assert result["canonical_path"] is None


def test_root_link_migrates_through_catchall(client):
    active = [{"method": "GET", "path": "/**", "id": "catchall"}]
    assert publish(client, active, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/v2/**", "id": "catchall"}]
    body = migrate(
        client,
        target,
        [{"method": "GET", "path": "/"}, {"method": "GET", "path": "/a//b"}],
    ).json()
    root, empty_seg = body["results"]
    assert root["canonical_path"] == "/v2"
    # 内部空段由 ** 原样吸收
    assert empty_seg["canonical_path"] == "/v2/a//b"


def test_migration_is_read_only_and_pins_version(client, published):
    body = migrate(
        client, TARGET_ROUTES, [{"method": "GET", "path": "/users/42"}]
    ).json()
    assert body["status"] == "generated"
    # 迁移不修改活动配置
    active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == ["user", "me", "assets"]
    # 过期预期版本仍按原规则拒绝
    stale = publish(client, TARGET_ROUTES, 0).json()
    assert stale["status"] == "version_conflict"
    assert client.get("/active").json()["version"] == 1


def test_empty_links_and_unpublished_active(client):
    # 未发布时：版本 0，活动侧一律无命中
    body = migrate(client, TARGET_ROUTES, [{"method": "GET", "path": "/users/42"}]).json()
    assert body["status"] == "generated"
    assert body["version"] == 0
    result = body["results"][0]
    assert result["version"] == 0
    assert result["active"] is None
    assert [e["code"] for e in result["errors"]] == ["no_active_match"]

    body = migrate(client, TARGET_ROUTES, []).json()
    assert body["status"] == "generated"
    assert body["results"] == []


def test_missing_envelope_fields_return_422(client):
    assert client.post("/migrate-links", json={"routes": []}).status_code == 422
    assert client.post("/migrate-links", json={"links": []}).status_code == 422
    response = client.post(
        "/migrate-links", json={"routes": [], "links": [{"method": "GET"}]}
    )
    assert response.status_code == 422
