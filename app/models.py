"""请求/响应 Pydantic 模型。"""

from __future__ import annotations

from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, Field

Method = Literal[
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"
]

#: 支持的 HTTP 方法集合（与 ``Method`` 字面量保持一致）。
ALLOWED_METHODS: frozenset[str] = frozenset(get_args(Method))

ConflictKind = Literal["duplicate", "full_shadow", "partial_overlap"]
ConflictStatus = Literal["proven", "unprovable"]


class RouteIn(BaseModel):
    method: str = Field(..., description="HTTP 方法，如 GET / POST（大小写不敏感）")
    path: str = Field(..., alias="template", description="路径模板，如 /users/{id:int}")
    priority: int = Field(
        default=0, description="优先级，数值越大越先匹配；相同优先级按注册顺序"
    )
    id: Optional[str] = Field(default=None, description="可选的调用方提供的路由标识")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class AnalyzeRequest(BaseModel):
    routes: list[RouteIn] = Field(..., description="按注册顺序排列的路由")


class ParseError(BaseModel):
    index: int
    id: Optional[str] = None
    method: Optional[str] = None
    template: Optional[str] = None
    error: str


class RouteOut(BaseModel):
    index: int
    id: str
    method: str
    template: str
    normalized_template: str
    priority: int
    fixed_segments: int
    wildcard: bool
    warnings: list[str] = Field(default_factory=list)


class Conflict(BaseModel):
    method: str
    winner_index: int
    winner_id: str
    loser_index: int
    loser_id: str
    winner_template: str
    loser_template: str
    winner_priority: int
    loser_priority: int
    kind: ConflictKind = Field(
        description="duplicate=可达集合相同；full_shadow=负方被完全遮蔽；"
        "partial_overlap=仅部分相交"
    )
    status: ConflictStatus = Field(
        description="proven=已构造示例路径；unprovable=约束相交但无法构造见证"
    )
    witness_path: Optional[str] = Field(
        default=None, description="同时命中两条路由的最短稳定示例路径"
    )
    reason: Optional[str] = Field(
        default=None, description="unprovable 时的原因说明"
    )


class SuggestedRoute(BaseModel):
    index: int
    id: str
    template: str
    priority: int
    original_position: int = Field(description="在该方法原注册顺序中的位置（从 1 起）")
    suggested_position: int = Field(description="建议顺序中的位置（从 1 起）")
    move: Optional[int] = Field(
        default=None,
        description="相对原位置的移动量（原位置-新位置）；正数表示前移，负数表示后移",
    )


class SuggestedOrder(BaseModel):
    method: str
    order: list[SuggestedRoute]


class Summary(BaseModel):
    total: int
    valid: int
    parse_errors: int
    conflicts: int
    full_shadows: int
    duplicates: int
    partial_overlaps: int
    unprovable: int
    shadowed_routes: int


class AnalyzeResponse(BaseModel):
    routes: list[RouteOut]
    parse_errors: list[ParseError]
    conflicts: list[Conflict]
    suggested_order: list[SuggestedOrder]
    summary: Summary


class ExportOpenApiRequest(BaseModel):
    routes: list[RouteIn] = Field(..., description="按注册顺序排列的路由")


class ExportOpenApiResponse(BaseModel):
    paths: dict[str, Any] = Field(
        default_factory=dict,
        description="可直接嵌入 OpenAPI 3.1 文档的 paths 片段（按路径、方法排序）",
    )
    parse_errors: list[ParseError] = Field(
        default_factory=list,
        description="解析失败或被拒绝的重复路由（含原始下标）",
    )


class RenderPathRouteIn(BaseModel):
    id: Optional[str] = Field(
        default=None,
        description="调用方提供的路由标识；在响应中原样回填，便于对齐结果",
    )
    path: str = Field(
        ..., alias="template", description="路径模板，如 /users/{id:int}/items/**"
    )
    parameters: Optional[dict[str, Any]] = Field(
        default=None,
        description="参数名 -> 参数值；JSON 字符串，或可精确表示为整数的数字",
    )
    wildcard: Optional[list[Any]] = Field(
        default=None,
        description="末尾 ** 接收的段数组（可为空/缺省）；模板不含 ** 时禁止提供",
    )

    model_config = {"populate_by_name": True, "extra": "ignore"}


class RenderPathsRequest(BaseModel):
    routes: list[RenderPathRouteIn] = Field(
        ..., description="按调用顺序排列的带参路由，结果按相同顺序返回"
    )


class RenderPathError(BaseModel):
    code: str = Field(
        description=(
            "template_invalid=模板解析失败；missing_parameter=缺少模板参数；"
            "extra_parameter=模板不接受的参数；invalid_parameter=参数类型/取值"
            "不满足约束；invalid_wildcard=通配段取值非法；"
            "wildcard_not_allowed=模板不含末尾 **；not_canonical=生成结果未通过"
            "规范化往返校验"
        )
    )
    field: Optional[str] = Field(
        default=None,
        description="出错字段：参数名或 wildcard[<下标>]；模板级错误时为 null",
    )
    message: str


class RenderPathResult(BaseModel):
    index: int = Field(..., description="在输入数组中的原始下标")
    id: Optional[str] = None
    template: Optional[str] = Field(
        default=None, description="输入的原始模板；信封级字段缺失时为 null"
    )
    normalized_template: Optional[str] = Field(
        default=None, description="规范化后的模板；模板解析失败时为 null"
    )
    canonical_path: Optional[str] = Field(
        default=None,
        description="反向生成的可调用 canonical path；任一项校验失败时为 null",
    )
    errors: list[RenderPathError] = Field(default_factory=list)


class RenderPathsResponse(BaseModel):
    results: list[RenderPathResult] = Field(
        ..., description="与输入 routes 等长、顺序一致的逐条结果"
    )


PublishStatus = Literal["published", "rejected", "version_conflict"]


class PublishRequest(BaseModel):
    routes: list[RouteIn] = Field(..., description="按注册顺序排列的候选路由")
    expected_version: int = Field(
        ..., ge=0, description="期望的当前活动版本号（CAS：必须等于当前版本才会发布）"
    )


class PublishResponse(BaseModel):
    status: PublishStatus = Field(
        description=(
            "published=已原子替换并递增版本；rejected=校验未通过，活动版本不变；"
            "version_conflict=预期版本与当前版本不一致，未覆盖已发布内容"
        )
    )
    version: int = Field(description="操作完成后的当前活动版本号")
    parse_errors: list[ParseError] = Field(
        default_factory=list,
        description="rejected 时：审计与导出两侧报告的解析错误/重复导出路径（含原始下标）",
    )
    conflicts: list[Conflict] = Field(
        default_factory=list,
        description="rejected 时：审计发现的 full_shadow 冲突（含双方原始下标）",
    )


class ActiveResponse(BaseModel):
    version: int = Field(..., description="当前活动版本号；初始为 0")
    routes: list[RouteIn] = Field(
        default_factory=list, description="发布时提交的路由（按注册顺序）"
    )
    audit: AnalyzeResponse = Field(
        ..., description="发布时对同一路由集运行的冲突审计结果"
    )
    openapi_paths: dict[str, Any] = Field(
        default_factory=dict,
        description="发布时生成的可嵌入 OpenAPI 3.1 的 paths 片段",
    )


class SampleIn(BaseModel):
    method: str = Field(..., description="HTTP 方法，如 GET / POST（大小写不敏感）")
    path: str = Field(..., description="绝对路径，如 /users/42（必须以 '/' 开头）")

    model_config = {"extra": "ignore"}


class PrecheckRequest(BaseModel):
    routes: list[RouteIn] = Field(
        ..., description="按注册顺序排列的候选路由（与 /publish 同构）"
    )
    samples: list[SampleIn] = Field(
        ..., description="请求样本（方法 + 绝对路径），结果按相同顺序返回"
    )


class AttributionHit(BaseModel):
    index: int = Field(description="命中路由在对应路由数组中的原始下标")
    id: str = Field(description="命中路由的有效 id（显式 id 或生成的 route-<下标>）")
    normalized_template: str = Field(description="命中路由的规范化模板")


class SampleAttribution(BaseModel):
    index: int = Field(description="样本在输入数组中的原始下标")
    method: str = Field(description="大写规范化后的方法回显")
    path: str = Field(description="原始路径回显")
    normalized_path: Optional[str] = Field(
        default=None, description="规范化后的路径；路径非法时为 null"
    )
    active: Optional[AttributionHit] = Field(
        default=None, description="活动快照侧的唯一命中路由；无命中为 null"
    )
    candidate: Optional[AttributionHit] = Field(
        default=None, description="候选路由侧的唯一命中路由；无命中为 null"
    )
    changed: Optional[bool] = Field(
        default=None,
        description="是否改投：两侧命中路由的身份（方法、规范化模板、显式 id）"
        "不一致，或仅一侧有命中；样本存在错误时为 null",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="样本级错误（非法方法/路径）；非空时该样本不做归属比较",
    )


PrecheckStatus = Literal["compared", "rejected"]


class PrecheckResponse(BaseModel):
    status: PrecheckStatus = Field(
        description="compared=候选通过校验并完成归属比较；rejected=候选未通过 "
        "/publish 同款校验，整批不做归属比较"
    )
    version: int = Field(description="本次比较所固定的活动快照版本号")
    parse_errors: list[ParseError] = Field(
        default_factory=list,
        description="rejected 时：候选路由的解析错误/重复导出路径（含原始下标）",
    )
    conflicts: list[Conflict] = Field(
        default_factory=list,
        description="rejected 时：候选路由的 full_shadow 冲突（含双方原始下标）",
    )
    results: list[SampleAttribution] = Field(
        default_factory=list,
        description="compared 时：与 samples 等长、同序的逐样本归属比较",
    )


class MigrateRequest(BaseModel):
    routes: list[RouteIn] = Field(
        ..., description="按注册顺序排列的目标路由（与 /publish 同构）"
    )
    links: list[SampleIn] = Field(
        ..., description="旧链接（方法 + 绝对路径），迁移结果按相同顺序返回"
    )


class MigrateError(BaseModel):
    code: str = Field(
        description=(
            "unsupported_method=方法不支持；invalid_path=路径非法；"
            "no_active_match=活动路由无命中；missing_route_id=旧命中路由缺少显式 id；"
            "ambiguous_route_id=显式 id 在同方法下不唯一；"
            "target_missing=目标配置缺少同方法同 id 的路由；"
            "parameter_unmapped=参数/通配尾段无法在两侧模板间完整对应；"
            "constraint_violation=提取值不满足目标参数约束；"
            "not_canonical=生成路径未通过规范化往返校验；"
            "intercepted=生成路径在目标全集中的胜方不是对应目标路由"
        )
    )
    message: str


class MigrateResult(BaseModel):
    index: int = Field(description="旧链接在输入数组中的原始下标")
    version: int = Field(description="本次迁移所固定的活动快照版本号")
    method: str = Field(description="大写规范化后的方法回显")
    path: str = Field(description="原始路径回显")
    normalized_path: Optional[str] = Field(
        default=None, description="规范化后的旧链接路径；路径非法时为 null"
    )
    active: Optional[AttributionHit] = Field(
        default=None, description="活动快照侧的旧命中路由；无命中为 null"
    )
    target: Optional[AttributionHit] = Field(
        default=None, description="按显式 id 对应到的目标路由；无法对应时为 null"
    )
    canonical_path: Optional[str] = Field(
        default=None, description="迁移后的 canonical path；无法迁移时为 null"
    )
    errors: list[MigrateError] = Field(
        default_factory=list,
        description="逐项失败原因；非空时 canonical_path 为 null，不影响其它项",
    )


MigrateStatus = Literal["generated", "rejected"]


class MigrateResponse(BaseModel):
    status: MigrateStatus = Field(
        description="generated=目标配置通过校验并逐项生成迁移结果；"
        "rejected=目标配置未通过 /publish 同款校验，整批不生成"
    )
    version: int = Field(description="本次迁移所固定的活动快照版本号")
    parse_errors: list[ParseError] = Field(
        default_factory=list,
        description="rejected 时：目标路由的解析错误/重复导出路径（含原始下标）",
    )
    conflicts: list[Conflict] = Field(
        default_factory=list,
        description="rejected 时：目标路由的 full_shadow 冲突（含双方原始下标）",
    )
    results: list[MigrateResult] = Field(
        default_factory=list,
        description="generated 时：与 links 等长、同序的逐项迁移结果",
    )
