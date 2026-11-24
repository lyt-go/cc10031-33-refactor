"""单位标定重构专项测试：共用换算骨架、批次台账对账、质量复验与故障续跑。"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir():
    """将数据目录指向临时位置，避免污染 real data。"""
    with tempfile.TemporaryDirectory() as tmp:
        with patch("app.services.base.DATA_ROOT", Path(tmp)):
            yield Path(tmp)


def _write_samples(data_dir: Path, point: str, raw_value: float = 120.0) -> None:
    """写入测点边缘采样文件，原值默认 120。"""
    samples_dir = data_dir / "edge_samples"
    samples_dir.mkdir(parents=True, exist_ok=True)
    with open(samples_dir / f"{point}.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "point": point,
            "timestamp": "2026-09-01T10:00:00",
            "raw_value": raw_value,
        }, ensure_ascii=False) + "\n")


def _add_rule(cal_id: int, point: str, scale: str, offset: str = "0") -> None:
    """登记一条待标定的换算规则。"""
    from app.store import store
    store.rows("unit_calibrate").append({
        "id": cal_id, "标定编号": f"UNIT-{point}", "测点编号": point,
        "原始单位": "mV", "目标单位": "V", "换算系数": scale,
        "零点偏移": offset, "标定方式": "线性换算",
        "标定时间": "2026-09-01", "status": "待标定", "pending": True, "abnormal": False,
    })


def _add_quality_report(report_id: int, point: str) -> None:
    from app.store import store
    store.rows("series_quality").append({
        "id": report_id, "报告编号": f"QUAL-{point}", "测点编号": point,
        "统计窗口": "2026-09-01", "完整率": "0", "重复点数": "0",
        "越界点数": "0", "校验规则": "完整性+重复+越界", "质检结论": "",
        "status": "待质检", "pending": True, "abnormal": False,
    })


def _read_calibrated(data_dir: Path, point: str) -> list[dict]:
    cal_file = data_dir / "calibrated" / f"{point}_calibrated.jsonl"
    if not cal_file.exists():
        return []
    return [json.loads(line) for line in
            cal_file.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestSharedConversionSkeleton:
    """单点与批量入口共用系数读取与精度写法，两种入口产出稳定一致。"""

    def test_single_and_batch_entries_produce_stable_values(self, setup_data_dir):
        from app.services.unit_calibrate import UnitCalibrateService

        _write_samples(setup_data_dir, "SENS-C1", raw_value=120.0)
        _add_rule(301, "SENS-C1", "0.01")
        svc = UnitCalibrateService()

        single = svc.calibrate_values(301)
        assert single["records_calibrated"] == 1
        assert single["scale"] == 0.01 and single["offset"] == 0.0

        batch = svc.batch_calibrate(["SENS-C1"])
        assert batch["total_points"] == 1 and batch["executed"] == 1
        assert batch["results"][0]["scale"] == single["scale"]
        assert batch["results"][0]["offset"] == single["offset"]

        # 两种入口写出的换算值一致：120 × 0.01 = 1.2，统一 4 位小数精度
        records = _read_calibrated(setup_data_dir, "SENS-C1")
        assert len(records) == 2
        assert {r["calibrated_value_V"] for r in records} == {1.2}
        assert {r["raw_value_mV"] for r in records} == {120.0}
        assert len({r["rule_version"] for r in records}) == 1


class TestBatchReconcile:
    """整批独立对账：请求点、实际产出点、未产出点与真实原因。"""

    def test_reconcile_lists_unproduced_point_with_true_reason(self, setup_data_dir):
        from app.services.unit_calibrate import UnitCalibrateService

        _write_samples(setup_data_dir, "SENS-B1")
        _write_samples(setup_data_dir, "SENS-B2")
        _write_samples(setup_data_dir, "SENS-B0")  # 有采样但从始至终无可用标定
        _add_rule(311, "SENS-B1", "0.01")
        _add_rule(312, "SENS-B2", "0.01")
        svc = UnitCalibrateService()
        points = ["SENS-B1", "SENS-B2", "SENS-B0"]

        summary = svc.batch_calibrate(points)
        assert summary["total_points"] == 3
        assert summary["executed"] == 2  # 只看成功条数猜不出哪台未产出

        produced_before = {pk: len(_read_calibrated(setup_data_dir, pk)) for pk in points}

        # 已完成的批次上直接对账，不重做标定
        recon = svc.reconcile_batch(points)
        assert recon["reconciled"] is True
        assert recon["requested"] == points
        assert recon["produced"] == ["SENS-B1", "SENS-B2"]
        assert recon["produced_count"] == 2
        assert recon["unproduced_count"] == 1
        unproduced = {u["point"]: u for u in recon["unproduced"]}
        assert unproduced["SENS-B0"]["status"] == "未产出"
        assert "无可用标定记录" in unproduced["SENS-B0"]["reason"]
        # 已产出测点的规则出处可查
        assert recon["rules"]["SENS-B1"][0]["calibrate_id"] == "UNIT-SENS-B1"
        assert recon["rules"]["SENS-B1"][0]["scale"] == 0.01

        # 对账不重做标定：产出文件条数不变，未产出测点依旧没有结果文件
        assert {pk: len(_read_calibrated(setup_data_dir, pk)) for pk in points} == produced_before
        assert _read_calibrated(setup_data_dir, "SENS-B0") == []


class TestBatchResumeAfterFailure:
    """故障续跑：已完成的换算保持稳定，未处理测点按更正后的规则续跑。"""

    def test_resume_keeps_completed_and_picks_up_corrected_rule(self, setup_data_dir):
        from app.services.unit_calibrate import UnitCalibrateService
        from app.services.series_quality import SeriesQualityService
        from app.store import store

        # 两台测点原值均为 120，系数起初都是 0.01
        _write_samples(setup_data_dir, "SENS-R1", raw_value=120.0)
        _write_samples(setup_data_dir, "SENS-R2", raw_value=120.0)
        _add_rule(321, "SENS-R1", "0.01")
        _add_rule(322, "SENS-R2", "0.01")
        svc = UnitCalibrateService()
        points = ["SENS-R1", "SENS-R2"]

        # 模拟故障：第一台处理完成后服务中断
        original = svc.calibrate_values
        calls = []

        def faulty(calibrate_id):
            calls.append(calibrate_id)
            if len(calls) > 1:
                raise RuntimeError("模拟故障：标定中断")
            return original(calibrate_id)

        svc.calibrate_values = faulty
        with pytest.raises(RuntimeError):
            svc.batch_calibrate(points)
        del svc.calibrate_values  # 恢复实例方法

        # 故障后台账：R1 已产出，R2 待处理且规则出处可查
        recon = svc.reconcile_batch(points)
        assert recon["produced"] == ["SENS-R1"]
        assert len(recon["unproduced"]) == 1
        assert recon["unproduced"][0]["point"] == "SENS-R2"
        assert recon["unproduced"][0]["status"] == "待处理"
        assert "尚未处理" in recon["unproduced"][0]["reason"]
        planned = recon["rules"]["SENS-R2"]
        assert planned[0]["scale"] == 0.01
        r2_v1 = planned[0]["rule_version"]

        r1_records = _read_calibrated(setup_data_dir, "SENS-R1")
        assert [r["calibrated_value_V"] for r in r1_records] == [1.2]
        r1_v1 = r1_records[0]["rule_version"]
        assert r1_v1 != r2_v1  # 不同测点的规则版本各自独立

        # 未处理测点的规则出处在复验中同样可查
        q_svc = SeriesQualityService()
        pre_recheck = q_svc.recheck_calibrate_batch(points)
        pre = {c["point"]: c for c in pre_recheck["checks"]}
        assert pre["SENS-R2"]["produced"] is False
        assert pre["SENS-R2"]["used_rule_versions"] == [r2_v1]

        # 恢复前收到规则更正：第二台系数 0.01 → 0.02
        store.find("unit_calibrate", 322)["换算系数"] = "0.02"

        # 继续运行：同一批请求重入，从台账续跑
        resumed = svc.batch_calibrate(points)
        assert resumed["total_points"] == 2
        assert resumed["executed"] == 2
        by_point = {r["point"]: r for r in resumed["results"]}
        assert by_point["SENS-R1"]["scale"] == 0.01
        assert by_point["SENS-R2"]["scale"] == 0.02

        # R1 沿用台账结果不重算：文件仍只有一条，值与版本稳定
        r1_after = _read_calibrated(setup_data_dir, "SENS-R1")
        assert len(r1_after) == 1
        assert r1_after[0]["calibrated_value_V"] == 1.2
        assert r1_after[0]["rule_version"] == r1_v1

        # R2 按更正后的规则续跑：120 × 0.02 = 2.4，规则版本随之变化
        r2_after = _read_calibrated(setup_data_dir, "SENS-R2")
        assert len(r2_after) == 1
        assert r2_after[0]["calibrated_value_V"] == 2.4
        r2_v2 = r2_after[0]["rule_version"]
        assert r2_v2 != r2_v1

        # 恢复后交给时序质量的实际采样与规则版本
        _add_quality_report(331, "SENS-R1")
        _add_quality_report(332, "SENS-R2")
        q1 = q_svc.run_quality_check(331)
        q2 = q_svc.run_quality_check(332)
        assert q1["total_records"] == 2 and q1["duplicate_count"] == 0
        assert q2["total_records"] == 2 and q2["duplicate_count"] == 0
        recheck = q_svc.recheck_calibrate_batch(points)
        assert recheck["consistent"] is True
        checks = {c["point"]: c for c in recheck["checks"]}
        assert checks["SENS-R1"]["used_rule_versions"] == [r1_v1]
        assert checks["SENS-R1"]["calibrated_values"] == [1.2]
        assert checks["SENS-R2"]["used_rule_versions"] == [r2_v2]
        assert checks["SENS-R2"]["calibrated_values"] == [2.4]

        # 对照两种入口的稳定值：单点入口对 R1 的换算与批次台账一致
        single = svc.calibrate_values(321)
        assert single["scale"] == 0.01
        r1_final = _read_calibrated(setup_data_dir, "SENS-R1")
        assert {r["calibrated_value_V"] for r in r1_final} == {1.2}
        assert {r["rule_version"] for r in r1_final} == {r1_v1}


class TestQualityRecheck:
    """质量复验：按批次台账核对各测点规则版本，复验只读不改写。"""

    def test_recheck_uses_ledgered_rule_versions(self, setup_data_dir, monkeypatch):
        from app.services.unit_calibrate import UnitCalibrateService
        from app.services.series_quality import SeriesQualityService
        from app.store import store

        _write_samples(setup_data_dir, "SENS-Q1", raw_value=120.0)
        _write_samples(setup_data_dir, "SENS-Q2", raw_value=120.0)
        _add_rule(341, "SENS-Q1", "0.01")
        _add_rule(342, "SENS-Q2", "0.01")
        cal_svc = UnitCalibrateService()
        points = ["SENS-Q1", "SENS-Q2"]
        cal_svc.batch_calibrate(points)

        files_before = {
            pk: (setup_data_dir / "calibrated" / f"{pk}_calibrated.jsonl").read_text(encoding="utf-8")
            for pk in points
        }
        v1 = {pk: _read_calibrated(setup_data_dir, pk)[0]["rule_version"] for pk in points}

        q_svc = SeriesQualityService()
        # 模拟复验处理到第二台时恰好收到规则更正（0.01 → 0.02）
        original_read = q_svc._read_jsonl

        def correcting_read(filepath):
            if Path(filepath).name == "SENS-Q2_calibrated.jsonl":
                store.find("unit_calibrate", 342)["换算系数"] = "0.02"
            return original_read(filepath)

        monkeypatch.setattr(q_svc, "_read_jsonl", correcting_read)
        recheck = q_svc.recheck_calibrate_batch(points)

        # 规则更正确实在复验期间到达
        assert store.find("unit_calibrate", 342)["换算系数"] == "0.02"
        assert recheck["consistent"] is True
        checks = {c["point"]: c for c in recheck["checks"]}
        # 已完成的换算仍按落账版本核对，不被复验期间到达的新版替代
        assert checks["SENS-Q1"]["used_rule_versions"] == [v1["SENS-Q1"]]
        assert checks["SENS-Q2"]["used_rule_versions"] == [v1["SENS-Q2"]]
        assert checks["SENS-Q2"]["file_rule_versions"] == [v1["SENS-Q2"]]
        assert checks["SENS-Q2"]["calibrated_values"] == [1.2]
        # 复验只读：标定结果文件内容不变
        for pk in points:
            after = (setup_data_dir / "calibrated" / f"{pk}_calibrated.jsonl").read_text(encoding="utf-8")
            assert after == files_before[pk]
