"""指标血缘领域服务：追踪实时指标从数据源到最终输出的全链路血缘关系。

序号 23（迁移切换）修复要点：
1. 刷新血缘必须重新解析并核对**全部**上下游引用，断链时不得写回「正常」；
2. 一次批量刷新对同一条记录只提升一次版本，重复提交同一批次保持幂等；
3. 每次版本更新都附带完整的路径校验结果（检查项、断链位置、路径签名）；
4. 迁移切换（replace / retain_old）在事务副本上执行，失败或重复切换时回滚原版本；
5. 维护唯一的「当前消费版本」指针与可验证标记，供回放引擎消费；
6. 输出迁移台账、版本存量与部署面板，供血缘页面 / 回放页面 / 概览页面交叉核对。
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 本修复所属的迁移序号 / 部署批次。
MIGRATION_SEQUENCE = 23

LINK_SEPARATOR = " → "
# 上游源端、中间算子、输出目标各自允许被引用的模块与名称字段。
SOURCE_MODULES: tuple[tuple[str, str], ...] = (("stream_source", "源端名称"),)
OPERATOR_MODULES: tuple[tuple[str, str], ...] = (
    ("calc_operator", "算子名称"),
    ("window_config", "策略名称"),
    ("sliding_index", "指标名称"),
    ("stream_job", "作业名称"),
    ("task_topology", "拓扑名称"),
)
OUTPUT_MODULES: tuple[tuple[str, str], ...] = (
    ("sink_output", "配置名称"),
    ("aggregate_sink", "任务名称"),
)

STAGE_SPECS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("source", "sources", SOURCE_MODULES),
    ("operator", "operators", OPERATOR_MODULES),
    ("output", "outputs", OUTPUT_MODULES),
)


def split_path(value: Any) -> list[str]:
    """把「a → b → c」形式的历史字段拆成名称列表，兼容带空格/不带空格的写法。"""
    text = str(value or "").replace("->", "→")
    return [part.strip() for part in text.split("→") if part.strip()]


def join_path(names: list[str]) -> str:
    return LINK_SEPARATOR.join(name for name in names if str(name).strip())


def parse_version(value: Any) -> int:
    """把「v12」或历史脏数据解析为整数版本；无法解析时从 0 开始。"""
    text = str(value or "").strip().lower().lstrip("v")
    if not text.isdigit():
        return 0
    return int(text)


class MetricLineageService(BaseModuleService):
    MODULE = "metric_lineage"
    ENTITY = "血缘记录"
    LIST_FIELDS = ["记录ID", "指标名称", "上游源端", "中间算子", "输出目标", "更新时间", "版本号"]
    REQUIRED_FIELDS = ["记录ID", "指标名称", "上游源端"]
    STATUS_ORDER = ["正常", "变更中", "断链"]
    ACTION_RULES = {"刷新血缘": "正常", "标记断链": "变更中"}
    NEGATIVE_ACTIONS = []

    # 迁移切换的幂等登记表：切换键 → 首次生效结果。
    # 类级共享 + 互斥锁，保证并发重复切换只有一次真正落库。
    _switch_lock = threading.RLock()
    _switch_registry: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 路径解析与校验
    # ------------------------------------------------------------------

    @staticmethod
    def _path_from_row(row: dict[str, Any]) -> dict[str, list[str]]:
        """始终以记录上的三个路径字段为准重新解析，避免沿用旧的校验缓存。"""
        return {
            "sources": split_path(row.get("上游源端")),
            "operators": split_path(row.get("中间算子")),
            "outputs": split_path(row.get("输出目标")),
        }

    @staticmethod
    def _path_signature(path: dict[str, list[str]]) -> str:
        raw = "|".join(
            f"{stage}:{','.join(path.get(stage, []))}"
            for stage in ("sources", "operators", "outputs")
        )
        return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _resolve_reference(
        name: str, modules: tuple[tuple[str, str], ...],
    ) -> tuple[str | None, dict[str, Any] | None]:
        for module, field in modules:
            for row in store.rows(module):
                if row.get(field) == name:
                    return module, row
        return None, None

    def _ensure_all_current_versions(self) -> None:
        for row in store.rows(self.MODULE):
            if row.get("记录ID"):
                self._ensure_current_version(row)

    def list_entries(self, **kwargs: Any):
        """列表读取前先给存量记录固定一个可验证的当前版本（不提升版本号）。"""
        self._ensure_all_current_versions()
        return super().list_entries(**kwargs)

    def verify_path(self, path: dict[str, list[str]]) -> dict[str, Any]:
        """逐段核对路径上的每一个引用是否存在，返回检查项与断链位置。

        这是所有版本更新、回放建任务的唯一判定入口：只有 ok=True 才允许
        把血缘记录标记为「正常」、才允许回放任务进入可执行状态。
        """
        checks: list[dict[str, Any]] = []
        broken: list[dict[str, Any]] = []

        for stage, key, modules in STAGE_SPECS:
            names = [str(n).strip() for n in path.get(key, []) if str(n).strip()]
            if not names:
                item = {
                    "stage": stage,
                    "name": "",
                    "found": False,
                    "reason": "empty_stage",
                    "detail": "路径缺少该环节引用",
                }
                checks.append(item)
                broken.append(item)
                continue
            for name in names:
                module, ref_row = self._resolve_reference(name, modules)
                item = {
                    "stage": stage,
                    "name": name,
                    "found": ref_row is not None,
                    "module": module,
                    "referenced_status": (ref_row or {}).get("status", ""),
                }
                if ref_row is None:
                    item["reason"] = "missing"
                    item["detail"] = f"引用的{stage}「{name}」在数据仓库中不存在"
                    broken.append(item)
                checks.append(item)

        return {
            "ok": not broken,
            "checks": checks,
            "broken": broken,
            "total_links": len(checks),
            "broken_links": len(broken),
            "path": {
                "sources": list(path.get("sources", [])),
                "operators": list(path.get("operators", [])),
                "outputs": list(path.get("outputs", [])),
            },
            "signature": self._path_signature(path),
        }

    def _ensure_current_version(
        self, row: dict[str, Any], environment: str = "当前环境",
    ) -> dict[str, Any]:
        """保证记录始终固定在一个可验证的当前版本上。

        历史样例数据可能只有版本号、没有校验结果；这里按原路径补一次基线校验，
        不提升版本号，只把「当前消费版本」指针和校验结论补齐。
        """
        if row.get("_current_version") and row.get("_versions"):
            return self.verify_path(self._path_from_row(row))
        verification = self.verify_path(self._path_from_row(row))
        version = str(row.get("版本号") or "v1")
        original_status = row.get("status", "")
        self._apply_verification(
            row, verification,
            batch_id=f"baseline-{MIGRATION_SEQUENCE}-{row.get('id')}",
            version=version,
            environment=environment,
            note="建立可验证的当前版本基线",
        )
        # 基线只纠正「实际断链却显示正常」的记录；路径完整时保留变更中 / 断链等既有状态。
        if verification["ok"] and original_status != "正常":
            row["status"] = original_status
            row["abnormal"] = bool(row.get("abnormal"))
        row["_last_batch_bumped"] = False
        return verification

    def _locate(
        self, entry_id: int | None = None, metric_name: str | None = None,
    ) -> dict[str, Any] | None:
        if entry_id is not None:
            row = store.find(self.MODULE, int(entry_id))
            if row is not None:
                return row
        if metric_name:
            for row in store.rows(self.MODULE):
                if row.get("指标名称") == metric_name:
                    return row
            for row in store.rows(self.MODULE):
                if row.get("记录ID") == metric_name:
                    return row
        return None

    # ------------------------------------------------------------------
    # 版本与验证快照
    # ------------------------------------------------------------------

    def _apply_verification(
        self,
        row: dict[str, Any],
        verification: dict[str, Any],
        *,
        batch_id: str,
        version: str,
        environment: str,
        note: str = "",
    ) -> None:
        """把校验结果、路径和唯一当前版本指针写回记录（只做内存写，由调用方决定事务边界）。"""
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        path = verification["path"]
        ok = verification["ok"]

        row["上游源端"] = join_path(path["sources"])
        row["中间算子"] = join_path(path["operators"])
        row["输出目标"] = join_path(path["outputs"])
        row["版本号"] = version
        row["更新时间"] = now
        row["status"] = "正常" if ok else "断链"
        row["pending"] = not ok
        row["abnormal"] = not ok
        row["_verified_path"] = copy.deepcopy(path)
        row["_path_signature"] = verification["signature"]
        row["_link_checks"] = copy.deepcopy(verification["checks"])
        row["_broken_links"] = copy.deepcopy(verification["broken"])
        row["_last_verified_at"] = time.time()
        row["_last_batch"] = batch_id
        row["_last_batch_bumped"] = True
        row["_current_version"] = version
        row["_current_verified"] = ok
        row["_current_environment"] = environment
        row["_version_number"] = parse_version(version)

        versions = row.setdefault("_versions", [])
        for item in versions:
            item["current"] = False
        versions.append({
            "版本号": version,
            "path": copy.deepcopy(path),
            "signature": verification["signature"],
            "status": "正常" if ok else "断链",
            "verified": ok,
            "broken_links": verification["broken_links"],
            "environment": environment,
            "batch_id": batch_id,
            "verified_at": now,
            "current": True,
            "note": note,
        })

    # ------------------------------------------------------------------
    # 刷新与断链检测
    # ------------------------------------------------------------------

    def refresh_lineage(
        self,
        entry_id: int | None = None,
        metric_name: str | None = None,
        batch_id: str | None = None,
        environment: str = "当前环境",
    ) -> dict[str, Any]:
        """刷新单条血缘：重新读取上下游引用、重算路径、核对完整性后再提升版本。

        - 同一 batch_id 重复刷新不会重复提升版本（幂等）；
        - 存在断链时状态写为「断链」，绝不写「正常」；
        - 校验结果与路径签名随版本一起落库，供回放引擎引用本次重算结果。
        """
        row = self._locate(entry_id, metric_name)
        if row is None:
            return {"ok": False, "error": f"血缘记录「{metric_name or entry_id}」不存在"}

        batch = str(batch_id or f"batch-{time.time_ns()}")
        metric = row.get("指标名称", "")
        if row.get("_last_batch") == batch and row.get("_last_batch_bumped"):
            return {
                "ok": True,
                "bumped": False,
                "idempotent": True,
                "batch_id": batch,
                "metric": metric,
                "version": row.get("版本号", ""),
                "current_version": row.get("_current_version", row.get("版本号", "")),
                "status": row.get("status", ""),
                "verified": bool(row.get("_current_verified")),
                "broken_links": len(row.get("_broken_links", [])),
                "broken": copy.deepcopy(row.get("_broken_links", [])),
                "checks": copy.deepcopy(row.get("_link_checks", [])),
                "total_links": len(row.get("_link_checks", [])),
                "path": copy.deepcopy(row.get("_verified_path", {})),
            }

        path = self._path_from_row(row)
        verification = self.verify_path(path)
        version = f"v{parse_version(row.get('版本号')) + 1}"
        self._apply_verification(
            row, verification, batch_id=batch, version=version,
            environment=environment, note="刷新血缘重算路径",
        )
        return {
            "ok": True,
            "bumped": True,
            "idempotent": False,
            "batch_id": batch,
            "metric": metric,
            "version": version,
            "current_version": row["_current_version"],
            "status": row["status"],
            "verified": verification["ok"],
            "broken_links": verification["broken_links"],
            "broken": verification["broken"],
            "checks": verification["checks"],
            "path": verification["path"],
        }

    def refresh_all_lineages(self, batch_id: str | None = None) -> dict[str, Any]:
        """批量刷新全部血缘：同一批次内每条记录只提升一次版本。"""
        batch = str(batch_id or f"batch-{time.time_ns()}")
        refreshed = 0
        bumped = 0
        normal = 0
        broken = 0
        records: list[dict[str, Any]] = []
        for row in list(store.rows(self.MODULE)):
            if not row.get("记录ID"):
                continue
            result = self.refresh_lineage(entry_id=int(row["id"]), batch_id=batch)
            if not result.get("ok"):
                continue
            refreshed += 1
            if result.get("bumped"):
                bumped += 1
            if result.get("verified"):
                normal += 1
            else:
                broken += 1
            records.append({
                "id": row.get("id"),
                "metric": result["metric"],
                "version": result["version"],
                "status": result["status"],
                "bumped": result["bumped"],
                "broken_links": result["broken_links"],
            })
        return {
            "ok": True,
            "batch_id": batch,
            "refreshed": refreshed,
            "version_bumped": bumped,
            "normal": normal,
            "broken": broken,
            "records": records,
        }

    def detect_broken_lineage(self, metric_name: str) -> dict[str, Any]:
        """重新校验血缘上下游引用，并把真实的断链结果同步到记录状态。"""
        row = self._locate(metric_name=metric_name)
        if row is None:
            return {"ok": False, "error": f"血缘记录「{metric_name}」不存在"}

        verification = self.verify_path(self._path_from_row(row))
        row["_link_checks"] = copy.deepcopy(verification["checks"])
        row["_broken_links"] = copy.deepcopy(verification["broken"])
        row["_current_verified"] = verification["ok"]
        row["_last_verified_at"] = time.time()
        row["status"] = "正常" if verification["ok"] else "断链"
        row["abnormal"] = not verification["ok"]
        row["pending"] = not verification["ok"]

        return {
            "ok": True,
            "metric": metric_name,
            "version": row.get("版本号", ""),
            "current_version": row.get("_current_version", row.get("版本号", "")),
            "total_links": verification["total_links"],
            "broken_links": verification["broken_links"],
            "broken": verification["broken"],
            "checks": verification["checks"],
            "status": row["status"],
            "verified": verification["ok"],
        }

    def verify_current_version(
        self, metric_name: str | None = None, entry_id: int | None = None,
    ) -> dict[str, Any]:
        """回放引擎消费入口：对当前版本重新做一次完整校验，返回可核对的结果。"""
        row = self._locate(entry_id, metric_name)
        if row is None:
            return {
                "ok": False,
                "verified": False,
                "error": f"血缘记录「{metric_name or entry_id}」不存在",
            }
        if not row.get("_current_version") or not row.get("_versions"):
            # 存量记录先固定一个可验证的当前版本（不提升版本号）。
            self._ensure_current_version(row, row.get("_current_environment", "当前环境"))
        verification = self.verify_path(self._path_from_row(row))
        # 校验结论同步回记录，避免「数据看起来正常、实际已断链」。
        row["_link_checks"] = copy.deepcopy(verification["checks"])
        row["_broken_links"] = copy.deepcopy(verification["broken"])
        row["_current_verified"] = verification["ok"]
        row["_last_verified_at"] = time.time()
        row["status"] = "正常" if verification["ok"] else "断链"
        row["abnormal"] = not verification["ok"]
        row["pending"] = not verification["ok"]

        return {
            "ok": True,
            "metric": row.get("指标名称", ""),
            "version": row.get("版本号", ""),
            "current_version": row.get("_current_version", row.get("版本号", "")),
            "verified": verification["ok"],
            "status": row["status"],
            "signature": verification["signature"],
            "path": verification["path"],
            "checks": verification["checks"],
            "broken": verification["broken"],
            "broken_links": verification["broken_links"],
            "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------------
    # 迁移切换 / 修订 / 回滚 / 台账
    # ------------------------------------------------------------------

    @staticmethod
    def _edit_path(
        path: dict[str, list[str]],
        *,
        removed_upstreams: list[str],
        added_upstreams: list[str],
        removed_operators: list[str],
        added_operators: list[str],
        removed_outputs: list[str],
        added_outputs: list[str],
    ) -> dict[str, list[str]]:
        def _edit(values: list[str], removed: list[str], added: list[str]) -> list[str]:
            result = [value for value in values if value not in removed]
            for value in added:
                value = str(value).strip()
                if value and value not in result:
                    result.append(value)
            return result

        return {
            "sources": _edit(path["sources"], removed_upstreams, added_upstreams),
            "operators": _edit(path["operators"], removed_operators, added_operators),
            "outputs": _edit(path["outputs"], removed_outputs, added_outputs),
        }

    def switch_environment(
        self,
        *,
        metric_name: str | None = None,
        entry_id: int | None = None,
        switch_key: str | None = None,
        mode: str = "replace",
        source_env: str = "旧环境",
        target_env: str = "新环境",
        removed_upstreams: list[str] | None = None,
        added_upstreams: list[str] | None = None,
        removed_operators: list[str] | None = None,
        added_operators: list[str] | None = None,
        removed_outputs: list[str] | None = None,
        added_outputs: list[str] | None = None,
        simulate_failure: bool = False,
    ) -> dict[str, Any]:
        """执行序号 23 的环境迁移切换。

        两种取舍路径：
        - replace（整体替换）：按新路径整体生成新版本并切换当前消费版本；
        - retain_old（保留旧路径）：当前版本与路径保持不变，只新增一条修订记录。

        切换在记录副本上进行，校验失败 / 模拟事务失步时丢弃副本，原版本回滚。
        相同切换键（并发重复提交）只生效一次，后续请求按幂等结果返回。
        """
        row = self._locate(entry_id, metric_name)
        if row is None:
            return {"ok": False, "error": f"血缘记录「{metric_name or entry_id}」不存在"}
        if mode not in ("replace", "retain_old"):
            return {"ok": False, "error": f"未知迁移模式「{mode}」"}

        metric = row.get("指标名称", "")
        key = switch_key or (
            f"seq{MIGRATION_SEQUENCE}:{metric}:{source_env}->{target_env}:{mode}:"
            f"{removed_upstreams or []}:{added_upstreams or []}:"
            f"{removed_operators or []}:{added_operators or []}:"
            f"{removed_outputs or []}:{added_outputs or []}"
        )

        with self._switch_lock:
            cached = self._switch_registry.get(key)
            if cached is not None:
                replay = copy.deepcopy(cached)
                replay["applied"] = False
                replay["idempotent"] = True
                replay["switch_key"] = key
                replay["message"] = "同一迁移切换已生效，本次按幂等返回，不再重复提升版本"
                return replay

            self._ensure_current_version(row, source_env)
            committed_at = time.strftime("%Y-%m-%d %H:%M:%S")
            working = copy.deepcopy(row)
            original_version = row.get("版本号", "")
            original_path = self._path_from_row(row)
            before = {
                "版本号": original_version,
                "current_version": row.get("_current_version", original_version),
                "status": row.get("status", ""),
                "path": copy.deepcopy(original_path),
            }
            new_path = self._edit_path(
                original_path,
                removed_upstreams=removed_upstreams or [],
                added_upstreams=added_upstreams or [],
                removed_operators=removed_operators or [],
                added_operators=added_operators or [],
                removed_outputs=removed_outputs or [],
                added_outputs=added_outputs or [],
            )

            try:
                if simulate_failure:
                    raise RuntimeError("事务失步：模拟迁移提交中断")
                verification = self.verify_path(new_path)
                if mode == "replace":
                    version = f"v{parse_version(original_version) + 1}"
                    self._apply_verification(
                        working, verification,
                        batch_id=f"migration-{MIGRATION_SEQUENCE}-{int(time.time() * 1000)}",
                        version=version,
                        environment=target_env,
                        note=f"序号{MIGRATION_SEQUENCE}整体替换：{source_env} → {target_env}",
                    )
                    working["_pending_path"] = None
                    ledger_status = "已生效" if verification["ok"] else "已切换（断链）"
                    revision_note = "按新路径整体替换版本，旧路径进入历史版本"
                else:
                    # 保留旧路径：不动版本号与当前消费版本，只登记修订。
                    revision = {
                        "id": max(
                            (int(r.get("id", 0)) for r in store.rows("lineage_revision")),
                            default=0,
                        ) + 1,
                        "序号": MIGRATION_SEQUENCE,
                        "指标名称": metric,
                        "记录ID": row.get("记录ID", ""),
                        "原版本号": original_version,
                        "候选路径": copy.deepcopy(new_path),
                        "候选人": {
                            "sources": join_path(new_path["sources"]),
                            "operators": join_path(new_path["operators"]),
                            "outputs": join_path(new_path["outputs"]),
                        },
                        "校验": {
                            "ok": verification["ok"],
                            "broken_links": verification["broken_links"],
                            "broken": copy.deepcopy(verification["broken"]),
                        },
                        "状态": "修订待生效" if verification["ok"] else "修订不可生效",
                        "环境": target_env,
                        "时间": committed_at,
                    }
                    store.rows("lineage_revision").append(revision)
                    working["_pending_path"] = copy.deepcopy(new_path)
                    working["_pending_signature"] = verification["signature"]
                    working["_pending_verification"] = {
                        "ok": verification["ok"],
                        "broken_links": verification["broken_links"],
                        "checks": copy.deepcopy(verification["checks"]),
                    }
                    working.setdefault("_revisions", []).append({
                        "id": revision["id"],
                        "状态": revision["状态"],
                        "环境": target_env,
                        "时间": committed_at,
                    })
                    version = original_version
                    ledger_status = "修订待生效" if verification["ok"] else "修订不可生效"
                    revision_note = "保留旧路径与当前版本，仅新增修订记录"

                # 事务提交：校验与写入都成功后才替换真实记录。
                row.clear()
                row.update(working)
            except Exception as exc:  # 事务失步 → 回滚原版本，不落任何修改
                ledger = self._append_ledger({
                    "指标名称": metric,
                    "来源环境": source_env,
                    "目标环境": target_env,
                    "迁移模式": "整体替换" if mode == "replace" else "保留旧路径",
                    "版本号": original_version,
                    "当前消费版本": row.get("_current_version", original_version),
                    "状态": "已回滚",
                    "断链数": 0,
                    "幂等键": key,
                    "回滚": True,
                    "说明": f"事务失步，原版本保持：{exc}",
                })
                return {
                    "ok": False,
                    "applied": False,
                    "idempotent": False,
                    "rolled_back": True,
                    "switch_key": key,
                    "metric": metric,
                    "mode": mode,
                    "version": original_version,
                    "current_version": row.get("_current_version", original_version),
                    "status": row.get("status", ""),
                    "broken_links": 0,
                    "broken": [],
                    "ledger_id": ledger["id"],
                    "message": "迁移事务失步，已回滚原版本",
                    "error": str(exc),
                }

            verification = self.verify_path(row.get("_verified_path") or new_path)
            ledger = self._append_ledger({
                "指标名称": metric,
                "来源环境": source_env,
                "目标环境": target_env,
                "迁移模式": "整体替换" if mode == "replace" else "保留旧路径",
                "版本号": version,
                "当前消费版本": row.get("_current_version", version),
                "状态": ledger_status,
                "断链数": verification["broken_links"],
                "幂等键": key,
                "回滚": False,
                "说明": revision_note,
            })

            result = {
                "ok": True,
                "applied": True,
                "idempotent": False,
                "rolled_back": False,
                "switch_key": key,
                "sequence": MIGRATION_SEQUENCE,
                "metric": metric,
                "mode": mode,
                "status": ledger_status,
                "version": version,
                "current_version": row.get("_current_version", version),
                "current_verified": bool(row.get("_current_verified")),
                "path": copy.deepcopy(row.get("_verified_path") or original_path),
                "candidate_path": copy.deepcopy(new_path),
                "broken_links": verification["broken_links"],
                "broken": copy.deepcopy(verification["broken"]),
                "before": before,
                "after": {
                    "版本号": row.get("版本号", ""),
                    "current_version": row.get("_current_version", ""),
                    "status": row.get("status", ""),
                    "path": copy.deepcopy(row.get("_verified_path") or original_path),
                },
                "ledger_id": ledger["id"],
                "message": revision_note,
            }
            self._switch_registry[key] = copy.deepcopy(result)
            return result

    def _append_ledger(self, values: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows("migration_ledger")
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "序号": MIGRATION_SEQUENCE,
            "时间": time.strftime("%Y-%m-%d %H:%M:%S"),
            "操作人": "数据工程组",
            "切换次数": 1,
            "回滚": False,
            "断链数": 0,
        }
        entry.update(values)
        rows.append(entry)
        return entry

    def migration_ledger(self) -> dict[str, Any]:
        """迁移台账：切换记录、版本存量与部署面板数据。"""
        ledger = [copy.deepcopy(row) for row in store.rows("migration_ledger")]
        versions: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            for version in row.get("_versions", []):
                versions.append({
                    "指标名称": row.get("指标名称", ""),
                    "版本号": version.get("版本号", ""),
                    "环境": version.get("environment", ""),
                    "状态": version.get("status", ""),
                    "current": bool(version.get("current")),
                    "verified": bool(version.get("verified")),
                    "verified_at": version.get("verified_at", ""),
                })
        revisions = [copy.deepcopy(r) for r in store.rows("lineage_revision")]
        return {
            "ok": True,
            "sequence": MIGRATION_SEQUENCE,
            "ledger": ledger,
            "versions": versions,
            "revisions": revisions,
            "switches": len(ledger),
            "applied": sum(1 for r in ledger if r.get("状态") in ("已生效", "已切换（断链）")),
            "retained": sum(1 for r in ledger if r.get("状态") == "修订待生效"),
            "rolled_back": sum(1 for r in ledger if r.get("回滚")),
        }

    def deployment_panel(self) -> dict[str, Any]:
        """序号 23 部署面板：数据库部署、指标血缘存量与回放构建流水线的核对视图。"""
        lineage_rows = store.rows(self.MODULE)
        replay_rows = store.rows("replay_engine")
        ledger = store.rows("migration_ledger")
        versions = [
            {
                "指标名称": row.get("指标名称", ""),
                "版本号": version.get("版本号", ""),
                "环境": version.get("environment", ""),
                "current": bool(version.get("current")),
                "verified": bool(version.get("verified")),
                "status": version.get("status", ""),
            }
            for row in lineage_rows
            for version in row.get("_versions", [])
        ]
        return {
            "ok": True,
            "sequence": MIGRATION_SEQUENCE,
            "environment": "开发环境 → 生产环境",
            "database_deployment": {
                "artifact": "指标血缘数据",
                "records": sum(1 for row in lineage_rows if row.get("记录ID")),
                "current_versions": sum(1 for row in lineage_rows if row.get("_current_version")),
                "broken": sum(1 for row in lineage_rows if row.get("status") == "断链"),
                "ledger_entries": len(ledger),
            },
            "lineage_versions": versions,
            "replay_build": {
                "tasks": len(replay_rows),
                "executable": sum(1 for row in replay_rows if row.get("_executable")),
                "blocked": sum(1 for row in replay_rows if row.get("_executable") is False),
                "blocked_tasks": [
                    {
                        "task_id": row.get("id"),
                        "任务名称": row.get("任务名称", ""),
                        "lineage_metric": row.get("_lineage_metric", ""),
                        "blocked_reason": row.get("_blocked_reason", ""),
                    }
                    for row in replay_rows
                    if row.get("_executable") is False
                ],
            },
            "pages": ["指标血缘", "回放引擎", "运营概览"],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ------------------------------------------------------------------
    # 图谱与动作
    # ------------------------------------------------------------------

    def build_lineage_graph(self, metric_name: str) -> dict[str, Any]:
        """基于当前记录的已核对路径生成 DAG 图谱，并标注断链位置。"""
        row = self._locate(metric_name=metric_name)
        if row is None:
            return {"ok": False, "error": f"血缘记录「{metric_name}」不存在"}

        path = self._path_from_row(row)
        verification = self.verify_path(path)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []

        def _node(stage: str, name: str, check: dict[str, Any] | None) -> str:
            node_id = f"{stage}:{name}" if name else f"{stage}:__empty__"
            nodes.append({
                "id": node_id,
                "stage": stage,
                "label": name or "（缺失引用）",
                "found": bool(check and check.get("found")),
                "module": (check or {}).get("module"),
                "reason": (check or {}).get("reason", ""),
            })
            return node_id

        check_map = {
            (item["stage"], item["name"]): item
            for item in verification["checks"]
        }
        source_nodes = [
            _node("source", name, check_map.get(("source", name)))
            for name in path["sources"]
        ] or [_node("source", "", check_map.get(("source", "")))]
        operator_nodes = [
            _node("operator", name, check_map.get(("operator", name)))
            for name in path["operators"]
        ] or [_node("operator", "", check_map.get(("operator", "")))]
        output_nodes = [
            _node("output", name, check_map.get(("output", name)))
            for name in path["outputs"]
        ] or [_node("output", "", check_map.get(("output", "")))]

        def _link(source_id: str, target_id: str) -> None:
            broken_edge = any(
                not node["found"] for node in nodes if node["id"] in (source_id, target_id)
            )
            edges.append({"from": source_id, "to": target_id, "broken": broken_edge})

        for source_id in source_nodes:
            _link(source_id, operator_nodes[0])
        for left, right in zip(operator_nodes, operator_nodes[1:]):
            _link(left, right)
        for output_id in output_nodes:
            _link(operator_nodes[-1], output_id)

        return {
            "ok": True,
            "metric": row.get("指标名称", ""),
            "version": row.get("版本号", ""),
            "current_version": row.get("_current_version", row.get("版本号", "")),
            "current_verified": bool(row.get("_current_verified")),
            "status": row.get("status", ""),
            "signature": verification["signature"],
            "nodes": nodes,
            "edges": edges,
            "broken": verification["broken"],
            "broken_links": verification["broken_links"],
            "total_links": verification["total_links"],
            "sources": path["sources"],
            "operators": path["operators"],
            "outputs": path["outputs"],
            "path": path,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action == "刷新血缘":
            result = self.refresh_lineage(entry_id=entry_id)
            if not result.get("ok"):
                return None, result.get("error", "血缘刷新失败")
            if result.get("verified"):
                message = (
                    f"血缘已刷新：{len(result.get('checks', []))} 个链接均完整，"
                    f"版本 {result['version']}"
                )
            else:
                message = (
                    f"血缘已刷新：发现 {result['broken_links']} 处断链，"
                    f"版本 {result['version']} 标记为断链，回放任务将无法执行"
                )
            return entry, message
        if action == "标记断链":
            entry["status"] = "断链"
            entry["pending"] = True
            entry["abnormal"] = True
            entry["_manual_broken"] = True
            return entry, "血缘记录已标记为断链"
        return super().run_action(entry_id, action)

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        base["normal"] = sum(1 for r in rows if r.get("status") == "正常")
        base["changing"] = sum(1 for r in rows if r.get("status") == "变更中")
        base["broken"] = sum(1 for r in rows if r.get("status") == "断链")
        base["verified_current"] = sum(1 for r in rows if r.get("_current_verified"))
        base["current_versions"] = sum(1 for r in rows if r.get("_current_version"))
        base["ledger_entries"] = len(store.rows("migration_ledger"))
        base["revisions"] = len(store.rows("lineage_revision"))
        return base
