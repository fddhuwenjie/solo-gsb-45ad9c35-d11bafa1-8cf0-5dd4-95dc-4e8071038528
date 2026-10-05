"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI

from .compare import compare_attribution
from .exporter import export_openapi_fragment
from .migrate import migrate_links
from .models import (
    ActiveResponse,
    AnalyzeRequest,
    AnalyzeResponse,
    ExportOpenApiRequest,
    ExportOpenApiResponse,
    MigrateLinksRequest,
    MigrateLinksResponse,
    PrecheckRequest,
    PrecheckResponse,
    PublishRequest,
    PublishResponse,
    RenderPathsRequest,
    RenderPathsResponse,
)
from .publish import PublishStore
from .render import render_paths
from .service import analyze_routes

app = FastAPI(
    title="HTTP 路由覆盖冲突审计 API",
    description=(
        "按注册顺序接收路由模板，判定同方法路由的可达集合是否相交、谁遮蔽谁，"
        "并构造最短稳定的示例路径；也可把路由配置导出为可嵌入 OpenAPI 3.1 的 "
        "paths 片段，或按模板与参数值反向生成可调用的 canonical path。"
        "支持把校验通过的路由配置发布为可持久化的活动版本（CAS 递增），"
        "并从同一快照读取路由、审计结果与 OpenAPI paths；"
        "还可在固定活动快照上只读预检候选路由，比较样本请求在两侧的归属；"
        "或把一批旧链接经显式 id 映射到校验通过的目标路由，生成迁移用的 "
        "canonical path 清单。"
    ),
    version="1.0.0",
)

#: 活动版本存储（测试可通过 monkeypatch 替换为独立实例）。
store = PublishStore.from_env()


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/analyze", response_model=AnalyzeResponse, tags=["audit"])
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    """分析一组路由的覆盖冲突。"""
    return analyze_routes(request.routes)


@app.post("/export-openapi", response_model=ExportOpenApiResponse, tags=["export"])
def export_openapi(request: ExportOpenApiRequest) -> ExportOpenApiResponse:
    """把路由配置转换为可嵌入 OpenAPI 3.1 的 paths 片段（不含冲突统计）。"""
    return export_openapi_fragment(request.routes)


@app.post("/render-paths", response_model=RenderPathsResponse, tags=["render"])
def render_path_batch(request: RenderPathsRequest) -> RenderPathsResponse:
    """按路由模板与参数值反向生成可调用 URL（逐条报告错误，不中止批处理）。"""
    return render_paths(request.routes)


@app.post("/publish", response_model=PublishResponse, tags=["publish"])
def publish(request: PublishRequest) -> PublishResponse:
    """校验候选路由集，并在 expected_version 等于当前版本时原子发布新版本。"""
    return store.publish(request.routes, request.expected_version)


@app.get("/active", response_model=ActiveResponse, response_model_by_alias=False, tags=["publish"])
def active() -> ActiveResponse:
    """读取当前活动版本的完整快照：路由、审计结果与 OpenAPI paths。"""
    snapshot = store.get()
    return ActiveResponse(
        version=snapshot.version,
        routes=snapshot.routes,
        audit=snapshot.audit,
        openapi_paths=snapshot.openapi_paths,
    )


@app.post("/precheck", response_model=PrecheckResponse, tags=["precheck"])
def precheck(request: PrecheckRequest) -> PrecheckResponse:
    """只读预检：固定当前活动快照，比较样本请求在活动与候选路由下的归属。"""
    # 先固定活动快照：版本与活动路由在整个比较期间保持一致，且不修改活动配置。
    snapshot = store.get()
    return compare_attribution(snapshot, request.routes, request.samples)


@app.post("/migrate-links", response_model=MigrateLinksResponse, tags=["migrate"])
def migrate_old_links(request: MigrateLinksRequest) -> MigrateLinksResponse:
    """只读迁移清单：固定活动快照，把旧链接经显式 id 映射到目标路由并生成 canonical path。"""
    # 先固定活动快照：目标校验与逐项迁移都基于这同一份快照，不修改活动配置。
    snapshot = store.get()
    return migrate_links(snapshot, request.routes, request.links)
