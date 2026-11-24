"""巡检预约领域服务。

巡检预约在创建时绑定一份围栏修订（首次确认前仍有效的旧边界）和一个试算
样本批。首次确认时：

- 持有效规则的进行中巡检不能被任何修订停用/替换动作打断；
- 若该围栏另有已通过独立验收的新边界，可显式选择新边界完成首次确认，
  确认时正式计算只消费预约绑定的样本批；
- 不合法、未验收的边界永远不能用于首次确认。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class InspectionAppointmentService(BaseModuleService):
    MODULE = "inspection_appointment"
    ENTITY = "巡检预约"
    OPERATION_TERM = "巡检"
    LIST_FIELDS = ["预约编号", "围栏编号", "绑定修订", "绑定样本批", "预约时间", "巡检状态", "首次确认边界", "正式命中数"]
    REQUIRED_FIELDS = ["预约编号", "围栏编号", "绑定修订", "绑定样本批"]
    STATUS_ORDER = ["待首次确认", "巡检中", "已完成", "已取消"]
    ACTION_RULES = {"启动巡检": "巡检中", "完成巡检": "已完成", "取消预约": "已取消"}
    NEGATIVE_ACTIONS = []

    def create_appointment(self, values: dict[str, Any]) -> dict[str, Any]:
        """创建巡检预约并校验绑定的修订与样本批真实存在、边界合法。"""
        missing = [f for f in self.REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            raise ValueError(f"缺少必填字段: {missing}")
        revision_id = int(values["绑定修订"])
        fence_svc = self._safe_import(
            "app.services.geofence_rule", "GeofenceRuleService")()
        revision = fence_svc.get_revision(revision_id)
        if revision is None:
            raise ValueError(f"绑定的围栏修订 {revision_id} 不存在")
        if revision.get("合法性") != "合法":
            raise ValueError(
                f"围栏 {revision.get('围栏编号')} 边界不合法，不能据此建立巡检预约")
        if revision.get("状态") == "已停用":
            raise ValueError("绑定的围栏修订已停用，不能建立巡检预约")

        sample_no = str(values["绑定样本批"])
        geo_svc = self._safe_import(
            "app.services.geo_source", "GeoSourceService")()
        geo_svc.get_trial_sample_batch(sample_no)  # 样本批不存在直接抛错

        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "status": "待首次确认",
            "pending": True,
            "abnormal": False,
            "预约编号": values["预约编号"],
            "围栏编号": values["围栏编号"],
            "绑定修订": revision_id,
            "绑定样本批": sample_no,
            "预约时间": values.get("预约时间", time.strftime("%Y-%m-%d %H:%M:%S")),
            "巡检状态": "待首次确认",
            "首次确认边界": "",
            "正式命中数": "0",
            "适用修订ID": "",
        }
        rows.append(entry)
        return entry

    def active_appointments_for_revision(self, revision_id: int) -> list[dict[str, Any]]:
        """返回仍持有指定修订、且巡检尚未结束的预约（有效规则不可打断的依据）。"""
        return [
            row for row in store.rows(self.MODULE)
            if int(row.get("绑定修订", 0)) == revision_id
            and row.get("巡检状态") in ("待首次确认", "巡检中")
        ]

    def first_confirm(self, appointment_id: int,
                      revision_id: int | None = None) -> dict[str, Any]:
        """执行预约的首次确认。

        默认沿用预约绑定的旧边界（要求其仍未停用）；若传入同一围栏下另一份
        已通过独立验收的修订，则改用新边界。正式命中只计算绑定样本批。
        """
        appointment = store.find(self.MODULE, appointment_id)
        if appointment is None:
            raise ValueError(f"巡检预约 {appointment_id} 不存在")
        if appointment.get("巡检状态") != "待首次确认":
            raise ValueError(
                f"预约 {appointment.get('预约编号')} 已完成首次确认，不能重复确认")

        fence_svc = self._safe_import(
            "app.services.geofence_rule", "GeofenceRuleService")()
        bound = fence_svc.get_revision(int(appointment["绑定修订"]))
        chosen_id = revision_id or int(appointment["绑定修订"])
        chosen = fence_svc.get_revision(chosen_id)
        if chosen is None:
            raise ValueError(f"围栏修订 {chosen_id} 不存在")
        if chosen.get("围栏编号") != appointment.get("围栏编号"):
            raise ValueError("首次确认只能选用同一围栏的修订边界")
        if chosen.get("合法性") != "合法":
            raise ValueError(
                f"围栏 {chosen.get('围栏编号')} 边界不合法，不可进入正式计算")
        if chosen.get("状态") != "已验收":
            raise ValueError(
                f"修订 {chosen_id} 状态为{chosen.get('状态')}，"
                "只有通过独立验收的边界才能用于首次确认")

        # 沿用旧边界的前提：旧规则仍然有效（未被停用），有效巡检不被打断。
        if chosen_id == int(appointment["绑定修订"]) and bound.get("状态") == "已停用":
            raise ValueError("预约绑定的旧边界已停用，不能再沿用，请改用已验收的新边界")

        formal = fence_svc.formal_check_fence(
            str(appointment["绑定样本批"]), chosen_id)

        appointment["巡检状态"] = "巡检中"
        appointment["status"] = "巡检中"
        appointment["pending"] = False
        appointment["首次确认边界"] = chosen.get("规则版本", str(chosen_id))
        appointment["适用修订ID"] = chosen_id
        appointment["正式命中数"] = str(formal["hit_count"])
        appointment["首次确认时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
        appointment["正式命中明细"] = formal["hits"]
        return {"appointment": appointment, "formal_check": formal}
