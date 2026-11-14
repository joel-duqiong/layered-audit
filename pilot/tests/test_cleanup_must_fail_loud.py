"""清理失败必须机器判红 —— 独立审查（2026-11-15）确认的两条 HIGH。

本轮把 `db_query` 改成抛错，只完成了「错误可见」，没完成「判红」：
  · 文件层 run_cleanup 把异常降格成 deleted=-1 写进产物，**全仓库没有任何检查消费它**
  · 跨仓库探针清理失败只打印，main 无退出码 -> exit 0

两处造的数据都用**种子客户名**（demo-vip），残留守卫按 `ai_probe_` 前缀查不到。
于是「零残留、机器守」在这两条路上是假绿。这组测试锁住「失败必须非 0」。

另锁一条安全边界：基线缺失时 run_cleanup 绝不许把 {max_id_before} 当成 0 ——
那会让 `DELETE FROM t WHERE id > 0` 删光种子数据。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import file_http_probe as fhp  # noqa: E402
import xrepo_probe as xr  # noqa: E402


# ------------------------------------------------------- 文件层：deleted<0 判红

def test_cleanup_failure_is_detected() -> None:
    raw = {"_cleanup": {"results": [{"table": "shop_order", "deleted": -1,
                                     "error": "psql 失败"}]}}
    assert len(fhp.cleanup_failures(raw)) == 1


def test_successful_cleanup_is_not_flagged() -> None:
    raw = {"_cleanup": {"results": [{"table": "shop_order", "deleted": 2},
                                    {"table": "shop_ledger_entry", "deleted": 0}]}}
    assert fhp.cleanup_failures(raw) == []


def test_no_cleanup_section_is_not_flagged() -> None:
    assert fhp.cleanup_failures({}) == []


def test_missing_baseline_refuses_sql_instead_of_deleting_seed() -> None:
    """基线缺失 -> 拒绝执行。绝不能退化成 WHERE id > 0（删光种子）。"""
    cfg = {"db": {"container": "c", "user": "u", "database": "d"}}
    cleanups = [{"table": "shop_order", "id_column": "id",
                 "sql": "DELETE FROM {table} WHERE {id_column} > {max_id_before}"}]
    out = fhp.run_cleanup(cfg, cleanups, {})      # 基线字典是空的
    assert out[0]["deleted"] == -1
    assert "基线" in out[0]["error"]
    assert "id > 0" not in out[0]["sql"] or out[0].get("refused") is True


# --------------------------------------------------- 跨仓库：清理失败 / 定位不到

def test_failed_cleanup_statement_is_residue_risk() -> None:
    triggered = [{"status": 200, "order_id": 1193}]
    log = [{"sql": "DELETE FROM shop_order WHERE id = 1193", "ok": False, "error": "x"}]
    assert xr.residue_risks(triggered, log)


def test_successful_cleanup_is_not_residue_risk() -> None:
    triggered = [{"status": 200, "order_id": 1193}]
    log = [{"sql": "DELETE FROM shop_order WHERE id = 1193", "ok": True}]
    assert xr.residue_risks(triggered, log) == []


def test_http_200_without_id_is_residue_risk() -> None:
    """200 即已提交（@Transactional）。响应形状漂移导致定位不到 id，
    订单留在库里 —— 不许当成「没造数据」静默跳过。"""
    triggered = [{"status": 200, "order_id": None}]
    assert xr.residue_risks(triggered, [])


def test_non_200_without_id_is_not_residue_risk() -> None:
    """请求被拒（4xx/5xx）时没有订单，不算残留风险。"""
    assert xr.residue_risks([{"status": 500, "order_id": None}], []) == []


def test_no_trigger_at_all_is_not_residue_risk() -> None:
    assert xr.residue_risks([{"error": "connection refused"}], []) == []


def test_json_shape_drift_does_not_crash_trigger() -> None:
    """响应是 list / 标量时 .get 抛 AttributeError —— 订单已创建却崩在清理前。"""
    class _Resp:
        status_code = 200

        def json(self) -> Any:
            return [{"id": 1}]        # 不是 dict

    entry = xr._order_entry(_Resp())
    assert entry["status"] == 200
    assert entry["order_id"] is None


# --------------------------------------------- 文件层主流程：失败必须非 0 退出

def _run_file_dynamic(monkeypatch, tmp_path, http_raw: dict) -> int:
    """只替换重活（classpath / JVM harness / 产物目录），保留 main 的判定与退出逻辑。"""
    import json
    import file_dynamic as fd

    monkeypatch.setattr(sys, "argv", ["file_dynamic.py"])   # 别让 argparse 吃到 pytest 的参数
    cases = tmp_path / "file_cases.json"
    cases.write_text(json.dumps({"scenarios": [], "http": {"probes": []}}), encoding="utf-8")
    monkeypatch.setattr(fd, "cases_path", lambda _a: cases)
    monkeypatch.setattr(fd, "build_classpath", lambda: "")
    monkeypatch.setattr(fd, "run_harness", lambda _c, _cp: {"scenarios": []})
    monkeypatch.setattr(fd.file_http_probe, "run", lambda _cfg: http_raw)
    monkeypatch.setattr(fd.project, "artifact_dir", lambda: tmp_path)
    return fd.main()


def test_file_layer_exits_nonzero_when_cleanup_failed(monkeypatch, tmp_path) -> None:
    raw = {"_cleanup": {"results": [{"table": "shop_order", "deleted": -1, "error": "x"}]}}
    assert _run_file_dynamic(monkeypatch, tmp_path, raw) == 1


def test_file_layer_exits_zero_when_cleanup_succeeded(monkeypatch, tmp_path) -> None:
    raw = {"_cleanup": {"results": [{"table": "shop_order", "deleted": 2}]}}
    assert _run_file_dynamic(monkeypatch, tmp_path, raw) == 0


def test_file_layer_artifacts_written_even_when_cleanup_failed(monkeypatch, tmp_path) -> None:
    """判红不许把证据一起丢掉：产物必须先落盘再退出。"""
    raw = {"_cleanup": {"results": [{"table": "shop_order", "deleted": -1, "error": "x"}]}}
    assert _run_file_dynamic(monkeypatch, tmp_path, raw) == 1
    assert (tmp_path / "file_observation.json").exists()
