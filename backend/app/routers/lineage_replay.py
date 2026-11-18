"""指标血缘 / 回放引擎的联动接口（序号 23 迁移切换）。

这些接口必须注册在通用资源路由之前，否则 /api/{module} 会把固定路径吃掉。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api", tags=["血缘与回放"])


class RefreshPayload(BaseModel):
    batch_id: str | None = None


class SwitchPayload(BaseModel):
    metric_name: str | None = None
    entry_id: int | None = None
    switch_key: str | None = None
    mode: str = "replace"
    source_env: str = "旧环境"
    target_env: str = "新环境"
    removed_upstreams: list[str] = Field(default_factory=list)
    added_upstreams: list[str] = Field(default_factory=list)
    removed_operators: list[str] = Field(default_factory=list)
    added_operators: list[str] = Field(default_factory=list)
    removed_outputs: list[str] = Field(default_factory=list)
    added_outputs: list[str] = Field(default_factory=list)
    simulate_failure: bool = False


class ReplayJobPayload(BaseModel):
    job_name: str
    checkpoint_id: str
    topology_name: str = ""
    mode: str = "full_replay"
    lineage_metric: str | None = None


def _lineage_service():
    service = service_for("metric_lineage")
    if service is None:
        raise HTTPException(status_code=404, detail="指标血缘服务未注册")
    return service


def _replay_service():
    service = service_for("replay_engine")
    if service is None:
        raise HTTPException(status_code=404, detail="回放引擎服务未注册")
    return service


# ---- 指标血缘 ----

@router.get("/lineage/migration-ledger")
def migration_ledger() -> dict[str, Any]:
    """序号 23 迁移台账：切换记录、版本存量与修订记录。"""
    service = _lineage_service()
    service._ensure_all_current_versions()
    return service.migration_ledger()


@router.get("/lineage/deployment-panel")
def deployment_panel() -> dict[str, Any]:
    """序号 23 部署面板：数据库部署写入的血缘数据、回放构建流水线与阻断任务。"""
    service = _lineage_service()
    service._ensure_all_current_versions()
    return service.deployment_panel()


@router.post("/lineage/refresh-all")
def refresh_all_lineages(payload: RefreshPayload | None = None) -> dict[str, Any]:
    """批量刷新全部血缘：重新核对上下游链接，同一批次只提升一次版本。"""
    return _lineage_service().refresh_all_lineages(
        batch_id=(payload.batch_id if payload else None),
    )


@router.post("/lineage/switch-env")
def switch_environment(payload: SwitchPayload) -> dict[str, Any]:
    """执行序号 23 的迁移切换（replace / retain_old），重复切换幂等。"""
    return _lineage_service().switch_environment(
        metric_name=payload.metric_name,
        entry_id=payload.entry_id,
        switch_key=payload.switch_key,
        mode=payload.mode,
        source_env=payload.source_env,
        target_env=payload.target_env,
        removed_upstreams=payload.removed_upstreams,
        added_upstreams=payload.added_upstreams,
        removed_operators=payload.removed_operators,
        added_operators=payload.added_operators,
        removed_outputs=payload.removed_outputs,
        added_outputs=payload.added_outputs,
        simulate_failure=payload.simulate_failure,
    )


@router.get("/lineage/{entry_id}/graph")
def lineage_graph(entry_id: int) -> dict[str, Any]:
    """血缘 DAG：节点、连线与断链位置。"""
    service = _lineage_service()
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"血缘记录 {entry_id} 不存在或已归档")
    return service.build_lineage_graph(entry.get("指标名称", ""))


@router.post("/lineage/{entry_id}/refresh")
def refresh_lineage(entry_id: int, payload: RefreshPayload | None = None) -> dict[str, Any]:
    """刷新单条血缘：重算路径并核对完整性。"""
    service = _lineage_service()
    result = service.refresh_lineage(
        entry_id=entry_id,
        batch_id=(payload.batch_id if payload else None),
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "血缘刷新失败"))
    return result


@router.get("/lineage/{entry_id}/verify")
def verify_lineage(entry_id: int) -> dict[str, Any]:
    """核对血缘当前版本的上下游链接完整性。"""
    service = _lineage_service()
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"血缘记录 {entry_id} 不存在或已归档")
    return service.verify_current_version(entry_id=entry_id)


# ---- 回放引擎 ----

@router.get("/replay/consistency")
def replay_consistency() -> dict[str, Any]:
    """核对指标血缘状态与回放验证报告是否一致。"""
    return _replay_service().lineage_consistency_check()


@router.get("/replay/build-pipeline")
def replay_build_pipeline() -> dict[str, Any]:
    """回放构建流水线详情：可执行 / 被阻断的任务清单。"""
    panel = _lineage_service().deployment_panel()
    return {"ok": True, "build": panel["replay_build"], "sequence": panel["sequence"]}


@router.post("/replay/jobs")
def create_replay_job(payload: ReplayJobPayload) -> dict[str, Any]:
    """创建回放任务：引用血缘当前版本，断链时直接生成验证失败报告。"""
    return _replay_service().create_replay_job(
        job_name=payload.job_name,
        checkpoint_id=payload.checkpoint_id,
        topology_name=payload.topology_name,
        mode=payload.mode,
        lineage_metric=payload.lineage_metric,
    )


@router.post("/replay/{task_id}/execute")
def execute_replay(task_id: int) -> dict[str, Any]:
    """执行回放：执行前后都会核对血缘，断链任务只产出验证失败报告。"""
    return _replay_service().execute_replay(task_id)


@router.get("/replay/{task_id}/report")
def replay_report(task_id: int) -> dict[str, Any]:
    """读取回放任务的验证报告。"""
    report = _replay_service().replay_report(task_id)
    if not report.get("ok"):
        raise HTTPException(status_code=404, detail=report.get("error", "回放任务不存在"))
    return report
