"""定位源接入领域服务：采集定位点数据批次登记、采样频率与采集水位推进。"""
from __future__ import annotations

import json
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class GeoSourceService(BaseModuleService):
    MODULE = "geo_source"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次编号", "采集终端", "定位方式", "采样频率", "记录条数", "时间跨度", "采集水位", "批次校验值"]
    REQUIRED_FIELDS = ["批次编号", "采集终端", "定位方式"]
    STATUS_ORDER = ["草稿", "待校验", "已接入", "已拒收"]
    ACTION_RULES = {"提交校验": "待校验", "确认接入": "已接入", "标记拒收": "已拒收"}
    NEGATIVE_ACTIONS = []

    def onboard_batch(self, values: dict[str, Any], point_count: int = 50) -> dict[str, Any]:
        """接入新的定位点采集批次，生成模拟定位点序列并写入定位点数据集文件。"""
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"缺少必填字段: {missing}")
        batch_key = entry["批次编号"]
        # 写入采集位点
        self._write_checkpoint(f"geo_collect_{batch_key}", {
            "batch_key": batch_key,
            "offset": 0,
            "point_count": point_count,
            "status": "active",
            "created_at": time.time(),
        })
        # 生成模拟定位点并写入定位数据文件
        points = self._generate_sample_points(count=point_count)
        for p in points:
            p["batch_key"] = batch_key
            p["terminal"] = entry.get("采集终端", "UNKNOWN")
        output_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        written = self._write_jsonl(output_file, points)
        # 更新记录条数
        entry["记录条数"] = str(written)
        first_ts = min(p["timestamp"] for p in points)
        last_ts = max(p["timestamp"] for p in points)
        entry["时间跨度"] = f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(first_ts))} ~ {time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(last_ts))}"
        checksum = self._compute_checksum(json.dumps(points, default=str))
        entry["批次校验值"] = checksum
        entry["采集水位"] = f"已采集{written}条"
        return entry

    def confirm_batch(self, batch_id: int) -> dict[str, Any]:
        """确认接入采集批次，自动触发坐标标准化和网格索引生成。"""
        entry, msg = self.run_action(batch_id, "确认接入")
        if entry is None:
            return {"error": msg, "batch_id": batch_id}
        batch_key = entry["批次编号"]

        triggers = {}
        # 自动触发坐标标准化
        try:
            coord_svc = self._safe_import("app.services.coord_transform", "CoordTransformService")()
            coord_result = coord_svc.transform_batch(batch_key)
            triggers["coord_transform"] = coord_result
        except Exception as e:
            triggers["coord_transform"] = {"error": str(e)}

        # 自动触发点位归并
        try:
            dedup_svc = self._safe_import("app.services.dedup_merge", "DedupMergeService")()
            dedup_result = dedup_svc.dedup_batch(batch_key)
            triggers["dedup_merge"] = dedup_result
        except Exception as e:
            triggers["dedup_merge"] = {"error": str(e)}

        # 自动触发网格索引生成
        try:
            grid_svc = self._safe_import("app.services.grid_index", "GridIndexService")()
            grid_result = grid_svc.index_points_from_batch(batch_key)
            triggers["grid_index"] = grid_result
        except Exception as e:
            triggers["grid_index"] = {"error": str(e)}

        return {
            "batch_entry": entry,
            "batch_key": batch_key,
            "confirmed_at": time.time(),
            "auto_triggers": triggers,
        }

    def get_batch_points(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取指定采集批次的定位点数据文件，返回定位点记录列表和文件路径。"""
        file_path = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not file_path.exists():
            raise FileNotFoundError(f"采集批次 {batch_key} 的定位点数据文件不存在")
        points = self._read_jsonl(file_path)
        return points, str(file_path)

    def list_active_batches(self) -> list[dict[str, Any]]:
        """获取所有已接入的活跃采集批次列表。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("status") == "已接入"]

    # ------------------------------------------------------------------
    # 显式采集点接入与补报：围栏试算需要可回看的真实点，而不是随机模拟点。
    # ------------------------------------------------------------------

    def ingest_collected_points(
        self,
        batch_key: str,
        points: list[dict[str, Any]],
        *,
        terminal: str = "TERM-EXPLICIT",
        locate_method: str = "GPS",
    ) -> dict[str, Any]:
        """向采集批次写入显式提供的真实采集点（新接入或补报均可）。

        每个点按观测身份 ``point_id`` 落盘，补报时追加到同一批次文件，
        不改动此前已经冻结出去的试算样本批。
        """
        if not points:
            raise ValueError("采集点列表不能为空")
        rows = store.rows(self.MODULE)
        entry = next((r for r in rows if r.get("批次编号") == batch_key), None)
        is_new = entry is None
        if is_new:
            entry, missing = self.create_entry({
                "批次编号": batch_key,
                "采集终端": terminal,
                "定位方式": locate_method,
            })
            if entry is None:
                raise ValueError(f"缺少必填字段: {missing}")

        output_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        before = len(self._read_jsonl(output_file))
        records: list[dict[str, Any]] = []
        for seq, p in enumerate(points, start=1):
            record = dict(p)
            record.setdefault("point_id", before + seq)
            record["batch_key"] = batch_key
            record.setdefault("terminal", terminal)
            records.append(record)
        written = self._write_jsonl(output_file, records)
        all_points = self._read_jsonl(output_file)
        entry["记录条数"] = str(len(all_points))
        entry["批次校验值"] = self._compute_checksum(
            json.dumps(all_points, default=str, ensure_ascii=False))
        entry["采集水位"] = f"已采集{len(all_points)}条"
        self._write_checkpoint(f"geo_collect_{batch_key}", {
            "batch_key": batch_key,
            "offset": len(all_points),
            "point_count": len(all_points),
            "status": "active",
            "updated_at": time.time(),
        })
        return {
            "batch_key": batch_key,
            "is_new_batch": is_new,
            "appended_points": written,
            "total_points": len(all_points),
            "output_file": str(output_file),
        }

    # ------------------------------------------------------------------
    # 试算样本批：取样时刻冻结，后续原采集批次补报不影响已冻结的样本。
    # ------------------------------------------------------------------

    def create_trial_sample_batch(
        self,
        source_batch_key: str,
        sample_batch_no: str,
        *,
        point_ids: list[Any] | None = None,
        scope: dict[str, Any] | None = None,
        terminal: str = "TERM-EXPLICIT",
        locate_method: str = "GPS",
    ) -> dict[str, Any]:
        """从已有采集批次冻结一份试算样本批。

        - 按观测身份 ``point_id`` 保存实际进入试算的点；
        - 记录取样时刻的范围（时间跨度、经纬度边界、来源批次水位）；
        - 样本点复制到独立文件并计算校验值，原批次之后补报不改变样本批。
        """
        source_file = self._data_dir("geo_points") / f"{source_batch_key}.jsonl"
        if not source_file.exists():
            raise FileNotFoundError(
                f"采集批次 {source_batch_key} 的定位点数据文件不存在，无法取样")
        source_points = self._read_jsonl(source_file)
        if not source_points:
            raise ValueError(f"采集批次 {source_batch_key} 没有可取样的定位点")

        wanted = {pid for pid in point_ids} if point_ids is not None else None
        frozen: list[dict[str, Any]] = []
        for p in source_points:
            if wanted is None or p.get("point_id") in wanted:
                frozen.append(dict(p))
        if wanted is not None and len(frozen) != len(wanted):
            found = {p.get("point_id") for p in frozen}
            missing = sorted(str(x) for x in (wanted - found))
            raise ValueError(f"观测身份 {missing} 在采集批次 {source_batch_key} 中不存在")

        lats = [float(p["lat"]) for p in frozen]
        lons = [float(p["lon"]) for p in frozen]
        timestamps = [float(p.get("timestamp", 0.0)) for p in frozen]
        sample_scope = {
            "source_batch_key": source_batch_key,
            "source_point_count_at_sample": len(source_points),
            "time_range": {
                "start": min(timestamps),
                "end": max(timestamps),
            },
            "bounds": {
                "min_lat": min(lats),
                "max_lat": max(lats),
                "min_lon": min(lons),
                "max_lon": max(lons),
            },
        }
        if scope:
            sample_scope["extra"] = scope

        sample_file = self._data_dir("trial_samples") / f"{sample_batch_no}.jsonl"
        written = self._write_jsonl(sample_file, frozen)
        checksum = self._compute_checksum(
            json.dumps(frozen, default=str, ensure_ascii=False))

        sample_meta = {
            "sample_batch_no": sample_batch_no,
            "source_batch_key": source_batch_key,
            "point_count": written,
            "point_ids": [p.get("point_id") for p in frozen],
            "scope": sample_scope,
            "source_checksum": self._compute_checksum(
                json.dumps(source_points, default=str, ensure_ascii=False)),
            "sample_checksum": checksum,
            "sample_file": str(sample_file),
            "created_at": time.time(),
        }
        self._write_checkpoint(f"trial_sample_{sample_batch_no}", sample_meta)

        # 同步在定位源接入模块登记一条可列表回看的样本批记录。
        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "status": "已接入",
            "pending": False,
            "abnormal": False,
            "批次编号": sample_batch_no,
            "采集终端": terminal,
            "定位方式": locate_method,
            "采样频率": "试算样本批",
            "记录条数": str(written),
            "时间跨度": f"{sample_scope['time_range']['start']} ~ {sample_scope['time_range']['end']}",
            "采集水位": f"冻结{written}点/来源{source_batch_key}取样时{len(source_points)}点",
            "批次校验值": checksum,
            "记录类型": "试算样本批",
            "来源批次": source_batch_key,
            "取样范围": json.dumps(sample_scope, ensure_ascii=False, default=str),
        }
        rows.append(entry)
        return sample_meta

    def get_trial_sample_batch(self, sample_batch_no: str) -> dict[str, Any]:
        """按原编号回看试算样本批：返回冻结元数据与冻结点本身。

        即使原采集批次随后补报，这里读到的仍是取样时冻结的原点数、出处与校验值。
        """
        meta = self._read_checkpoint(f"trial_sample_{sample_batch_no}")
        sample_file = self._data_dir("trial_samples") / f"{sample_batch_no}.jsonl"
        if meta is None and not sample_file.exists():
            raise FileNotFoundError(f"试算样本批 {sample_batch_no} 不存在")
        points = self._read_jsonl(sample_file)
        if meta is None:  # 数据文件仍在、登记记录缺失时按文件重建可复查信息
            meta = {
                "sample_batch_no": sample_batch_no,
                "point_count": len(points),
                "point_ids": [p.get("point_id") for p in points],
                "sample_checksum": self._compute_checksum(
                    json.dumps(points, default=str, ensure_ascii=False)),
                "sample_file": str(sample_file),
                "rebuilt_from_file": True,
            }
        return {"meta": meta, "points": points}

    def list_trial_sample_batches(self) -> list[dict[str, Any]]:
        """列出全部试算样本批登记记录。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("记录类型") == "试算样本批"
        ]