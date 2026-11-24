"""围栏试算工作流专用接口。

通用资源路由负责列表/登记/动作；本路由承载题目要求的专有动作：
显式采集点接入与补报、试算样本批冻结与回看、围栏修订文件导入、
绑定样本批的围栏试算、修订验收/停用、巡检预约首次确认。
"""
from __future__ import annotations

import functools
from typing import Any, Callable, TypeVar

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.geo_source import GeoSourceService
from app.services.geofence_rule import GeofenceRuleService
from app.services.inspection_appointment import InspectionAppointmentService

router = APIRouter(prefix="/api/geo-workflow", tags=["围栏试算工作流"])

T = TypeVar("T")


def business_endpoint(func: Callable[..., T]) -> Callable[..., T]:
    """把领域层的业务校验/文件缺失异常统一翻译成 HTTP 400。"""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        try:
            return func(*args, **kwargs)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return wrapper


class IngestPointsPayload(BaseModel):
    batch_key: str
    points: list[dict[str, Any]]
    terminal: str = "TERM-EXPLICIT"
    locate_method: str = "GPS"


class TrialSamplePayload(BaseModel):
    source_batch_key: str
    sample_batch_no: str
    point_ids: list[Any] | None = None
    scope: dict[str, Any] | None = None


class FenceImportPayload(BaseModel):
    file_name: str
    fences: list[dict[str, Any]]


class TrialRunPayload(BaseModel):
    sample_batch_no: str
    revision_id: int
    run_no: str | None = None


class RevisionIdPayload(BaseModel):
    revision_id: int


class AcceptRevisionPayload(BaseModel):
    revision_id: int
    conclusion: str = "独立验收通过"


class AppointmentPayload(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


class FirstConfirmPayload(BaseModel):
    revision_id: int | None = None


@router.post("/collect/ingest")
@business_endpoint
def ingest_points(payload: IngestPointsPayload) -> dict[str, Any]:
    """显式接入或补报真实采集点（按观测身份 point_id 落盘）。"""
    return GeoSourceService().ingest_collected_points(
        payload.batch_key, payload.points,
        terminal=payload.terminal, locate_method=payload.locate_method)


@router.post("/trial-samples")
@business_endpoint
def create_trial_sample(payload: TrialSamplePayload) -> dict[str, Any]:
    """冻结试算样本批：记录实际进入试算的点、取样范围与出处校验值。"""
    return GeoSourceService().create_trial_sample_batch(
        payload.source_batch_key, payload.sample_batch_no,
        point_ids=payload.point_ids, scope=payload.scope)


@router.get("/trial-samples/{sample_batch_no}")
@business_endpoint
def get_trial_sample(sample_batch_no: str) -> dict[str, Any]:
    """按原编号回看试算样本批，原点数与出处不受原批次补报影响。"""
    return GeoSourceService().get_trial_sample_batch(sample_batch_no)


@router.get("/trial-samples")
@business_endpoint
def list_trial_samples() -> list[dict[str, Any]]:
    return GeoSourceService().list_trial_sample_batches()


@router.post("/fence-revisions/import")
@business_endpoint
def import_fence_revision_file(payload: FenceImportPayload) -> dict[str, Any]:
    """导入围栏修订文件：几何合法性校验、幂等去重、给出可下发围栏结论。"""
    return GeofenceRuleService().import_fence_revision_file(
        payload.file_name, payload.fences)


@router.get("/fence-revisions")
@business_endpoint
def list_fence_revisions(fence_code: str | None = None) -> list[dict[str, Any]]:
    return GeofenceRuleService().list_fence_revisions(fence_code)


@router.post("/fence-revisions/accept")
@business_endpoint
def accept_fence_revision(payload: AcceptRevisionPayload) -> dict[str, Any]:
    """对合法修订执行独立验收。"""
    return GeofenceRuleService().accept_fence_revision(
        payload.revision_id, payload.conclusion)


@router.post("/fence-revisions/deactivate")
@business_endpoint
def deactivate_fence_revision(payload: RevisionIdPayload) -> dict[str, Any]:
    """停用修订；进行中的巡检仍持有时拒绝停用。"""
    return GeofenceRuleService().deactivate_fence_revision(payload.revision_id)


@router.post("/fence-trials")
@business_endpoint
def trial_run_fence(payload: TrialRunPayload) -> dict[str, Any]:
    """用绑定的试算样本批执行围栏空间命中试算。"""
    return GeofenceRuleService().trial_run_fence(
        payload.sample_batch_no, payload.revision_id, run_no=payload.run_no)


@router.get("/fence-trials/{run_id}")
@business_endpoint
def get_trial_run(run_id: str) -> dict[str, Any]:
    return GeofenceRuleService().get_trial_run(run_id)


@router.post("/inspection-appointments")
@business_endpoint
def create_appointment(payload: AppointmentPayload) -> dict[str, Any]:
    """创建巡检预约，绑定仍有效的旧围栏修订与试算样本批。"""
    return InspectionAppointmentService().create_appointment(payload.values)


@router.get("/inspection-appointments")
@business_endpoint
def list_appointments() -> list[dict[str, Any]]:
    from app.store import store
    return list(store.rows("inspection_appointment"))


@router.post("/inspection-appointments/{appointment_id}/first-confirm")
@business_endpoint
def first_confirm(appointment_id: int,
                  payload: FirstConfirmPayload) -> dict[str, Any]:
    """巡检预约首次确认：正式计算只消费预约绑定的样本批。"""
    return InspectionAppointmentService().first_confirm(
        appointment_id, payload.revision_id)
