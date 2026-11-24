"""单位标定领域服务：维护传感器原始值到目标计量单位的换算系数与零点偏移标定。
对标定记录进行单位转换计算并写入标定结果数据文件。

换算规则、计算与记账共用一份骨架：单点与批量入口读取同一份系数、写同一种
精度；批量入口额外把每个测点的规则快照与产出状态落入批次台账（检查点），
支持故障续跑、整批独立对账，并供质量复验核对各测点实际采用的规则版本。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 标定结果统一精度：换算值保留 4 位小数
CALIBRATE_PRECISION = 4
# 可参与换算的标定记录状态
USABLE_RULE_STATUS = ("待标定", "已标定")


class UnitCalibrateService(BaseModuleService):
    MODULE = "unit_calibrate"
    ENTITY = "标定记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["标定编号", "测点编号", "原始单位", "目标单位", "换算系数", "零点偏移", "标定方式", "标定时间"]
    REQUIRED_FIELDS = ["标定编号", "测点编号", "原始单位"]
    STATUS_ORDER = ["草稿", "待标定", "已标定", "已失效"]
    ACTION_RULES = {"提交标定": "待标定", "确认标定": "已标定", "标记失效": "已失效"}
    NEGATIVE_ACTIONS = ["标记失效"]

    # -- 共用换算骨架：系数读取、精度写法、规则版本 --

    def _read_coefficients(self, entry: dict[str, Any]) -> tuple[float, float]:
        """系数读取：从标定记录解析换算系数与零点偏移，缺省按 1/0。"""
        scale = float(entry.get("换算系数") or "1")
        offset = float(entry.get("零点偏移") or "0")
        return scale, offset

    def _convert_value(self, raw: float, scale: float, offset: float) -> float:
        """精度写法：原始值 × 换算系数 + 零点偏移，统一保留 4 位小数。"""
        return round(raw * scale + offset, CALIBRATE_PRECISION)

    def _rule_version(self, entry: dict[str, Any], scale: float, offset: float) -> str:
        """规则版本：由标定编号与系数内容生成，规则更正后版本随之变化。"""
        basis = "|".join([
            str(entry.get("标定编号", "")), str(scale), str(offset),
            str(entry.get("原始单位", "")), str(entry.get("目标单位", "")),
        ])
        return f"rv-{self._compute_checksum(basis)}"

    def _rule_snapshot(self, entry: dict[str, Any]) -> dict[str, Any]:
        """规则快照：记录一次换算实际采用的系数、零点偏移与规则版本。"""
        scale, offset = self._read_coefficients(entry)
        return {
            "calibrate_id": entry.get("标定编号"),
            "scale": scale,
            "offset": offset,
            "rule_version": self._rule_version(entry, scale, offset),
        }

    def _find_usable_rules(self, point_key: str) -> list[dict[str, Any]]:
        """换算规则出处：查找测点当前可参与换算的标定记录。"""
        return [row for row in store.rows(self.MODULE)
                if row.get("测点编号") == point_key and row.get("status") in USABLE_RULE_STATUS]

    def _build_calibrated_record(
        self, sample: dict[str, Any], scale: float, offset: float,
        calibrate_id: str, rule_version: str,
    ) -> dict[str, Any]:
        """按统一精度写法生成一条标定结果记录，并携带换算使用的规则版本。"""
        raw = float(sample.get("raw_value", 0))
        return {
            "point": sample["point"],
            "timestamp": sample["timestamp"],
            "raw_value_mV": raw,
            "calibrated_value_V": self._convert_value(raw, scale, offset),
            "scale": scale,
            "offset": offset,
            "rule_version": rule_version,
            # 数据转换输出：将标定计算结果写入持久化标定数据JSONL文件存储
            "calibrate_id": calibrate_id,
        }

    def calibrate_values(self, calibrate_id: int) -> dict[str, Any]:
        """对标定记录的原始数据进行单位换算计算并写入标定结果归档文件。"""
        cal = store.find(self.MODULE, calibrate_id)
        if cal is None:
            return {"error": f"标定记录 {calibrate_id} 不存在"}
        entry, _ = self.run_action(calibrate_id, "确认标定")
        point_key = entry["测点编号"]
        scale, offset = self._read_coefficients(entry)
        rule_version = self._rule_version(entry, scale, offset)
        # 读取对应测点的边缘采样数据
        samples_file = self._data_dir("edge_samples") / f"{point_key}.jsonl"
        samples = self._read_jsonl(samples_file)
        # 执行单位标定计算：原始值 × 换算系数 + 零点偏移
        calibrated = [
            self._build_calibrated_record(s, scale, offset, entry["标定编号"], rule_version)
            for s in samples
        ]
        # 将标定结果写入持久化标定数据文件
        output_file = self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"
        written = self._write_jsonl(output_file, calibrated)
        entry["标定时间"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        return {"calibrate_id": entry["标定编号"], "point": point_key,
                "records_calibrated": written, "scale": scale, "offset": offset}

    # -- 批次台账（记账）：按批记录每个测点的规则快照与产出状态 --

    def _batch_key(self, point_keys: list[str]) -> str:
        """批次键：由请求测点列表生成，同一批请求对应同一份台账。"""
        return f"BATCH-{self._compute_checksum('|'.join(point_keys))}"

    def _ledger_name(self, batch_key: str) -> str:
        return f"calibrate_batch_{batch_key}"

    def _new_ledger_entry(self, point_key: str) -> dict[str, Any]:
        """为请求测点登记台账条目：记录当前规则出处快照，尚无产出。"""
        rules = self._find_usable_rules(point_key)
        return {
            "point": point_key,
            "status": "待处理",
            "reason": "" if rules else "无可用标定记录",
            "rules": [self._rule_snapshot(r) for r in rules],
            "results": [],
            "completed_at": None,
        }

    def _load_batch_ledger(self, point_keys: list[str]) -> dict[str, Any]:
        """读取批次台账；首次执行时为每个请求点登记规则出处。"""
        batch_key = self._batch_key(point_keys)
        ledger = self._read_checkpoint(self._ledger_name(batch_key))
        if ledger is None:
            ledger = {"batch_key": batch_key, "points": list(point_keys),
                      "entries": {}, "started_at": time.time()}
        entries = ledger.setdefault("entries", {})
        for pk in point_keys:
            if pk not in entries:
                entries[pk] = self._new_ledger_entry(pk)
        return ledger

    def _save_batch_ledger(self, ledger: dict[str, Any]) -> None:
        self._write_checkpoint(self._ledger_name(ledger["batch_key"]), ledger)

    def batch_calibrate(self, point_keys: list[str]) -> dict[str, Any]:
        """批量执行多个测点的单位标定转换，返回标定汇总数据集。

        每完成一台测点就把规则快照与产出结果落入批次台账；故障后重入同一批
        请求时，已产出测点直接沿用台账结果不重算，未产出测点按当前规则续跑。
        """
        ledger = self._load_batch_ledger(point_keys)
        # 批次一登记先落账，未处理测点的规则出处即可查
        self._save_batch_ledger(ledger)
        results: list[dict[str, Any]] = []
        for pk in point_keys:
            entry = ledger["entries"][pk]
            if entry["status"] == "已产出":
                # 已完成的换算沿用台账记录，不被后续规则版本替代
                results.extend(entry["results"])
                continue
            # 续跑前重新解析规则出处，吸收待处理期间收到的规则更正
            rules = self._find_usable_rules(pk)
            entry["rules"] = [self._rule_snapshot(r) for r in rules]
            if not rules:
                entry["status"] = "未产出"
                entry["reason"] = "无可用标定记录"
                self._save_batch_ledger(ledger)
                continue
            point_results = [self.calibrate_values(r["id"]) for r in rules]
            entry["status"] = "已产出"
            entry["reason"] = ""
            entry["results"] = point_results
            entry["completed_at"] = time.time()
            # 每台落账一次，故障中断后可从台账续跑
            self._save_batch_ledger(ledger)
            results.extend(point_results)
        # 写入批量标定汇总日志文件
        self._write_jsonl(
            self._data_dir("calibrated") / "batch_calibrate_summary.jsonl",
            [{"action": "batch_calibrate", "points": len(point_keys),
              "executed": len(results), "at": time.time()}]
        )
        return {"total_points": len(point_keys), "executed": len(results), "results": results}

    def reconcile_batch(self, point_keys: list[str]) -> dict[str, Any]:
        """整批独立对账：直接读取批次台账，核对请求点、实际产出点与未产出点。

        只读台账不重做标定；未产出测点给出真实原因，未处理测点的规则出处
        以台账最近登记的规则快照为准，可查可溯。
        """
        batch_key = self._batch_key(point_keys)
        ledger = self._read_checkpoint(self._ledger_name(batch_key))
        if ledger is None:
            return {
                "batch_key": batch_key,
                "reconciled": False,
                "requested": list(point_keys),
                "produced": [],
                "unproduced": [{"point": pk, "status": "无台账",
                                "reason": "批次台账不存在，尚未执行批量标定"}
                               for pk in point_keys],
                "rules": {},
            }
        entries = ledger.get("entries", {})
        produced: list[str] = []
        unproduced: list[dict[str, Any]] = []
        rules: dict[str, Any] = {}
        for pk in point_keys:
            entry = entries.get(pk)
            if entry is None:
                unproduced.append({"point": pk, "status": "无台账",
                                   "reason": "测点不在批次台账中"})
                continue
            rules[pk] = entry.get("rules") or []
            if entry.get("status") == "已产出":
                produced.append(pk)
            else:
                unproduced.append({
                    "point": pk,
                    "status": entry.get("status", "待处理"),
                    "reason": entry.get("reason") or "批次中断，测点尚未处理",
                })
        return {
            "batch_key": batch_key,
            "reconciled": True,
            "requested": list(point_keys),
            "produced": produced,
            "unproduced": unproduced,
            "produced_count": len(produced),
            "unproduced_count": len(unproduced),
            "rules": rules,
        }
