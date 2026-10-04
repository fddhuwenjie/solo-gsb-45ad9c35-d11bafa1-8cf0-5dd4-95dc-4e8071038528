"""可持久化的活动路由版本存储：发布（CAS）与一致快照读取。

语义：

- 活动版本是一个**整体快照**：路由、冲突审计结果与 OpenAPI paths 在同一次
  发布中一起计算、一起落盘、一起被替换；读取永远返回同一快照，不会把旧路由
  与新导出片段拼接在一起。
- 发布采用 compare-and-swap：``expected_version`` 必须等于当前版本才替换，
  否则返回 ``version_conflict`` 与当前版本号；并发发布同一预期版本时只有
  一个成功。
- 快照以 JSON 落盘（临时文件 + ``os.replace`` 原子重命名），进程/容器重启后
  从同一文件恢复；初始版本为 0、路由为空。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .exporter import export_openapi_fragment
from .models import (
    AnalyzeResponse,
    Conflict,
    ExportOpenApiResponse,
    ParseError,
    PublishResponse,
    RouteIn,
)
from .service import analyze_routes

#: 覆盖状态文件路径的环境变量名。
STATE_FILE_ENV = "ROUTE_AUDIT_STATE_FILE"

#: 未设置环境变量时的默认状态文件（相对当前工作目录）。
DEFAULT_STATE_FILE = "data/active.json"


@dataclass(frozen=True)
class Snapshot:
    """一次发布的完整结果；构造后不再修改，可安全地被并发读取共享。"""

    version: int
    routes: list[dict[str, Any]]  # 发布时提交的路由（按注册顺序）
    audit: dict[str, Any]  # AnalyzeResponse 的 JSON 形式
    openapi_paths: dict[str, Any]  # 可嵌入 OpenAPI 3.1 的 paths 片段


def _merge_parse_errors(
    primary: list[ParseError], secondary: list[ParseError]
) -> list[ParseError]:
    """合并两侧的解析错误（导出侧额外覆盖重复路径等），按原始下标排序。"""
    merged = list(primary)
    seen = {(e.index, e.error) for e in merged}
    for error in secondary:
        if (error.index, error.error) not in seen:
            merged.append(error)
    merged.sort(key=lambda e: e.index)
    return merged


@dataclass(frozen=True)
class CandidateValidation:
    """候选路由集的校验结果；与当前活动状态无关，可复用于只读预检。"""

    audit: AnalyzeResponse
    export: ExportOpenApiResponse
    parse_errors: list[ParseError]
    full_shadows: list[Conflict]

    @property
    def ok(self) -> bool:
        """无解析错误（含导出路径重复）且无 full_shadow 时才允许发布/比较。"""
        return not self.parse_errors and not self.full_shadows


def validate_candidate(routes: list[RouteIn]) -> CandidateValidation:
    """对候选集运行与 /analyze、/export-openapi 完全相同的审计与导出校验。"""
    audit = analyze_routes(routes)
    export = export_openapi_fragment(routes)
    parse_errors = _merge_parse_errors(export.parse_errors, audit.parse_errors)
    full_shadows = [c for c in audit.conflicts if c.kind == "full_shadow"]
    return CandidateValidation(
        audit=audit,
        export=export,
        parse_errors=parse_errors,
        full_shadows=full_shadows,
    )


class PublishStore:
    """活动版本的持有者：内存快照 + 落盘持久化 + CAS 发布锁。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._snapshot = self._load()

    @classmethod
    def from_env(cls) -> PublishStore:
        return cls(os.environ.get(STATE_FILE_ENV, DEFAULT_STATE_FILE))

    @staticmethod
    def _initial_snapshot() -> Snapshot:
        return Snapshot(
            version=0,
            routes=[],
            audit=analyze_routes([]).model_dump(mode="json"),
            openapi_paths={},
        )

    def _load(self) -> Snapshot:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._initial_snapshot()
        data = json.loads(raw)
        return Snapshot(
            version=int(data["version"]),
            routes=list(data["routes"]),
            audit=dict(data["audit"]),
            openapi_paths=dict(data["openapi_paths"]),
        )

    def _persist(self, snapshot: Snapshot) -> None:
        """先写临时文件再原子重命名，避免崩溃留下半截状态。"""
        payload = {
            "version": snapshot.version,
            "routes": snapshot.routes,
            "audit": snapshot.audit,
            "openapi_paths": snapshot.openapi_paths,
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    def get(self) -> Snapshot:
        """返回当前活动快照（引用读取是原子的，快照本身不可变）。"""
        return self._snapshot

    def publish(
        self, routes: list[RouteIn], expected_version: int
    ) -> PublishResponse:
        """校验候选集并在版本匹配时原子替换活动版本。"""
        # 校验与状态无关，在锁外完成：同一候选集上跑审计与导出。
        validation = validate_candidate(routes)
        if not validation.ok:
            return PublishResponse(
                status="rejected",
                version=self._snapshot.version,
                parse_errors=validation.parse_errors,
                conflicts=validation.full_shadows,
            )

        with self._lock:
            current = self._snapshot
            if expected_version != current.version:
                return PublishResponse(
                    status="version_conflict", version=current.version
                )
            snapshot = Snapshot(
                version=current.version + 1,
                routes=[r.model_dump(mode="json") for r in routes],
                audit=validation.audit.model_dump(mode="json"),
                openapi_paths=validation.export.paths,
            )
            # 先落盘成功再切换内存快照；落盘失败时活动版本保持不变。
            self._persist(snapshot)
            self._snapshot = snapshot
            return PublishResponse(status="published", version=snapshot.version)
