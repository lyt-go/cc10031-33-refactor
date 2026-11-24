"""时序质量领域服务：维护时序完整率、重复点与越界点的质量校验报告与结论。
对时序数据执行质量检测校验并生成质量分数报告数据文件。
质量复验以单位标定批次台账为准，核对批量标定中各测点实际采用的换算规则版本。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SeriesQualityService(BaseModuleService):
    MODULE = "series_quality"
    ENTITY = "质量报告"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["报告编号", "测点编号", "统计窗口", "完整率", "重复点数", "越界点数", "校验规则", "质检结论"]
    REQUIRED_FIELDS = ["报告编号", "测点编号", "统计窗口"]
    STATUS_ORDER = ["草稿", "待质检", "质检中", "已出报告"]
    ACTION_RULES = {"启动质检": "待质检", "出具报告": "已出报告", "标记驳回": "已出报告"}
    # 数据校验操作：对传感器时序数据执行完整性重复度和越界三项质量校验生成质检报告
    NEGATIVE_ACTIONS = []

    def run_quality_check(self, report_id: int) -> dict[str, Any]:
        """对传感器时序数据执行完整性、重复度和越界三项质量校验并生成质检报告数据文件。"""
        report = store.find(self.MODULE, report_id)
        if report is None:
            return {"error": f"质量报告 {report_id} 不存在"}
        self.run_action(report_id, "启动质检")
        point_key = report["测点编号"]

        # 读取标定数据检测质量指标
        cal_file = self._data_dir("calibrated") / f"{point_key}_calibrated.jsonl"
        records = self._read_jsonl(cal_file)
        edge_file = self._data_dir("edge_samples") / f"{point_key}.jsonl"
        edge_records = self._read_jsonl(edge_file)

        total = len(records) + len(edge_records)
        if total == 0:
            report["完整率"] = "0"
            report["重复点数"] = "0"
            report["越界点数"] = "0"
            report["质检结论"] = "无数据"
            return {"report_id": report["报告编号"], "total_records": 0, "conclusion": "无数据"}

        # 1. 完整性检测：标定记录数 / 原始采样数
        completeness = round(len(records) / max(total, 1), 2)

        # 2. 重复点检测
        timestamps_seen = set()
        duplicate_count = 0
        # 数据产物输出：汇总质量分数数据写入质检报告JSONL数据文件
        for r in records:
            ts = r.get("timestamp", "")
            if ts and ts in timestamps_seen:
                duplicate_count += 1
            timestamps_seen.add(ts)

        # 3. 越界点检测
        out_of_range = 0
        for r in records:
            val = float(r.get("calibrated_value_V", r.get("raw_value_mV", 0)))
            if abs(val) > 1000:
                out_of_range += 1

        # 综合判定
        if completeness > 0.95 and duplicate_count == 0 and out_of_range == 0:
            conclusion = "合格"
        elif completeness > 0.7:
            conclusion = "待优化"
        else:
            conclusion = "不合格"

        report["完整率"] = f"{completeness:.2f}"
        report["重复点数"] = str(duplicate_count)
        report["越界点数"] = str(out_of_range)
        report["质检结论"] = conclusion
        self.run_action(report_id, "出具报告")

        # 将质检结果写入时序质量报告数据文件
        self._write_jsonl(
            self._data_dir("quality_reports") / f"{report['报告编号']}_quality.jsonl",
            [{"report_id": report["报告编号"], "point": point_key, "total_records": total,
              "completeness": completeness, "duplicates": duplicate_count,
              "out_of_range": out_of_range, "conclusion": conclusion, "checked_at": time.time()}]
        )
        return {"report_id": report["报告编号"], "point": point_key,
                "total_records": total, "completeness": completeness,
                "duplicate_count": duplicate_count, "out_of_range": out_of_range,
                "conclusion": conclusion}

    def batch_quality_check(self, point_keys: list[str]) -> dict[str, Any]:
        """批量对多个测点执行时序质量校验并生成汇总质量分数。"""
        results = []
        for pk in point_keys:
            rid = max((int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0) + 1
            self._auto_create_seed(self.MODULE, {
                "报告编号": f"QUALITY-{pk}-{rid}",
                "测点编号": pk,
                "统计窗口": time.strftime("%Y-%m-%d", time.localtime()),
                "完整率": "0",
                "重复点数": "0",
                "越界点数": "0",
                "校验规则": "完整性+重复+越界",
                "质检结论": "",
            })
            r = self.run_quality_check(rid)
            results.append(r)
        # 汇总质量分数统计
        qualified = sum(1 for r in results if r.get("conclusion") == "合格")
        self._write_jsonl(
            self._data_dir("quality_reports") / "batch_summary.jsonl",
            [{"total": len(results), "qualified": qualified, "at": time.time()}]
        )
        return {"total_checked": len(results), "qualified": qualified,
                "qualification_rate": round(qualified / max(len(results), 1), 2)}

    def recheck_calibrate_batch(self, point_keys: list[str]) -> dict[str, Any]:
        """质量复验：核对一次批量标定中各测点实际采用的换算规则版本。

        以标定批次台账落账的规则快照为准，只读比对标定结果文件中的实际采样，
        不重算也不覆盖已完成的换算；复验期间到达的规则更正不会替代已落账版本，
        未处理测点的规则出处随台账快照一并返回。
        """
        cal_svc = self._safe_import("app.services.unit_calibrate", "UnitCalibrateService")()
        recon = cal_svc.reconcile_batch(point_keys)
        checks = []
        for pk in point_keys:
            ledger_rules = recon["rules"].get(pk, [])
            used_versions = [r.get("rule_version") for r in ledger_rules]
            # 读取交给时序质量的实际采样：标定结果文件原样比对，不回写
            cal_file = self._data_dir("calibrated") / f"{pk}_calibrated.jsonl"
            records = self._read_jsonl(cal_file)
            file_versions = sorted({str(r.get("rule_version", "")) for r in records})
            produced = pk in recon["produced"]
            check: dict[str, Any] = {
                "point": pk,
                "produced": produced,
                "used_rule_versions": used_versions,
                "file_rule_versions": file_versions,
                "records": len(records),
                "calibrated_values": [r.get("calibrated_value_V") for r in records],
            }
            if produced:
                consistent = bool(records) and set(used_versions).issubset(set(file_versions))
                check["status"] = "一致" if consistent else "不一致"
                check["consistent"] = consistent
            else:
                reason = next((u["reason"] for u in recon["unproduced"] if u["point"] == pk),
                              "未产出")
                check["status"] = "未产出"
                check["reason"] = reason
                check["consistent"] = None
            checks.append(check)
        produced_checks = [c for c in checks if c["produced"]]
        return {
            "batch_key": recon["batch_key"],
            "reconciled": recon["reconciled"],
            "points_checked": len(checks),
            "consistent": all(c["consistent"] for c in produced_checks),
            "checks": checks,
        }