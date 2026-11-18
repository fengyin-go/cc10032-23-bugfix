"""回放引擎领域服务：消费检查点数据执行数据回放与恢复验证。

数据流: metric_lineage（当前可验证版本）→ checkpoint_mgr（检查点管理）→ replay_engine（回放引擎）→ restore_plan（故障恢复）
核心逻辑: 检查点快照回放、状态恢复验证、位点对齐、回放进度管理。

序号 23 修复要点：
- 建任务时引用指标血缘「本次重算的当前版本」并重新核对全部上下游链接；
- 断链版本只生成验证失败报告，任务标记为不可执行，不能进入回放；
- 执行回放前再次校验血缘，执行后把回放报告与血缘版本绑定，供页面核对一致。
"""
from __future__ import annotations

import copy
import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.metric_lineage import MetricLineageService
from app.store import store


class ReplayEngineService(BaseModuleService):
    MODULE = "replay_engine"
    ENTITY = "回放任务"
    LIST_FIELDS = ["任务名称", "源检查点", "目标拓扑", "回放模式", "起始位点", "结果验证", "进度"]
    REQUIRED_FIELDS = ["任务名称", "源检查点", "回放模式"]
    STATUS_ORDER = ["草稿", "待回放", "回放中", "已完成", "验证失败"]
    ACTION_RULES = {"启动回放": "待回放", "暂停回放": "回放中", "标记完成": "已完成"}
    NEGATIVE_ACTIONS = ["暂停回放"]

    def __init__(self) -> None:
        self._lineage = MetricLineageService()

    def create_replay_job(
        self,
        job_name: str,
        checkpoint_id: str,
        topology_name: str,
        mode: str = "full_replay",
        lineage_metric: str | None = None,
    ) -> dict[str, Any]:
        """基于检查点创建数据回放任务。

        消费 checkpoint_mgr 中的检查点快照，结合 task_topology 的拓扑配置；
        同时引用 metric_lineage 的当前可验证版本——断链版本会生成
        「验证失败」任务并阻止其进入可执行回放。
        """
        # 1. 验证检查点存在
        cp_row = None
        for r in store.rows("checkpoint_mgr"):
            cid = r.get("id", "")
            if str(cid) == str(checkpoint_id):
                cp_row = r
                break
        if cp_row is None:
            return {"ok": False, "error": f"检查点「{checkpoint_id}」不存在"}

        if cp_row.get("status") != "已启用":
            return {"ok": False, "error": f"检查点「{checkpoint_id}」未启用"}

        # 2. 验证拓扑配置
        topo_found = any(
            r.get("拓扑名称") == topology_name
            for r in store.rows("task_topology")
        )
        if not topo_found and topology_name:
            return {"ok": False, "error": f"拓扑配置「{topology_name}」不存在"}

        # 3. 核对指标血缘当前版本（本次重算结果）
        lineage_result = self._lineage.verify_current_version(lineage_metric) \
            if lineage_metric else {
                "ok": False,
                "verified": False,
                "error": "回放任务未引用指标血缘版本，无法确认链路完整性",
            }

        lineage_ok = bool(lineage_result.get("verified"))
        lineage_found = bool(lineage_result.get("ok"))

        # 4. 创建回放任务
        task_id = max(
            (int(r.get("id", 0)) for r in store.rows(self.MODULE)), default=0
        ) + 1
        replay_task = {
            "id": task_id,
            "任务名称": job_name,
            "源检查点": str(checkpoint_id),
            "目标拓扑": topology_name,
            "回放模式": mode,
            "起始位点": cp_row.get("timestamp", cp_row.get("snapshot_ts", time.time() - 3600)),
            "结果验证": "待验证",
            "进度": "0%",
            "status": "待回放",
            "pending": True,
            "abnormal": False,
            "_checkpoint_snapshot": copy.deepcopy(cp_row),
            "_created_at": time.time(),
            "_lineage_metric": lineage_metric or "",
            "_lineage_version": lineage_result.get("current_version", ""),
            "_lineage_snapshot": {
                "verified": lineage_ok,
                "status": lineage_result.get("status", ""),
                "signature": lineage_result.get("signature", ""),
                "path": lineage_result.get("path", {}),
                "checks": lineage_result.get("checks", []),
                "broken": lineage_result.get("broken", []),
                "broken_links": lineage_result.get("broken_links", 0),
                "verified_at": lineage_result.get("verified_at", ""),
                "error": lineage_result.get("error", ""),
            },
            "_executable": lineage_ok,
        }

        if not lineage_ok:
            reason = (
                lineage_result.get("error")
                or f"指标血缘当前版本存在 {lineage_result.get('broken_links', 0)} 处断链，不可执行回放"
            )
            replay_task.update({
                "status": "验证失败",
                "pending": False,
                "abnormal": True,
                "结果验证": "failed",
                "进度": "0%",
                "_blocked_reason": reason,
                "_replay_report": {
                    "task_id": task_id,
                    "task_name": job_name,
                    "verdict": "验证失败",
                    "executable": False,
                    "reason": reason,
                    "lineage_metric": lineage_metric or "",
                    "lineage_version": replay_task["_lineage_version"],
                    "broken_links": lineage_result.get("broken_links", 0),
                    "broken": copy.deepcopy(lineage_result.get("broken", [])),
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
            })
        store.rows(self.MODULE).append(replay_task)

        return {
            "ok": True,
            "task_id": task_id,
            "task_name": job_name,
            "checkpoint_id": checkpoint_id,
            "topology": topology_name,
            "mode": mode,
            "lineage_metric": lineage_metric or "",
            "lineage_version": replay_task["_lineage_version"],
            "lineage_verified": lineage_ok,
            "executable": lineage_ok,
            "status": replay_task["status"],
            "blocked_reason": replay_task.get("_blocked_reason", ""),
            "broken_links": lineage_result.get("broken_links", 0),
            "broken": copy.deepcopy(lineage_result.get("broken", [])),
            "verification_report": copy.deepcopy(replay_task.get("_replay_report")),
        }

    def _lineage_still_valid(self, task: dict[str, Any]) -> dict[str, Any]:
        """回放前重新核对任务引用的血缘当前版本是否仍完整。"""
        metric = task.get("_lineage_metric", "")
        if not metric:
            return {"ok": False, "verified": False,
                    "error": "任务未引用指标血缘版本"}
        return self._lineage.verify_current_version(metric)

    def execute_replay(self, task_id: int) -> dict[str, Any]:
        """执行回放任务：检查点状态快照回放 + 结果验证。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "error": "回放任务不存在"}

        if task.get("_executable") is False:
            report = self._write_failed_report(task, task.get("_blocked_reason", "血缘断链"))
            return {
                "ok": False,
                "task_id": task_id,
                "executable": False,
                "status": task.get("status"),
                "message": "回放任务因指标血缘路径不完整被阻止，仅生成验证失败报告",
                "verification_report": report,
            }

        if task.get("status") not in ("待回放", "回放中"):
            return {"ok": False, "error": f"任务状态为「{task.get('status')}」, 不可回放"}

        # 执行前再次核对血缘，避免刷新后断链的任务继续回放。
        lineage_check = self._lineage_still_valid(task)
        if not lineage_check.get("verified"):
            reason = (
                lineage_check.get("error")
                or f"指标血缘当前版本存在 {lineage_check.get('broken_links', 0)} 处断链"
            )
            task["_executable"] = False
            task["_blocked_reason"] = reason
            task["_lineage_snapshot"] = {
                "verified": False,
                "status": lineage_check.get("status", ""),
                "signature": lineage_check.get("signature", ""),
                "path": lineage_check.get("path", {}),
                "checks": lineage_check.get("checks", []),
                "broken": lineage_check.get("broken", []),
                "broken_links": lineage_check.get("broken_links", 0),
                "verified_at": lineage_check.get("verified_at", ""),
            }
            report = self._write_failed_report(task, reason)
            return {
                "ok": False,
                "task_id": task_id,
                "executable": False,
                "status": task.get("status"),
                "message": "指标血缘在执行前已断链，回放被阻止",
                "verification_report": report,
            }

        task["status"] = "回放中"

        # 获取检查点快照中的状态数据
        cp_snapshot = task.get("_checkpoint_snapshot", {})
        state_data = cp_snapshot.get("state", cp_snapshot.get("operator_states", {}))

        # 执行回放：对每个算子状态复制到 state_store
        replay_results = []
        if state_data:
            for op_name, op_state in state_data.items():
                state_entry = {
                    "id": max(
                        (int(r.get("id", 0)) for r in store.rows("state_store")), default=0
                    ) + 1,
                    "配置名称": f"replay_{op_name}_{task_id}",
                    "后端类型": "memory",
                    "状态键模式": f"replay://{op_name}",
                    "TTL时长": "3600",
                    "检查点间隔": "30",
                    "增量模式": "full",
                    "清理策略": "after_replay",
                    "status": "已生效",
                    "pending": True,
                    "abnormal": False,
                    "_replay_task_id": task_id,
                    "_replay_state": copy.deepcopy(op_state),
                    "_created_at": time.time(),
                }
                store.rows("state_store").append(state_entry)
                replay_results.append({
                    "operator": op_name,
                    "state_keys": len(op_state) if isinstance(op_state, dict) else 0,
                    "restored_to": "state_store",
                })

        # 验证回放结果
        verification = self._verify_replay_results(task_id, replay_results)
        lineage_check = self._lineage_still_valid(task)

        task["结果验证"] = verification["status"]
        task["进度"] = "100%"
        task["status"] = "已完成" if verification["all_passed"] else "验证失败"
        task["_replay_results"] = replay_results
        task["_lineage_version"] = lineage_check.get("current_version", task.get("_lineage_version", ""))
        task["_replay_report"] = {
            "task_id": task_id,
            "task_name": task.get("任务名称", ""),
            "verdict": "通过" if verification["all_passed"] else "失败",
            "executable": True,
            "lineage_metric": task.get("_lineage_metric", ""),
            "lineage_version": task["_lineage_version"],
            "lineage_verified": bool(lineage_check.get("verified")),
            "lineage_signature": lineage_check.get("signature", ""),
            "broken_links": lineage_check.get("broken_links", 0),
            "verification": copy.deepcopy(verification),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        return {
            "ok": True,
            "task_id": task_id,
            "restored_operators": len(replay_results),
            "replay_results": replay_results,
            "verification": verification,
            "lineage_version": task["_lineage_version"],
            "verification_report": copy.deepcopy(task["_replay_report"]),
        }

    def _write_failed_report(self, task: dict[str, Any], reason: str) -> dict[str, Any]:
        """断链任务只生成验证失败报告，状态停在「验证失败」。"""
        task["status"] = "验证失败"
        task["结果验证"] = "failed"
        task["pending"] = False
        task["abnormal"] = True
        task["进度"] = task.get("进度", "0%")
        task["_executable"] = False
        task["_blocked_reason"] = reason
        report = {
            "task_id": task.get("id"),
            "task_name": task.get("任务名称", ""),
            "verdict": "验证失败",
            "executable": False,
            "reason": reason,
            "lineage_metric": task.get("_lineage_metric", ""),
            "lineage_version": task.get("_lineage_version", ""),
            "lineage_snapshot": copy.deepcopy(task.get("_lineage_snapshot", {})),
            "broken_links": len((task.get("_lineage_snapshot") or {}).get("broken", [])),
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        task["_replay_report"] = report
        return copy.deepcopy(report)

    def replay_report(self, task_id: int) -> dict[str, Any]:
        """读取某次回放任务的验证报告。"""
        task = store.find(self.MODULE, task_id)
        if task is None:
            return {"ok": False, "error": "回放任务不存在"}
        return {
            "ok": True,
            "task_id": task_id,
            "执行状态": task.get("status", ""),
            "结果验证": task.get("结果验证", ""),
            "executable": task.get("_executable"),
            "lineage_metric": task.get("_lineage_metric", ""),
            "lineage_version": task.get("_lineage_version", ""),
            "blocked_reason": task.get("_blocked_reason", ""),
            "report": copy.deepcopy(task.get("_replay_report", {})),
        }

    def lineage_consistency_check(self) -> dict[str, Any]:
        """核对指标血缘数据状态与回放验证报告是否一致（迁移后页面复核用）。

        报告是针对任务创建时引用的那个版本生成的，所以一致性以「该版本记录下来的
        路径快照」为准；如果之后又发生了迁移切换，额外标记为 stale（报告早于当前版本）。
        """
        items: list[dict[str, Any]] = []
        mismatches: list[dict[str, Any]] = []
        for task in store.rows(self.MODULE):
            metric = task.get("_lineage_metric", "")
            if not metric:
                continue
            pinned = task.get("_lineage_version", "")
            lineage_row = self._lineage._locate(metric_name=metric)
            current = self._lineage.verify_current_version(metric)
            current_version = current.get("current_version", "")

            pinned_path = None
            for version in (lineage_row or {}).get("_versions", []):
                if version.get("版本号") == pinned:
                    pinned_path = version.get("path")
                    break
            if pinned_path is None:
                pinned_verification = current
            else:
                pinned_verification = self._lineage.verify_path(pinned_path)

            report = task.get("_replay_report") or {}
            pinned_ok = bool(pinned_verification.get("ok", pinned_verification.get("verified")))
            executable = task.get("_executable")
            verdict = report.get("verdict", "")
            consistent = (
                bool(executable) is pinned_ok
                and (
                    (not pinned_ok and task.get("status") == "验证失败" and verdict == "验证失败")
                    or (pinned_ok and verdict in ("通过", "", "待验证"))
                )
            )
            stale = bool(pinned) and pinned != current_version
            item = {
                "task_id": task.get("id"),
                "任务名称": task.get("任务名称", ""),
                "lineage_metric": metric,
                "lineage_version": pinned,
                "current_version": current_version,
                "pinned_verified": pinned_ok,
                "lineage_status": current.get("status", "") if pinned == current_version else (
                    "断链" if not pinned_ok else "正常"
                ),
                "lineage_verified": pinned_ok,
                "task_status": task.get("status", ""),
                "executable": executable,
                "report_verdict": verdict,
                "broken_links": pinned_verification.get("broken_links", 0),
                "stale": stale,
                "consistent": consistent,
            }
            items.append(item)
            if not consistent:
                mismatches.append(item)
        return {
            "ok": True,
            "checked": len(items),
            "consistent": len(items) - len(mismatches),
            "stale": sum(1 for item in items if item["stale"]),
            "mismatches": mismatches,
            "items": items,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def _verify_replay_results(
        self, task_id: int, results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """验证回放结果的完整性：检查 state_store 中是否正确落入了回放状态。"""
        restored_ops = set(r["operator"] for r in results)
        found_in_store = set()
        for row in store.rows("state_store"):
            if row.get("_replay_task_id") == task_id:
                cfg_name = row.get("配置名称", "")
                parts = cfg_name.split("_")
                if len(parts) >= 3:
                    found_in_store.add(parts[1])

        missing = restored_ops - found_in_store
        all_passed = len(missing) == 0
        return {
            "status": "passed" if all_passed else "partial",
            "all_passed": all_passed,
            "expected": len(restored_ops),
            "found": len(found_in_store),
            "missing_operators": list(missing),
        }

    def list_replay_history(self) -> list[dict[str, Any]]:
        """列出所有回放任务历史记录及恢复方案关联。"""
        history = []
        for task in store.rows(self.MODULE):
            entry = {
                "task_id": task.get("id"),
                "任务名称": task.get("任务名称", ""),
                "源检查点": task.get("源检查点", ""),
                "回放模式": task.get("回放模式", ""),
                "结果验证": task.get("结果验证", "待验证"),
                "进度": task.get("进度", "0%"),
                "status": task.get("status", ""),
                "lineage_metric": task.get("_lineage_metric", ""),
                "lineage_version": task.get("_lineage_version", ""),
                "executable": task.get("_executable"),
                "blocked_reason": task.get("_blocked_reason", ""),
            }
            # 查找关联的恢复方案
            for rp in store.rows("restore_plan"):
                if task.get("目标拓扑", "") in rp.get("任务范围", ""):
                    entry["linked_restore_plan"] = rp.get("方案名称", "")
                    break
            history.append(entry)
        return history

    def domain_snapshot(self) -> dict[str, Any]:
        """输出模块级数据质量快照，附加回放成功率。"""
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        completed = sum(1 for t in rows if t.get("status") == "已完成")
        failed = sum(1 for t in rows if t.get("status") == "验证失败")
        total_done = completed + failed
        base["replay_success_rate"] = completed / max(total_done, 1)
        base["completed_replays"] = completed
        base["failed_replays"] = failed
        base["blocked_replays"] = sum(1 for t in rows if t.get("_executable") is False)
        return base
