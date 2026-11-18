# 实时计算服务中心

面向实时数据分析场景，提供流式计算定义、窗口聚合、策略告警和状态管理的实时数据平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流式源端 | `stream_source` | 数据源配置 | 源端名称、消息队列、Topic名称 |
| 窗口配置 | `window_config` | 窗口策略 | 策略名称、窗口类型、窗口大小 |
| 计算算子 | `calc_operator` | 算子定义 | 算子名称、算子类型、输入字段 |
| 状态存储 | `state_store` | 状态配置 | 配置名称、后端类型、状态键模式 |
| 水位追踪 | `watermark_track` | 水位记录 | 水位名称、数据源、当前水位 |
| 告警策略 | `alert_policy` | 告警规则 | 规则名称、监控指标、触发条件 |
| 滑动指标 | `sliding_index` | 指标计算 | 指标名称、计算窗口、聚合函数 |
| 流关联 | `join_stream` | 关联配置 | 配置名称、左流源、右流源 |
| 结果落地 | `sink_output` | 输出配置 | 配置名称、输出类型、目标地址 |
| 补数任务 | `backfill_job` | 补数作业 | 作业名称、目标指标、补数范围 |
| CEP规则 | `cep_rule` | 事件序列 | 序列名称、事件类型、匹配顺序 |
| 吞吐管理 | `throughput_mgr` | 吞吐配置 | 配置名称、数据源、最大QPS |
| 检查点 | `checkpoint_mgr` | 检查点配置 | 配置名称、间隔时间、超时时长 |
| 故障恢复 | `restore_plan` | 恢复方案 | 方案名称、任务范围、恢复模式 |
| 指标血缘 | `metric_lineage` | 血缘记录 | 记录ID、指标名称、上游源端 |
| 迟到处理 | `late_handler` | 处理策略 | 策略名称、允许延迟、迟到动作 |
| 任务拓扑 | `task_topology` | 拓扑配置 | 拓扑名称、算子链、并行度 |
| 数据采样 | `data_sampler` | 采样策略 | 策略名称、采样方式、采样比例 |
| 调试追踪 | `debug_trace` | 调试会话 | 会话名称、目标算子、采样数据 |
| 聚合落库 | `aggregate_sink` | 聚合任务 | 任务名称、源指标集、聚合粒度 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 序号 23：指标血缘 ↔ 回放引擎的迁移切换

血缘刷新以前只改版本号和更新时间，路径可能已经断链却仍显示「正常」。
现在刷新会重新读取上下游引用、重算路径并核对完整性：

- 存在断链时版本仍会提升，但状态只能是「断链」，不能写「正常」；
- 同一 `batch_id` 的批量刷新每条记录只提升一次版本，重复提交幂等；
- 每次版本更新都落一份路径快照（检查项、断链位置、路径签名），回放引擎引用这次重算结果；
- 迁移切换支持两种取舍：`replace` 按新路径整体替换并切换当前消费版本；
  `retain_old` 保留旧路径与当前版本，只新增一条修订记录；
- 切换按 `switch_key` 幂等，并发重复提交只生效一次；事务失步时丢弃副本、回滚原版本；
- 回放任务建单时重新校验血缘：断链版本只生成「验证失败」报告，任务不可执行；
- 迁移台账、版本存量与部署面板通过接口对齐，供血缘页 / 回放页 / 迁移部署页核对。

### 新增页面

| 路径 | 页面 | 用途 |
| --- | --- | --- |
| `/lineage` | 指标血缘 | DAG 图谱、断链位置、版本存量、迁移台账、刷新与切换 |
| `/replay` | 回放引擎 | 任务列表、血缘版本引用、执行/报告、血缘与报告一致性 |
| `/migration_deploy` | 迁移部署 | 数据库部署写入的血缘存量、回放构建流水线、迁移台账 |

### 联动接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/lineage/refresh-all` | 批量刷新，`batch_id` 幂等 |
| POST | `/api/lineage/{id}/refresh` | 单条刷新并核对链接 |
| GET | `/api/lineage/{id}/graph` | 血缘 DAG（含断链位置） |
| GET | `/api/lineage/{id}/verify` | 校验当前版本完整性 |
| POST | `/api/lineage/switch-env` | 迁移切换，`mode=replace|retain_old` |
| GET | `/api/lineage/migration-ledger` | 迁移台账与版本存量 |
| GET | `/api/lineage/deployment-panel` | 部署面板（血缘 + 回放构建流水线） |
| POST | `/api/replay/jobs` | 建回放任务，引用血缘当前版本 |
| POST | `/api/replay/{id}/execute` | 执行回放（执行前后都会校验血缘） |
| GET | `/api/replay/{id}/report` | 读取回放验证报告 |
| GET | `/api/replay/consistency` | 血缘状态与回放报告一致性核对 |

领域：数据工程
子方向：数据分析与数据应用
