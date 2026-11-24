"""电子围栏判定领域服务。

在原有进出判定基础上承担两条版本化链路：

1. 围栏修订导入：一次导入可携带多个围栏的边界修订，系统校验多边形合法性
   （自交、退化），按文件校验值幂等去重，合法修订进入待验收状态；
2. 试算与正式确认分离：围栏试算只消费绑定的试算样本批，正式确认
   （巡检预约首次确认）只消费预约绑定的样本批，两者互不串用。
   非法边界永远不能进入正式计算。
"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GeofenceRuleService(BaseModuleService):
    MODULE = "geofence_rule"
    ENTITY = "电子围栏"
    OPERATION_TERM = "判定"
    LIST_FIELDS = ["围栏编号", "围栏形状", "顶点序列", "判定方式", "进出条件", "生效时段", "命中次数", "规则版本"]
    REQUIRED_FIELDS = ["围栏编号", "围栏形状", "顶点序列"]
    STATUS_ORDER = ["草稿", "待生效", "已生效", "已停用"]
    ACTION_RULES = {"提交生效": "待生效", "确认生效": "已生效", "标记停用": "已停用"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 几何工具：顶点解析、多边形合法性（自交/退化）、射线法命中。
    # ------------------------------------------------------------------

    @staticmethod
    def parse_vertices(vertices_str: str) -> list[tuple[float, float]]:
        """把 ``lat,lon;lat,lon`` 形式的顶点序列解析为坐标元组列表。"""
        vertices: list[tuple[float, float]] = []
        for chunk in str(vertices_str).split(";"):
            parts = chunk.split(",")
            if len(parts) >= 2 and parts[0].strip() and parts[1].strip():
                vertices.append((float(parts[0]), float(parts[1])))
        return vertices

    @staticmethod
    def _orientation(a: tuple[float, float], b: tuple[float, float],
                     c: tuple[float, float]) -> float:
        """有向叉积：>0 左转，<0 右转，=0 共线。"""
        return ((b[0] - a[0]) * (c[1] - a[1])
                - (b[1] - a[1]) * (c[0] - a[0]))

    @classmethod
    def _on_segment(cls, a: tuple[float, float], b: tuple[float, float],
                    c: tuple[float, float]) -> bool:
        """共线前提下，点 c 是否落在闭线段 ab 上。"""
        return (cls._orientation(a, b, c) == 0
                and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
                and min(a[1], b[1]) <= c[1] <= max(a[1], b[1]))

    @classmethod
    def _segments_intersect(cls, a1: tuple[float, float], a2: tuple[float, float],
                            b1: tuple[float, float], b2: tuple[float, float]) -> bool:
        """判断两条闭线段是否相交（端点接触也算相交）。"""
        o1, o2 = cls._orientation(a1, a2, b1), cls._orientation(a1, a2, b2)
        o3, o4 = cls._orientation(b1, b2, a1), cls._orientation(b1, b2, a2)
        if o1 == 0 and cls._on_segment(a1, a2, b1):
            return True
        if o2 == 0 and cls._on_segment(a1, a2, b2):
            return True
        if o3 == 0 and cls._on_segment(b1, b2, a1):
            return True
        if o4 == 0 and cls._on_segment(b1, b2, a2):
            return True
        return (o1 > 0 > o2 or o1 < 0 < o2) and (o3 > 0 > o4 or o3 < 0 < o4)

    @classmethod
    def validate_polygon(cls, vertices: list[tuple[float, float]]) -> dict[str, Any]:
        """校验多边形边界：至少三个顶点且任意非相邻边不得相交。

        返回 ``{"valid": bool, "reason": str, "self_intersections": [...]}``。
        """
        if len(vertices) < 3:
            return {"valid": False,
                    "reason": f"多边形至少需要3个顶点，当前{len(vertices)}个",
                    "self_intersections": []}
        n = len(vertices)
        intersections: list[dict[str, Any]] = []
        for i in range(n):
            a1, a2 = vertices[i], vertices[(i + 1) % n]
            for j in range(i + 1, n):
                # 相邻边（含首尾闭合的那对）共享端点，属于正常邻接，跳过。
                if j == i + 1 or (i == 0 and j == n - 1):
                    continue
                b1, b2 = vertices[j], vertices[(j + 1) % n]
                if cls._segments_intersect(a1, a2, b1, b2):
                    intersections.append({"edge_a": [i, (i + 1) % n],
                                           "edge_b": [j, (j + 1) % n]})
        if intersections:
            return {"valid": False,
                    "reason": f"多边形边界存在{len(intersections)}处自交",
                    "self_intersections": intersections}
        return {"valid": True, "reason": "边界合法", "self_intersections": []}

    @staticmethod
    def _point_in_polygon(lat: float, lon: float, vertices: list[tuple[float, float]]) -> bool:
        """射线法判断点是否在多边形围栏内。"""
        n = len(vertices)
        inside = False
        j = n - 1
        for i in range(n):
            vi_lat, vi_lon = vertices[i]
            vj_lat, vj_lon = vertices[j]
            if ((vi_lon > lon) != (vj_lon > lon)) and \
               (lat < (vj_lat - vi_lat) * (lon - vi_lon) / (vj_lon - vi_lon) + vi_lat):
                inside = not inside
            j = i
        return inside

    # ------------------------------------------------------------------
    # 围栏修订文件导入：幂等、带几何校验、产出可下发围栏结论。
    # ------------------------------------------------------------------

    def import_fence_revision_file(self, file_name: str,
                                   fences: list[dict[str, Any]]) -> dict[str, Any]:
        """导入一次围栏边界修订文件。

        每个围栏条目需包含 ``围栏编号`` 与 ``顶点序列``，可选 ``围栏形状``、
        ``规则版本``、``生效时段``。同一文件按整体 SHA256 校验值幂等：
        重复导入直接返回首次导入结论，不会产生第二次有效修订。
        """
        if not fences:
            raise ValueError("导入文件中没有任何围栏修订条目")
        content = json.dumps(
            {"file_name": file_name, "fences": fences},
            ensure_ascii=False, sort_keys=True, default=str)
        file_checksum = self._compute_checksum(content)

        log_file = self._data_dir("fence_revisions") / "imports.jsonl"
        for previous in self._read_jsonl(log_file):
            if previous.get("file_checksum") == file_checksum:
                return {
                    "file_name": file_name,
                    "file_checksum": file_checksum,
                    "deduplicated": True,
                    "import_id": previous.get("import_id"),
                    "results": previous.get("results", []),
                    "deployable_fence_ids": previous.get("deployable_fence_ids", []),
                    "message": "文件已导入过，沿用首次导入结论，不产生新修订",
                }

        import_id = f"FIMP-{time.strftime('%Y%m%d%H%M%S')}-{file_checksum[:8]}"
        results: list[dict[str, Any]] = []
        deployable: list[str] = []
        revision_rows = store.rows("fence_revision")
        next_rid = max((int(r.get("id", 0)) for r in revision_rows), default=0) + 1

        for item in fences:
            fence_code = str(item.get("围栏编号", "")).strip()
            vertices_str = str(item.get("顶点序列", "")).strip()
            if not fence_code or not vertices_str:
                results.append({"围栏编号": fence_code or "未命名",
                                "legal": False, "reason": "缺少围栏编号或顶点序列"})
                continue
            vertices = self.parse_vertices(vertices_str)
            validation = self.validate_polygon(vertices)
            geometry_checksum = self._compute_checksum(vertices_str)
            # 同一围栏同一边界重复提交也不产生新的有效修订。
            duplicate = next((
                r for r in revision_rows
                if r.get("围栏编号") == fence_code
                and r.get("几何校验值") == geometry_checksum
                and r.get("状态") != "已驳回"
            ), None)
            base = {
                "围栏编号": fence_code,
                "顶点序列": vertices_str,
                "顶点数": len(vertices),
                "几何校验值": geometry_checksum,
                "self_intersections": validation["self_intersections"],
            }
            if not validation["valid"]:
                # 非法边界同样留痕，标记为已驳回，供后续守卫用真实ID拦截，
                # 且不进入可下发范围、不能试算、不能验收或正式计算。
                rejected = {
                    "id": next_rid,
                    "status": "已驳回",
                    "pending": False,
                    "abnormal": True,
                    "import_id": import_id,
                    "import_file": file_name,
                    "file_checksum": file_checksum,
                    "围栏编号": fence_code,
                    "围栏形状": item.get("围栏形状", "polygon"),
                    "顶点序列": vertices_str,
                    "判定方式": item.get("判定方式", "ray_casting"),
                    "进出条件": item.get("进出条件", "inside"),
                    "生效时段": item.get("生效时段", time.strftime("%Y-%m-%d")),
                    "规则版本": item.get("规则版本", f"rev-{next_rid}"),
                    "几何校验值": geometry_checksum,
                    "合法性": "非法",
                    "状态": "已驳回",
                    "驳回原因": validation["reason"],
                    "self_intersections": validation["self_intersections"],
                    "created_at": time.time(),
                }
                next_rid += 1
                revision_rows.append(rejected)
                base.update({
                    "legal": False, "reason": validation["reason"],
                    "revision_id": rejected["id"], "状态": "已驳回",
                })
                results.append(base)
                continue
            if duplicate is not None:
                base.update({
                    "legal": True, "reason": "与既有修订边界一致，未重复建修订",
                    "revision_id": duplicate["id"], "状态": duplicate["状态"],
                })
                results.append(base)
                deployable.append(fence_code)
                continue

            revision = {
                "id": next_rid,
                "status": "待验收",
                "pending": True,
                "abnormal": False,
                "import_id": import_id,
                "import_file": file_name,
                "file_checksum": file_checksum,
                "围栏编号": fence_code,
                "围栏形状": item.get("围栏形状", "polygon"),
                "顶点序列": vertices_str,
                "判定方式": item.get("判定方式", "ray_casting"),
                "进出条件": item.get("进出条件", "inside"),
                "生效时段": item.get("生效时段", time.strftime("%Y-%m-%d")),
                "规则版本": item.get("规则版本", f"rev-{next_rid}"),
                "几何校验值": geometry_checksum,
                "合法性": "合法",
                "状态": "待验收",
                "验收结论": "",
                "created_at": time.time(),
            }
            next_rid += 1
            revision_rows.append(revision)
            base.update({
                "legal": True, "reason": "边界合法，修订已进入待验收",
                "revision_id": revision["id"], "状态": "待验收",
            })
            results.append(base)
            deployable.append(fence_code)

        record = {
            "import_id": import_id,
            "file_name": file_name,
            "file_checksum": file_checksum,
            "imported_at": time.time(),
            "results": results,
            "deployable_fence_ids": deployable,
        }
        self._write_jsonl(log_file, [record])
        return {
            "file_name": file_name,
            "file_checksum": file_checksum,
            "deduplicated": False,
            "import_id": import_id,
            "results": results,
            "deployable_fence_ids": deployable,
        }

    def list_fence_revisions(self, fence_code: str | None = None) -> list[dict[str, Any]]:
        """查看围栏修订记录，可按围栏编号过滤。"""
        rows = store.rows("fence_revision")
        if fence_code is None:
            return list(rows)
        return [r for r in rows if r.get("围栏编号") == fence_code]

    def get_revision(self, revision_id: int) -> dict[str, Any] | None:
        return store.find("fence_revision", revision_id)

    def accept_fence_revision(self, revision_id: int,
                              conclusion: str = "独立验收通过") -> dict[str, Any]:
        """对合法修订执行独立验收，验收通过后才允许用于正式确认。"""
        revision = self.get_revision(revision_id)
        if revision is None:
            raise ValueError(f"围栏修订 {revision_id} 不存在")
        if revision.get("合法性") != "合法":
            raise ValueError(
                f"围栏 {revision.get('围栏编号')} 的修订边界不合法，不能通过验收")
        revision["状态"] = "已验收"
        revision["验收结论"] = conclusion
        revision["accepted_at"] = time.time()
        revision["pending"] = False
        return revision

    def deactivate_fence_revision(self, revision_id: int) -> dict[str, Any]:
        """停用旧边界；若仍有巡检中的预约依赖该规则则拒绝停用（有效规则不可打断）。"""
        revision = self.get_revision(revision_id)
        if revision is None:
            raise ValueError(f"围栏修订 {revision_id} 不存在")
        try:
            appointment_svc = self._safe_import(
                "app.services.inspection_appointment",
                "InspectionAppointmentService")()
            active = appointment_svc.active_appointments_for_revision(revision_id)
        except Exception:
            active = []
        if active:
            raise ValueError(
                f"修订 {revision_id} 仍被 {len(active)} 个进行中的巡检预约持有，不能停用")
        revision["状态"] = "已停用"
        revision["pending"] = False
        return revision

    # ------------------------------------------------------------------
    # 命中试算：只消费绑定的试算样本批，输出两阶段命中明细。
    # ------------------------------------------------------------------

    def _load_sample_points(self, sample_batch_no: str) -> list[dict[str, Any]]:
        geo_svc = self._safe_import(
            "app.services.geo_source", "GeoSourceService")()
        bundle = geo_svc.get_trial_sample_batch(sample_batch_no)
        return bundle["points"]

    def _evaluate_hits(self, points: list[dict[str, Any]],
                       vertices: list[tuple[float, float]]) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for p in points:
            lat, lon = float(p["lat"]), float(p["lon"])
            if self._point_in_polygon(lat, lon, vertices):
                hits.append({
                    "point_id": p.get("point_id"),
                    "lat": lat,
                    "lon": lon,
                    "timestamp": p.get("timestamp"),
                    "terminal": p.get("terminal"),
                })
        return hits

    def trial_run_fence(self, sample_batch_no: str, revision_id: int,
                        *, run_no: str | None = None) -> dict[str, Any]:
        """用试算样本批对指定围栏修订做空间命中试算。

        试算严格读取样本批冻结文件，不读取原采集批次，因此补报不会影响
        已绑定样本批的试算结果。非法边界直接拒绝试算落库。
        """
        revision = self.get_revision(revision_id)
        if revision is None:
            raise ValueError(f"围栏修订 {revision_id} 不存在")
        vertices = self.parse_vertices(revision["顶点序列"])
        validation = self.validate_polygon(vertices)
        if not validation["valid"]:
            raise ValueError(
                f"围栏 {revision.get('围栏编号')} 边界不合法（{validation['reason']}），"
                "不可进入试算/正式计算")
        points = self._load_sample_points(sample_batch_no)
        hits = self._evaluate_hits(points, vertices)
        run_id = run_no or f"TRIAL-{time.strftime('%Y%m%d%H%M%S')}-{revision_id}"
        result = {
            "run_id": run_id,
            "phase": "试算",
            "sample_batch_no": sample_batch_no,
            "revision_id": revision_id,
            "围栏编号": revision.get("围栏编号"),
            "revision_status": revision.get("状态"),
            "legal": True,
            "total_points": len(points),
            "hit_count": len(hits),
            "hits": hits,
            "ran_at": time.time(),
        }
        self._write_jsonl(
            self._data_dir("fence_trials") / f"{run_id}.jsonl", [result])
        return result

    def get_trial_run(self, run_id: str) -> dict[str, Any]:
        """回看某次试算的冻结结论。"""
        records = self._read_jsonl(
            self._data_dir("fence_trials") / f"{run_id}.jsonl")
        if not records:
            raise FileNotFoundError(f"试算 {run_id} 不存在")
        return records[0]

    def formal_check_fence(self, sample_batch_no: str,
                           revision_id: int) -> dict[str, Any]:
        """正式确认阶段的命中计算：只接受已验收的合法修订与绑定样本批。"""
        revision = self.get_revision(revision_id)
        if revision is None:
            raise ValueError(f"围栏修订 {revision_id} 不存在")
        if revision.get("合法性") != "合法":
            raise ValueError(
                f"围栏 {revision.get('围栏编号')} 修订边界不合法，不可进入正式计算")
        if revision.get("状态") != "已验收":
            raise ValueError(
                f"围栏修订 {revision_id} 当前状态为{revision.get('状态')}，"
                "需先通过独立验收才能用于正式确认")
        points = self._load_sample_points(sample_batch_no)
        vertices = self.parse_vertices(revision["顶点序列"])
        hits = self._evaluate_hits(points, vertices)
        result = {
            "phase": "正式确认",
            "sample_batch_no": sample_batch_no,
            "revision_id": revision_id,
            "围栏编号": revision.get("围栏编号"),
            "total_points": len(points),
            "hit_count": len(hits),
            "hits": hits,
            "ran_at": time.time(),
        }
        self._write_jsonl(
            self._data_dir("fence_trials") / "formal_checks.jsonl", [result])
        return result

    # ------------------------------------------------------------------
    # 兼容旧链路：按采集批次对围栏主数据做命中检测。
    # ------------------------------------------------------------------

    def check_points_against_fence(self, batch_key: str, fence_id: int = 1) -> dict[str, Any]:
        """读取采集批次定位点数据文件，按围栏规则对定位点执行进出判定，生成围栏命中数据集。"""
        fence = store.find(self.MODULE, fence_id)
        if fence is None:
            return {"error": f"围栏 {fence_id} 不存在"}

        # 读取归并后的定位点
        try:
            dedup_svc = self._safe_import("app.services.dedup_merge", "DedupMergeService")()
            points, _ = dedup_svc.get_merged_points(batch_key)
        except Exception:
            points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
            if not points_file.exists():
                raise FileNotFoundError(f"批次 {batch_key} 的定位点文件不存在")
            points = self._read_jsonl(points_file)

        if not points:
            return {"batch_key": batch_key, "hits": 0, "total_points": 0}

        # 解析围栏顶点
        vertices_str = fence.get("顶点序列", "39.9,116.4;39.92,116.4;39.92,116.42;39.9,116.42")
        vertices = self.parse_vertices(vertices_str)

        hits = []
        for p in points:
            lat = p.get("lat", 0)
            lon = p.get("lon", 0)
            inside = self._point_in_polygon(lat, lon, vertices)
            if inside:
                hits.append({
                    "point_id": p.get("point_id"),
                    "batch_key": batch_key,
                    "fence_id": fence_id,
                    "fence_name": fence.get("围栏编号"),
                    "lat": lat,
                    "lon": lon,
                    "timestamp": p.get("timestamp"),
                    "terminal": p.get("terminal"),
                    "hit_at": time.time(),
                })

        # 写入围栏命中数据集
        output_file = self._data_dir("geofence_hits") / f"{batch_key}_fence{fence_id}_hits.jsonl"
        written = self._write_jsonl(output_file, hits)

        # 更新命中次数（兼容种子数据中非数字值）
        try:
            current_hits = int(fence.get("命中次数", 0))
        except (ValueError, TypeError):
            current_hits = 0
        fence["命中次数"] = str(current_hits + written)

        return {
            "batch_key": batch_key,
            "fence_id": fence_id,
            "total_points": len(points),
            "hits": written,
            "output_file": str(output_file),
        }

    def get_fence_hits(self, batch_key: str, fence_id: int = 1) -> tuple[list[dict[str, Any]], str]:
        """读取批次在指定围栏的命中记录数据集文件。"""
        file_path = self._data_dir("geofence_hits") / f"{batch_key}_fence{fence_id}_hits.jsonl"
        hits = self._read_jsonl(file_path)
        return hits, str(file_path)
