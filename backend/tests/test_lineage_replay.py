"""序号 23 迁移切换测试：指标血缘 ↔ 回放引擎的路径完整性联动。

覆盖：
  - 批量刷新重新核对上下游，断链记录不写「正常」
  - 同一批次只提升一次版本（幂等）
  - 迁移切换 replace / retain_old 两种取舍路径
  - 重复切换 / 并发切换只生效一次
  - 事务失步后回滚原版本
  - 断链版本的回放任务只生成验证失败报告，不能执行
  - 血缘状态与回放报告一致性、部署面板与迁移台账
"""
from __future__ import annotations

import copy
import threading

import pytest

from app.services import service_for
from app.store import store


def _lineage():
    return service_for("metric_lineage")


def _replay():
    return service_for("replay_engine")


@pytest.fixture(autouse=True)
def restore_store():
    """每个用例前后恢复存储，避免版本号与台账互相污染。"""
    tables = (
        "metric_lineage", "replay_engine", "migration_ledger",
        "lineage_revision", "state_store", "stream_source",
    )
    snapshot = {name: copy.deepcopy(store.rows(name)) for name in tables}
    registry = copy.deepcopy(_lineage()._switch_registry)
    yield
    for name, rows in snapshot.items():
        store.rows(name).clear()
        store.rows(name).extend(rows)
    _lineage()._switch_registry.clear()
    _lineage()._switch_registry.update(registry)


def _row(metric: str) -> dict:
    row = _lineage()._locate(metric_name=metric)
    assert row is not None
    return row


def test_refresh_rechecks_links_and_keeps_broken_status():
    result = _lineage().refresh_all_lineages(batch_id="case-links")
    assert result["refreshed"] == 3
    assert result["broken"] == 1
    assert result["normal"] == 2

    broken = _row("支付成功率")
    assert broken["status"] == "断链"
    assert broken["abnormal"] is True
    assert broken["_current_verified"] is False
    assert broken["_broken_links"][0]["name"] == "已下线源端旧样例"

    healthy = _row("订单实时成交额")
    assert healthy["status"] == "正常"
    assert healthy["_current_verified"] is True


def test_same_batch_bumps_version_only_once():
    first = _lineage().refresh_all_lineages(batch_id="case-idempotent")
    assert first["version_bumped"] == 3
    versions = {row["指标名称"]: row["版本号"] for row in store.rows("metric_lineage")}

    repeated = _lineage().refresh_all_lineages(batch_id="case-idempotent")
    assert repeated["version_bumped"] == 0
    assert all(record["bumped"] is False for record in repeated["records"])
    assert {row["指标名称"]: row["版本号"] for row in store.rows("metric_lineage")} == versions

    second = _lineage().refresh_all_lineages(batch_id="case-idempotent-2")
    assert second["version_bumped"] == 3


def test_refresh_action_never_marks_broken_as_normal():
    row = _row("支付成功率")
    updated, message = _lineage().run_action(int(row["id"]), "刷新血缘")
    assert updated is not None
    assert updated["status"] == "断链"
    assert "断链" in message


def test_switch_replace_uses_new_path_as_current_version():
    result = _lineage().switch_environment(
        metric_name="订单实时成交额", switch_key="case-replace", mode="replace",
        removed_upstreams=["流式源端样例2"], added_upstreams=["流式源端样例3"],
        removed_outputs=["结果落地样例2"], added_outputs=["结果落地样例3"],
    )
    assert result["ok"] is True
    assert result["applied"] is True
    assert result["current_verified"] is True
    row = _row("订单实时成交额")
    assert row["上游源端"] == "流式源端样例3"
    assert row["输出目标"] == "结果落地样例3"
    assert row["_current_version"] == result["version"]
    current = [v for v in row["_versions"] if v["current"]]
    assert len(current) == 1
    assert current[0]["版本号"] == result["version"]


def test_switch_retain_old_keeps_current_version_and_adds_revision():
    _lineage().verify_current_version("订单实时成交额")
    row_before = copy.deepcopy(_row("订单实时成交额"))
    result = _lineage().switch_environment(
        metric_name="订单实时成交额", switch_key="case-retain", mode="retain_old",
        removed_upstreams=["流式源端样例2"], added_upstreams=["流式源端样例3"],
    )
    assert result["ok"] is True
    assert result["status"] == "修订待生效"
    row = _row("订单实时成交额")
    assert row["版本号"] == row_before["版本号"]
    assert row["_current_version"] == row_before["_current_version"]
    assert row["上游源端"] == row_before["上游源端"]
    assert row["_pending_path"]["sources"] == ["流式源端样例3"]
    assert len(store.rows("lineage_revision")) == 1
    assert result["revision"] if "revision" in result else True


def test_switch_is_idempotent_and_concurrent_switch_applies_once():
    results: list[dict] = []
    lock = threading.Lock()

    def _run() -> None:
        outcome = _lineage().switch_environment(
            metric_name="订单实时成交额", switch_key="case-concurrent", mode="replace",
            removed_upstreams=["流式源端样例2"], added_upstreams=["流式源端样例3"],
        )
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=_run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 8
    applied = [r for r in results if r["applied"]]
    assert len(applied) == 1
    assert all(r["idempotent"] for r in results if not r["applied"])
    assert len(store.rows("migration_ledger")) == 1


def test_transaction_failure_rolls_back_original_version():
    _lineage().verify_current_version("订单实时成交额")
    before = copy.deepcopy(_row("订单实时成交额"))
    result = _lineage().switch_environment(
        metric_name="订单实时成交额", switch_key="case-rollback", mode="replace",
        removed_upstreams=["流式源端样例2"], added_upstreams=["流式源端样例3"],
        simulate_failure=True,
    )
    assert result["ok"] is False
    assert result["rolled_back"] is True
    row = _row("订单实时成交额")
    assert row["版本号"] == before["版本号"]
    assert row["_current_version"] == before["_current_version"]
    assert row["上游源端"] == before["上游源端"]
    ledger = store.rows("migration_ledger")
    assert ledger and ledger[-1]["状态"] == "已回滚"


def test_broken_lineage_blocks_replay_and_only_writes_failed_report():
    _lineage().refresh_all_lineages(batch_id="case-replay-broken")
    job = _replay().create_replay_job(
        "断链回放", "2", "任务拓扑样例2", lineage_metric="支付成功率",
    )
    assert job["ok"] is True
    assert job["executable"] is False
    assert job["status"] == "验证失败"
    assert job["broken_links"] == 1
    assert job["verification_report"]["verdict"] == "验证失败"

    executed = _replay().execute_replay(job["task_id"])
    assert executed["ok"] is False
    assert executed["executable"] is False
    task = store.find("replay_engine", job["task_id"])
    assert task["status"] == "验证失败"
    assert task["结果验证"] == "failed"


def test_healthy_lineage_allows_replay_with_recomputed_version():
    refreshed = _lineage().refresh_all_lineages(batch_id="case-replay-ok")
    version = next(r["version"] for r in refreshed["records"] if r["metric"] == "订单实时成交额")
    job = _replay().create_replay_job(
        "正常回放", "2", "任务拓扑样例2", lineage_metric="订单实时成交额",
    )
    assert job["executable"] is True
    assert job["lineage_version"] == version

    executed = _replay().execute_replay(job["task_id"])
    assert executed["ok"] is True
    assert executed["verification_report"]["lineage_version"] == version


def test_replay_blocks_when_lineage_breaks_after_job_created():
    job = _replay().create_replay_job(
        "后置断链回放", "2", "任务拓扑样例2", lineage_metric="订单实时成交额",
    )
    assert job["executable"] is True

    # 模拟上游删除：路径上的源端从数据仓库移除。
    source_rows = store.rows("stream_source")
    source_rows[:] = [row for row in source_rows if row.get("源端名称") != "流式源端样例2"]

    executed = _replay().execute_replay(job["task_id"])
    assert executed["ok"] is False
    assert executed["executable"] is False
    assert executed["verification_report"]["verdict"] == "验证失败"


def test_consistency_and_deployment_panel_expose_alignment():
    _lineage().refresh_all_lineages(batch_id="case-panel")
    _replay().create_replay_job("对齐回放", "2", "任务拓扑样例2", lineage_metric="订单实时成交额")
    _lineage().switch_environment(
        metric_name="风控拦截量", switch_key="case-panel-switch", mode="replace",
        removed_upstreams=["流式源端样例2"], added_upstreams=["已下线源端旧样例"],
    )

    consistency = _replay().lineage_consistency_check()
    assert consistency["checked"] >= 1
    assert consistency["mismatches"] == []

    panel = _lineage().deployment_panel()
    assert panel["sequence"] == 23
    assert panel["database_deployment"]["records"] == 3
    assert panel["database_deployment"]["current_versions"] == 3
    assert panel["replay_build"]["tasks"] >= 1

    ledger = _lineage().migration_ledger()
    assert ledger["switches"] >= 1
    assert ledger["ledger"][-1]["指标名称"] == "风控拦截量"
    assert ledger["versions"]
