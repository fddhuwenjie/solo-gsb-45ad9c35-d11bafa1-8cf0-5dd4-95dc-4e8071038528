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


# 活动版本 1：旧路由（与目标路由用相同显式 id 对应）。
ACTIVE_ROUTES = [
    {"method": "GET", "path": "/v1/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/v1/assets/**", "priority": 0, "id": "assets"},
    {"method": "GET", "path": "/v1/items/{slug:slug}", "id": "item"},
]

# 目标版本：路径搬家、参数改名映射不到、新增更具体的静态路由。
TARGET_ROUTES = [
    {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    {"method": "GET", "path": "/api/assets/**", "priority": 0, "id": "assets"},
    {"method": "GET", "path": "/api/items/{key:slug}", "id": "item"},
]


@pytest.fixture()
def published(client):
    assert publish(client, ACTIVE_ROUTES, 0).json()["status"] == "published"
    return ACTIVE_ROUTES


def test_basic_migration_parameter_and_wildcard(client, published):
    body = migrate(
        client,
        TARGET_ROUTES,
        [
            {"method": "GET", "path": "/v1/users/42"},
            {"method": "GET", "path": "/v1/assets/img/logo.png"},
            {"method": "GET", "path": "/v1/assets"},
        ],
    ).json()
    assert body["status"] == "migrated"
    assert body["version"] == 1
    assert body["parse_errors"] == []
    assert body["conflicts"] == []

    user, assets, assets_root = body["results"]

    assert user["index"] == 0
    assert user["active_version"] == 1
    assert user["active"] == {
        "index": 0, "id": "user", "normalized_template": "/v1/users/{id:int}",
    }
    assert user["target"] == {
        "index": 0, "id": "user", "normalized_template": "/api/users/{id:int}",
    }
    assert user["canonical_path"] == "/api/users/42"
    assert user["errors"] == []

    assert assets["canonical_path"] == "/api/assets/img/logo.png"
    assert assets["target"]["id"] == "assets"
    # 旧 ** 匹配零段时，目标也渲染为无前缀尾段。
    assert assets_root["canonical_path"] == "/api/assets"


def test_renamed_parameter_cannot_map(client, published):
    # 旧参数名 slug 与目标参数名 key 不一致 -> 无法完整对应。
    body = migrate(
        client, TARGET_ROUTES, [{"method": "GET", "path": "/v1/items/hello-world"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] is None
    assert result["target"]["id"] == "item"
    codes = {e["code"] for e in result["errors"]}
    assert codes == {"parameter_mismatch"}
    assert result["errors"][0]["field"] == "key"


def test_target_candidate_rejected_batch_aborts(client, published):
    # 目标含 full_shadow：/files/{name} 完全遮蔽静态路由 -> 整批拒绝。
    routes = [
        {"method": "GET", "path": "/files/{name}", "priority": 5, "id": "all"},
        {"method": "GET", "path": "/files/report.pdf", "id": "report"},
    ]
    body = migrate(
        client, routes, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    assert body["status"] == "rejected"
    assert body["version"] == 1
    assert body["results"] == []
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["kind"] == "full_shadow"
    # 活动配置不变。
    assert client.get("/active").json()["version"] == 1


def test_target_parse_error_rejects_batch(client, published):
    routes = [
        {"method": "GET", "path": "/ok/{id:int}", "id": "user"},
        {"method": "GET", "path": "/bad/{n:float}", "id": "bad"},
    ]
    body = migrate(
        client, routes, [{"method": "GET", "path": "/v1/users/1"}]
    ).json()
    assert body["status"] == "rejected"
    assert [e["index"] for e in body["parse_errors"]] == [1]
    assert body["results"] == []


def test_no_active_match(client, published):
    body = migrate(
        client,
        TARGET_ROUTES,
        [
            {"method": "GET", "path": "/nowhere/xxx"},
            {"method": "POST", "path": "/v1/users/42"},
        ],
    ).json()
    miss, wrong_method = body["results"]
    assert miss["active"] is None
    assert miss["target"] is None
    assert miss["canonical_path"] is None
    assert [e["code"] for e in miss["errors"]] == ["no_active_match"]

    assert wrong_method["active"] is None
    assert [e["code"] for e in wrong_method["errors"]] == ["no_active_match"]


def test_active_route_without_explicit_id(client):
    routes = [
        {"method": "GET", "path": "/v1/users/{id:int}", "priority": 5},
    ]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [
        {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    ]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    result = body["results"][0]
    assert result["active"]["id"] == "route-0"  # 生成 id 仅用于回显
    assert result["target"] is None
    assert [e["code"] for e in result["errors"]] == ["active_id_missing"]


def test_empty_explicit_id_counts_as_missing(client):
    routes = [{"method": "GET", "path": "/v1/users/{id:int}", "id": ""}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/api/users/{id:int}", "id": "user"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    assert body["results"][0]["errors"][0]["code"] == "active_id_missing"


def test_ambiguous_id_in_active_configuration(client):
    # 活动侧同方法两条路由共用同一显式 id（无 full_shadow，可发布）。
    routes = [
        {"method": "GET", "path": "/v1/users/{id:int}", "id": "shared"},
        {"method": "GET", "path": "/v1/people/{id:int}", "id": "shared"},
    ]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [
        {"method": "GET", "path": "/api/users/{id:int}", "id": "shared"},
    ]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] is None
    assert result["target"] is None
    assert [e["code"] for e in result["errors"]] == ["ambiguous_id"]


def test_ambiguous_id_in_target_configuration(client, published):
    # 目标侧同方法两条同 id 路由：模板不同但都与其它路由不相交，可通过发布校验。
    target = [
        {"method": "GET", "path": "/api/users/{id:int}", "id": "user"},
        {"method": "GET", "path": "/api/people/{id:int}", "id": "user"},
    ]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    assert body["status"] == "migrated"
    result = body["results"][0]
    assert [e["code"] for e in result["errors"]] == ["ambiguous_id"]
    assert result["canonical_path"] is None


def test_target_missing_id(client, published):
    target = [
        {"method": "GET", "path": "/api/assets/**", "id": "assets"},
    ]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    result = body["results"][0]
    assert result["active"]["id"] == "user"
    assert result["target"] is None
    assert [e["code"] for e in result["errors"]] == ["target_missing"]


def test_constraint_violated(client):
    # 旧通用参数可以取 'abc'，目标 int 参数不接受（同名映射才轮到约束校验）。
    routes = [{"method": "GET", "path": "/v1/users/{id}", "id": "user"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/api/users/{id:int}", "id": "user"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/abc"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] is None
    assert [e["code"] for e in result["errors"]] == ["constraint_violated"]
    assert result["errors"][0]["field"] == "id"


def test_slug_value_fails_uuid_target_constraint(client):
    routes = [{"method": "GET", "path": "/v1/users/{id:slug}", "id": "user"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/api/users/{id:uuid}", "id": "user"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/abc"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] is None
    assert [e["code"] for e in result["errors"]] == ["constraint_violated"]
    assert result["errors"][0]["field"] == "id"


def test_extra_old_wildcard_segments_target_has_no_wildcard(client):
    # 旧 ** 的段只能进目标 **；目标无 ** 时，任何尾部段都无处安放。
    routes = [{"method": "GET", "path": "/v1/files/**", "id": "files"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/files/{name}", "id": "files"}]

    # 即便旧 ** 只吸收一段，也不会顶替目标命名参数 name。
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/files/a"}]
    ).json()
    one = body["results"][0]
    assert one["canonical_path"] is None
    codes = {e["code"] for e in one["errors"]}
    assert "parameter_mismatch" in codes
    assert any(e["field"] == "name" for e in one["errors"])
    assert any(e["field"] == "wildcard" for e in one["errors"])

    # 旧 ** 吸收多段：同样无法迁移。
    body2 = migrate(
        client, target, [{"method": "GET", "path": "/v1/files/a/b/c"}]
    ).json()
    result2 = body2["results"][0]
    assert result2["canonical_path"] is None
    assert any(
        e["code"] == "parameter_mismatch" and e["field"] == "wildcard"
        for e in result2["errors"]
    )

    # 旧 ** 匹配零段、目标也无对应参数的诉求仍在（name 缺失），故失败；
    # 而目标为静态模板时旧 ** 匹配零段可以成功（见 test_basic_migration）。
    target_static = [{"method": "GET", "path": "/files", "id": "files"}]
    body3 = migrate(
        client, target_static, [{"method": "GET", "path": "/v1/files"}]
    ).json()
    assert body3["results"][0]["canonical_path"] == "/files"


def test_rendered_path_intercepted_by_another_target_route(client, published):
    # 目标侧新增更高优先级的静态路由，会截获生成出的 canonical path。
    target = TARGET_ROUTES + [
        {"method": "GET", "path": "/api/assets/img/logo.png",
         "priority": 10, "id": "logo"},
    ]
    body = migrate(
        client,
        target,
        [
            {"method": "GET", "path": "/v1/assets/img/logo.png"},
            {"method": "GET", "path": "/v1/assets/css/site.css"},
        ],
    ).json()
    assert body["status"] == "migrated"
    intercepted, free = body["results"]
    assert intercepted["target"]["id"] == "assets"
    assert intercepted["canonical_path"] is None
    assert [e["code"] for e in intercepted["errors"]] == ["target_not_winner"]

    # 未被截获的路径照常迁移。
    assert free["canonical_path"] == "/api/assets/css/site.css"
    assert free["errors"] == []


def test_rendered_path_intercepted_by_overlapping_route(client):
    # 目标侧两条同方法路由仅 partial_overlap（各有独占路径，能通过发布校验）：
    #   /a/{id}/**（generic，低优先级）与 /a/b/c（other，高优先级静态三段）。
    # 旧路由 /v1/a/{id}/** 的链接 /v1/a/b/c 渲染到 generic 得 /a/b/c，
    # 恰好落在高优先级 other 上而被截获。
    routes = [{"method": "GET", "path": "/v1/a/{id}/**", "id": "generic"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [
        {"method": "GET", "path": "/a/{id}/**", "priority": 0, "id": "generic"},
        {"method": "GET", "path": "/a/b/c", "priority": 9, "id": "other"},
    ]
    audit = client.post("/analyze", json={"routes": target}).json()
    assert [c["kind"] for c in audit["conflicts"]] == ["partial_overlap"]

    # 旧链接 /v1/a/b/c：generic 渲染 /a/b/c，被高优先级静态 other 截获。
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/a/b/c"}]
    ).json()
    assert body["status"] == "migrated"
    result = body["results"][0]
    assert result["target"]["id"] == "generic"
    assert result["canonical_path"] is None
    assert [e["code"] for e in result["errors"]] == ["target_not_winner"]

    # 旧链接 /v1/a/x/c：渲染 /a/x/c，不等于静态 /a/b/c -> 仍归 generic。
    body2 = migrate(
        client, target, [{"method": "GET", "path": "/v1/a/x/c"}]
    ).json()
    result2 = body2["results"][0]
    assert result2["canonical_path"] == "/a/x/c"
    assert result2["errors"] == []


def test_percent_encoded_slash_is_single_segment(client):
    routes = [{"method": "GET", "path": "/v1/files/{name}", "id": "file"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/files/{name}", "id": "file"}]
    body = migrate(
        client,
        target,
        [
            {"method": "GET", "path": "/v1/files/a%2Fb"},
            {"method": "GET", "path": "/v1/files/a%2fb"},
        ],
    ).json()
    upper, lower = body["results"]
    # %2F 不拆段、不重复编码，大小写统一为大写。
    assert upper["canonical_path"] == "/files/a%2Fb"
    assert upper["normalized_path"] == "/v1/files/a%2Fb"
    assert lower["canonical_path"] == "/files/a%2Fb"


def test_percent_encoding_not_double_encoded(client):
    # 旧值本身携带 % 字面量：%252F 必须保持单段且不被二次编码成 %25252F。
    routes = [{"method": "GET", "path": "/v1/files/{name}", "id": "file"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/files/{name}", "id": "file"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/files/%252F"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] == "/files/%252F"


def test_non_ascii_segment_round_trips(client):
    routes = [{"method": "GET", "path": "/v1/assets/**", "id": "assets"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/assets/**", "id": "assets"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/assets/caf%C3%A9.png"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] == "/assets/caf%C3%A9.png"


def test_wildcard_into_parameter_template_keeps_segments(client):
    # 旧通配把固定段数变长；目标同 id 仍是通配，逐段原样迁移。
    routes = [{"method": "GET", "path": "/old/**", "id": "root"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/new/**", "id": "root"}]
    body = migrate(
        client,
        target,
        [
            {"method": "GET", "path": "/old/a/b/c"},
            {"method": "GET", "path": "/old"},
            {"method": "GET", "path": "/old/a//b"},
        ],
    ).json()
    deep, root_path, empty_seg = body["results"]
    assert deep["canonical_path"] == "/new/a/b/c"
    assert root_path["canonical_path"] == "/new"
    # 内部空段可被 ** 匹配到，但无法渲染成 canonical 段。
    assert empty_seg["canonical_path"] is None
    assert empty_seg["errors"][0]["code"] == "constraint_violated"
    assert empty_seg["errors"][0]["field"] == "wildcard[1]"


def test_invalid_links_do_not_affect_others(client, published):
    body = migrate(
        client,
        TARGET_ROUTES,
        [
            {"method": "GET", "path": "/v1/users/42"},
            {"method": "FROG", "path": "/v1/users/42"},
            {"method": "GET", "path": "relative/path"},
            {"method": "GET", "path": "/v1/users/%zz"},
        ],
    ).json()
    assert body["status"] == "migrated"
    results = body["results"]
    assert len(results) == 4

    assert results[0]["canonical_path"] == "/api/users/42"

    assert results[1]["errors"][0]["code"] == "invalid_method"
    assert results[1]["active"] is None
    assert results[1]["canonical_path"] is None

    assert results[2]["errors"][0]["code"] == "invalid_path"
    assert results[2]["normalized_path"] is None

    assert results[3]["errors"][0]["code"] == "invalid_path"
    assert "percent-escape" in results[3]["errors"][0]["message"]
    #其它项不受影响
    assert results[0]["errors"] == []


def test_same_id_on_different_methods_are_independent(client):
    routes = [
        {"method": "GET", "path": "/v1/x", "id": "same"},
        {"method": "POST", "path": "/v1/x", "id": "same"},
    ]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [
        {"method": "GET", "path": "/x", "id": "same"},
        {"method": "POST", "path": "/x", "id": "same"},
    ]
    body = migrate(
        client,
        target,
        [
            {"method": "GET", "path": "/v1/x"},
            {"method": "POST", "path": "/v1/x"},
        ],
    ).json()
    get_result, post_result = body["results"]
    assert get_result["canonical_path"] == "/x"
    assert post_result["canonical_path"] == "/x"
    assert get_result["errors"] == [] and post_result["errors"] == []


def test_migration_is_read_only_and_pinned_to_snapshot(client, published):
    body = migrate(
        client, TARGET_ROUTES, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    assert body["version"] == 1
    assert body["results"][0]["active_version"] == 1

    active = client.get("/active").json()
    assert active["version"] == 1
    assert [r["id"] for r in active["routes"]] == ["user", "assets", "item"]


def test_empty_active_version_zero(client):
    # 未发布任何版本：固定版本 0，所有链接都无旧命中。
    body = migrate(
        client, TARGET_ROUTES, [{"method": "GET", "path": "/v1/users/42"}]
    ).json()
    assert body["status"] == "migrated"
    assert body["version"] == 0
    result = body["results"][0]
    assert result["active_version"] == 0
    assert [e["code"] for e in result["errors"]] == ["no_active_match"]


def test_empty_links(client, published):
    body = migrate(client, TARGET_ROUTES, []).json()
    assert body["status"] == "migrated"
    assert body["results"] == []


def test_target_with_unused_old_parameter_is_fine(client):
    # 旧模板参数比目标多：多余旧参数被忽略，不视为失败。
    routes = [{"method": "GET", "path": "/v1/{a}/{b}", "id": "ab"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/t/{a}", "id": "ab"}]
    body = migrate(client, target, [{"method": "GET", "path": "/v1/1/2"}]).json()
    result = body["results"][0]
    assert result["canonical_path"] == "/t/1"
    assert result["errors"] == []


def test_old_static_segment_target_parameter_fails_without_value(client):
    # 旧模板对应位置是静态段（没有同名参数），目标却要求参数 -> 无法对应。
    routes = [{"method": "GET", "path": "/v1/users/me", "id": "profile"}]
    assert publish(client, routes, 0).json()["status"] == "published"
    target = [{"method": "GET", "path": "/users/{id}", "id": "profile"}]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/me"}]
    ).json()
    result = body["results"][0]
    assert result["canonical_path"] is None
    assert result["errors"][0]["code"] == "parameter_mismatch"


def test_registration_index_shift_does_not_break_id_mapping(client, published):
    # 目标中同 id 路由移动了注册下标：按 id 而非下标对应，仍能迁移。
    target = [
        {"method": "GET", "path": "/api/items/{key:slug}", "id": "item"},
        {"method": "GET", "path": "/api/assets/**", "id": "assets"},
        {"method": "GET", "path": "/api/users/{id:int}", "priority": 5, "id": "user"},
    ]
    body = migrate(
        client, target, [{"method": "GET", "path": "/v1/users/7"}]
    ).json()
    result = body["results"][0]
    assert result["target"]["index"] == 2
    assert result["canonical_path"] == "/api/users/7"


def test_missing_envelope_fields_return_422(client):
    assert client.post("/migrate-links", json={"routes": []}).status_code == 422
    assert client.post("/migrate-links", json={"links": []}).status_code == 422
    response = client.post(
        "/migrate-links",
        json={"routes": [], "links": [{"method": "GET"}]},
    )
    assert response.status_code == 422
