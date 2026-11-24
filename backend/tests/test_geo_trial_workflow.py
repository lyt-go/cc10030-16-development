"""围栏试算样本批与修订版本化的端到端叙事测试。

覆盖需求链条：
显式采集点 → 冻结试算样本批 → 原批次补报 → 样本批原点数不变 →
新试算才能选到含补报的样本 → 一次导入合法/自交两类围栏修订 →
同次导入判断实际可下发范围 → 试算与正式确认各自只消费绑定样本批 →
巡检预约沿用旧边界/改用验收新边界 → 有效巡检不可打断、错误边界不可正式计算 →
更正文件重传 → 首次适用围栏、两阶段命中、重复导入仅一次有效修订。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir(monkeypatch):
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_trial_test_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "trial_samples", "fence_revisions",
                "fence_trials", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


# 甲围栏（JIA）新旧两个合法矩形；乙围栏（YI）先自交后更正为合法矩形。
JIA_OLD = "39.900,116.400;39.930,116.400;39.930,116.430;39.900,116.430"
JIA_NEW = "39.900,116.400;39.920,116.400;39.920,116.420;39.900,116.420"
# 蝴蝶结：边 (0,1)-(2,3) 与 (1,2)-(3,0) 交叉，必然自交。
YI_SELF_INTERSECT = ("39.950,116.450;39.980,116.480;39.980,116.450;39.950,116.480")
YI_FIXED = "39.950,116.450;39.980,116.450;39.980,116.480;39.950,116.480"

def make_points() -> list[dict]:
    """8 个真实采集点：甲新框 3 个、甲旧框多出 1 个、乙框 2 个、框外 2 个。"""
    base_ts = 1_790_000_000.0
    raw = [
        (1, 39.905, 116.405),  # 甲新旧框内
        (2, 39.915, 116.415),  # 甲新旧框内
        (3, 39.910, 116.410),  # 甲新旧框内
        (4, 39.925, 116.425),  # 仅甲旧框内（新框之外）
        (5, 39.960, 116.460),  # 乙框内
        (6, 39.970, 116.470),  # 乙框内
        (7, 30.000, 120.000),  # 框外
        (8, 31.000, 121.000),  # 框外
    ]
    return [
        {"point_id": pid, "lat": lat, "lon": lon,
         "timestamp": base_ts + idx * 60, "terminal": "TERM-A"}
        for idx, (pid, lat, lon) in enumerate(raw)
    ]


class TestTrialSampleBatchImmutability:
    def test_sample_batch_freezes_points_and_survives_backfill(self):
        from app.services.geo_source import GeoSourceService

        geo = GeoSourceService()
        batch_key = "BATCH-SRC-01"
        ingest = geo.ingest_collected_points(batch_key, make_points())
        assert ingest["total_points"] == 8

        # 先建试算样本批（全部 8 点进入试算）。
        meta = geo.create_trial_sample_batch(
            batch_key, "TSB-01", scope={"purpose": "围栏导入前试算"})
        assert meta["point_count"] == 8
        assert meta["point_ids"] == [1, 2, 3, 4, 5, 6, 7, 8]
        assert meta["scope"]["source_point_count_at_sample"] == 8
        assert meta["scope"]["bounds"]["min_lat"] == 30.0
        assert meta["source_batch_key"] == batch_key

        # 再补报一个落在甲围栏内的采集点。
        backfill = geo.ingest_collected_points(batch_key, [{
            "point_id": 9, "lat": 39.908, "lon": 116.408,
            "timestamp": 1_790_001_000.0, "terminal": "TERM-A",
        }])
        assert backfill["appended_points"] == 1
        assert backfill["total_points"] == 9

        # 直接按原编号回看：原点数、出处、校验值保持不变。
        view = geo.get_trial_sample_batch("TSB-01")
        assert len(view["points"]) == 8
        assert view["meta"]["point_count"] == 8
        assert view["meta"]["sample_checksum"] == meta["sample_checksum"]
        assert all(p["point_id"] != 9 for p in view["points"])
        current, _ = geo.get_batch_points(batch_key)
        assert len(current) == 9

        # 启动一份新的试算样本批才能选到含补报的 9 个点。
        meta2 = geo.create_trial_sample_batch(batch_key, "TSB-02")
        assert meta2["point_count"] == 9
        assert 9 in meta2["point_ids"]
        view2 = geo.get_trial_sample_batch("TSB-02")
        assert len(view2["points"]) == 9

        # 登记记录可在定位源接入模块列表中回看。
        listed = {row["批次编号"]: row for row in geo.list_trial_sample_batches()}
        assert "TSB-01" in listed and "TSB-02" in listed
        assert listed["TSB-01"]["记录条数"] == "8"

    def test_sample_batch_filters_by_observation_identity(self):
        from app.services.geo_source import GeoSourceService

        geo = GeoSourceService()
        geo.ingest_collected_points("BATCH-SRC-02", make_points())
        meta = geo.create_trial_sample_batch(
            "BATCH-SRC-02", "TSB-SUB", point_ids=[1, 5, 7])
        assert meta["point_ids"] == [1, 5, 7]
        with pytest.raises(ValueError, match="观测身份"):
            geo.create_trial_sample_batch(
                "BATCH-SRC-02", "TSB-BAD", point_ids=[1, 999])


class TestFenceRevisionImportAndTrial:
    def test_single_import_distinguishes_legal_and_self_intersecting(self):
        from app.services.geo_source import GeoSourceService
        from app.services.geofence_rule import GeofenceRuleService

        geo = GeoSourceService()
        fence = GeofenceRuleService()
        geo.ingest_collected_points("BATCH-FENCE", make_points())
        geo.create_trial_sample_batch("BATCH-FENCE", "TSB-FENCE")

        outcome = fence.import_fence_revision_file("fence_rev_20261124.json", [
            {"围栏编号": "JIA-SI", "顶点序列": JIA_NEW, "规则版本": "JIA-v2"},
            {"围栏编号": "YI-SI", "顶点序列": YI_SELF_INTERSECT, "规则版本": "YI-v1"},
        ])

        by_code = {r["围栏编号"]: r for r in outcome["results"]}
        assert by_code["JIA-SI"]["legal"] is True
        assert by_code["YI-SI"]["legal"] is False
        assert by_code["YI-SI"]["self_intersections"]  # 自交边对被定位出来
        # 同一次导入即可判断实际可下发的围栏范围：只有甲。
        assert outcome["deployable_fence_ids"] == ["JIA-SI"]

        # 非法修订留痕为已驳回，可按真实ID复查但不进入可下发清单。
        yi_rejected = fence.list_fence_revisions("YI-SI")[0]
        assert yi_rejected["状态"] == "已驳回" and yi_rejected["合法性"] == "非法"
        jia_rev = fence.list_fence_revisions("JIA-SI")[0]

        # 自交围栏不能试算，也不能进入任何正式计算。
        with pytest.raises(ValueError, match="不合法"):
            fence.trial_run_fence("TSB-FENCE", yi_rejected["id"])
        with pytest.raises(ValueError, match="不合法"):
            fence.accept_fence_revision(yi_rejected["id"])

        # 合法围栏的试算：两阶段中的第一阶段，命中只来自绑定样本批。
        trial = fence.trial_run_fence(
            "TSB-FENCE", jia_rev["id"], run_no="TRIAL-JIA-1")
        assert trial["total_points"] == 8
        assert trial["hit_count"] == 3
        assert {h["point_id"] for h in trial["hits"]} == {1, 2, 3}
        assert fence.get_trial_run("TRIAL-JIA-1")["hit_count"] == 3

    def test_duplicate_import_is_idempotent_and_keeps_single_revision(self):
        from app.services.geofence_rule import GeofenceRuleService

        fence = GeofenceRuleService()
        payload = [
            {"围栏编号": "JIA-DUP", "顶点序列": JIA_NEW, "规则版本": "JIA-v2"},
            {"围栏编号": "YI-DUP", "顶点序列": YI_SELF_INTERSECT, "规则版本": "YI-v1"},
        ]
        first = fence.import_fence_revision_file("fence_rev_dup.json", payload)
        second = fence.import_fence_revision_file("fence_rev_dup.json", payload)
        assert first["deduplicated"] is False
        assert second["deduplicated"] is True
        assert second["import_id"] == first["import_id"]
        jia_revisions = fence.list_fence_revisions("JIA-DUP")
        assert len(jia_revisions) == 1  # 重复导入仍只产生一次有效修订


class TestInspectionAppointmentLifecycle:
    def test_full_appointment_flow_with_old_and_new_boundary(self):
        from app.services.geo_source import GeoSourceService
        from app.services.geofence_rule import GeofenceRuleService
        from app.services.inspection_appointment import InspectionAppointmentService

        geo = GeoSourceService()
        fence = GeofenceRuleService()
        appointment_svc = InspectionAppointmentService()

        geo.ingest_collected_points("BATCH-APPT", make_points())
        tsb1 = geo.create_trial_sample_batch("BATCH-APPT", "TSB-APPT-1")
        # 补报点 9 只进入新样本批。
        geo.ingest_collected_points("BATCH-APPT", [{
            "point_id": 9, "lat": 39.908, "lon": 116.408,
            "timestamp": 1_790_001_000.0, "terminal": "TERM-A"}])
        tsb2 = geo.create_trial_sample_batch("BATCH-APPT", "TSB-APPT-2")
        assert tsb1["point_count"] == 8 and tsb2["point_count"] == 9

        # 旧边界先导入并验收，作为预约获准继续采用的仍有效旧边界。
        old_import = fence.import_fence_revision_file("jia_old_flow.json", [
            {"围栏编号": "JIA-FLOW", "顶点序列": JIA_OLD, "规则版本": "JIA-v1"}])
        old_rev_id = old_import["results"][0]["revision_id"]
        fence.accept_fence_revision(old_rev_id)

        # 预约绑定旧边界与 TSB-APPT-1（独立的巡检预约，尚未首次确认）。
        appointment = appointment_svc.create_appointment({
            "预约编号": "APPT-JIA-01",
            "围栏编号": "JIA-FLOW",
            "绑定修订": old_rev_id,
            "绑定样本批": "TSB-APPT-1",
        })
        appt_id = appointment["id"]

        # 新边界通过同一次导入的独立文件到达，乙的自交仍待修。
        new_import = fence.import_fence_revision_file("rev_flow_20261124.json", [
            {"围栏编号": "JIA-FLOW", "顶点序列": JIA_NEW, "规则版本": "JIA-v2"},
            {"围栏编号": "YI-FLOW", "顶点序列": YI_SELF_INTERSECT, "规则版本": "YI-v1"},
        ])
        assert new_import["deployable_fence_ids"] == ["JIA-FLOW"]
        new_rev_id = next(
            r["revision_id"] for r in new_import["results"]
            if r["围栏编号"] == "JIA-FLOW")
        # 新边界通过独立验收，获准用于这笔预约的首次确认。
        fence.accept_fence_revision(new_rev_id)

        # 试算阶段：新边界在两个样本批上的命中各自独立（两阶段命中的第一阶段）。
        trial_old_sample = fence.trial_run_fence(
            "TSB-APPT-1", new_rev_id, run_no="TRIAL-NEW-TSB1")
        trial_new_sample = fence.trial_run_fence(
            "TSB-APPT-2", new_rev_id, run_no="TRIAL-NEW-TSB2")
        assert trial_old_sample["hit_count"] == 3
        assert trial_new_sample["hit_count"] == 4  # 补报点 9 只在 TSB-2 命中

        # 乙未修好：乙修订只能以已驳回留痕，无法验收或进入正式计算。
        yi_rejected = fence.list_fence_revisions("YI-FLOW")[0]
        assert yi_rejected["状态"] == "已驳回"
        with pytest.raises(ValueError, match="不合法"):
            fence.accept_fence_revision(yi_rejected["id"])
        with pytest.raises(ValueError, match="不合法"):
            fence.formal_check_fence("TSB-APPT-1", yi_rejected["id"])

        # 有效规则的已有巡检不能被打断：预约存在期间旧边界不能停用。
        with pytest.raises(ValueError, match="巡检预约"):
            fence.deactivate_fence_revision(old_rev_id)

        # 首次确认显式选用已验收的新边界；正式计算只消费预约绑定的 TSB-APPT-1。
        confirmed = appointment_svc.first_confirm(appt_id, new_rev_id)
        formal = confirmed["formal_check"]
        assert formal["sample_batch_no"] == "TSB-APPT-1"
        assert formal["revision_id"] == new_rev_id
        assert formal["hit_count"] == 3  # 与 TSB-1 试算命中一致，不含补报点
        assert confirmed["appointment"]["巡检状态"] == "巡检中"
        assert confirmed["appointment"]["适用修订ID"] == new_rev_id

        # 重复首次确认被拒绝。
        with pytest.raises(ValueError, match="不能重复确认"):
            appointment_svc.first_confirm(appt_id, new_rev_id)

        # 重传更正后的乙围栏文件：甲的修订数量不变，乙首次产生合法修订。
        fixed_import = fence.import_fence_revision_file("rev_flow_20261124_fixed.json", [
            {"围栏编号": "JIA-FLOW", "顶点序列": JIA_NEW, "规则版本": "JIA-v2"},
            {"围栏编号": "YI-FLOW", "顶点序列": YI_FIXED, "规则版本": "YI-v2"},
        ])
        assert fixed_import["deployable_fence_ids"] == ["JIA-FLOW", "YI-FLOW"]
        assert len(fence.list_fence_revisions("JIA-FLOW")) == 2  # 旧+新，无重复修订
        yi_rev = next(r for r in fence.list_fence_revisions("YI-FLOW")
                      if r["状态"] == "待验收")
        assert yi_rev["合法性"] == "合法"

        # 对更正后的乙启动一份新试算：修复后可以正常命中。
        yi_trial = fence.trial_run_fence(
            "TSB-APPT-2", yi_rev["id"], run_no="TRIAL-YI-FIXED")
        assert yi_trial["hit_count"] == 2
        fence.accept_fence_revision(yi_rev["id"])

        # 第一次适用的围栏仍是新甲边界，且正式命中快照未被重传改变。
        record = appointment_svc.get_entry(appt_id)
        assert record["适用修订ID"] == new_rev_id
        assert record["正式命中数"] == "3"

        # 原文件再传一次仍然幂等：甲始终只有两次有效修订（旧边界、新边界）。
        fence.import_fence_revision_file("rev_flow_20261124.json", [
            {"围栏编号": "JIA-FLOW", "顶点序列": JIA_NEW, "规则版本": "JIA-v2"},
            {"围栏编号": "YI-FLOW", "顶点序列": YI_SELF_INTERSECT, "规则版本": "YI-v1"},
        ])
        assert len(fence.list_fence_revisions("JIA-FLOW")) == 2

    def test_illegal_or_unaccepted_revision_cannot_enter_formal_check(self):
        from app.services.geo_source import GeoSourceService
        from app.services.geofence_rule import GeofenceRuleService

        geo = GeoSourceService()
        fence = GeofenceRuleService()
        geo.ingest_collected_points("BATCH-GUARD", make_points())
        geo.create_trial_sample_batch("BATCH-GUARD", "TSB-GUARD")
        outcome = fence.import_fence_revision_file("guard.json", [
            {"围栏编号": "JIA-GUARD", "顶点序列": JIA_NEW, "规则版本": "JIA-v2"}])
        rev_id = outcome["results"][0]["revision_id"]

        # 未验收的合法修订不能进入正式计算。
        with pytest.raises(ValueError, match="独立验收"):
            fence.formal_check_fence("TSB-GUARD", rev_id)
        fence.accept_fence_revision(rev_id)
        formal = fence.formal_check_fence("TSB-GUARD", rev_id)
        assert formal["phase"] == "正式确认" and formal["hit_count"] == 3

    def test_appointment_defaults_to_still_valid_old_boundary(self):
        from app.services.geo_source import GeoSourceService
        from app.services.geofence_rule import GeofenceRuleService
        from app.services.inspection_appointment import InspectionAppointmentService

        geo = GeoSourceService()
        fence = GeofenceRuleService()
        appointment_svc = InspectionAppointmentService()
        geo.ingest_collected_points("BATCH-OLD", make_points())
        geo.create_trial_sample_batch("BATCH-OLD", "TSB-OLD")
        outcome = fence.import_fence_revision_file("old_only.json", [
            {"围栏编号": "JIA-OLD", "顶点序列": JIA_OLD, "规则版本": "JIA-v1"}])
        rev_id = outcome["results"][0]["revision_id"]
        fence.accept_fence_revision(rev_id)
        appt = appointment_svc.create_appointment({
            "预约编号": "APPT-OLD-01", "围栏编号": "JIA-OLD",
            "绑定修订": rev_id, "绑定样本批": "TSB-OLD"})
        # 不传修订即沿用仍有效的旧边界；旧框比新框多命中点 4。
        confirmed = appointment_svc.first_confirm(appt["id"])
        assert confirmed["appointment"]["适用修订ID"] == rev_id
        assert confirmed["formal_check"]["hit_count"] == 4


class TestGeometryValidation:
    def test_self_intersection_and_degenerate_polygons_are_rejected(self):
        from app.services.geofence_rule import GeofenceRuleService

        fence = GeofenceRuleService()
        bad = fence.validate_polygon(fence.parse_vertices(YI_SELF_INTERSECT))
        assert bad["valid"] is False and bad["self_intersections"]
        good = fence.validate_polygon(fence.parse_vertices(JIA_NEW))
        assert good["valid"] is True
        degenerate = fence.validate_polygon(fence.parse_vertices("39.9,116.4;39.92,116.4"))
        assert degenerate["valid"] is False
