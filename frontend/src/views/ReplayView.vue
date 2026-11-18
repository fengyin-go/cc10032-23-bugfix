<template>
  <section class="page replay-page">
    <header class="page-head">
      <div>
        <h2>回放引擎 · 序号 23 迁移核对</h2>
        <p class="page-desc">
          回放任务会引用指标血缘本次重算的当前版本；断链版本只能生成「验证失败」报告，
          不能进入可执行回放。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="replay-reload" type="button" @click="reloadAll">重新加载</button>
      </div>
    </header>

    <p v-if="message" class="notice" data-testid="replay-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="replay-error">{{ errorMessage }}</p>

    <section class="stat-row">
      <article class="stat-card">
        <span class="stat-label">回放任务总数</span>
        <strong class="stat-value" data-testid="replay-stat-total">{{ tasks.length }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可执行回放</span>
        <strong class="stat-value" data-testid="replay-stat-executable">{{ executableCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">因断链被阻断</span>
        <strong class="stat-value" data-testid="replay-stat-blocked">{{ blockedCount }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">血缘/报告一致</span>
        <strong class="stat-value" data-testid="replay-stat-consistent">
          {{ consistency ? `${consistency.consistent}/${consistency.checked}` : '—' }}
        </strong>
      </article>
    </section>

    <form class="analysis-form" data-testid="create-replay-form" @submit.prevent="onCreate">
      <label>
        <span>任务名称</span>
        <input v-model="form.job_name" required data-testid="replay-job-name" />
      </label>
      <label>
        <span>源检查点 ID</span>
        <input v-model="form.checkpoint_id" required data-testid="replay-checkpoint" />
      </label>
      <label>
        <span>目标拓扑</span>
        <input v-model="form.topology_name" data-testid="replay-topology" />
      </label>
      <label>
        <span>引用指标血缘</span>
        <select v-model="form.lineage_metric" data-testid="replay-metric">
          <option value="">（不引用）</option>
          <option v-for="metric in lineageMetrics" :key="metric" :value="metric">{{ metric }}</option>
        </select>
      </label>
      <button class="btn primary" type="submit" data-testid="replay-create">创建回放任务</button>
    </form>

    <table class="data-table" data-testid="replay-tasks">
      <thead>
        <tr>
          <th>任务名称</th><th>源检查点</th><th>回放模式</th><th>血缘指标</th>
          <th>血缘版本</th><th>结果验证</th><th>状态</th><th>可执行</th><th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="task in tasks"
          :key="String(task.id)"
          :data-testid="`replay-row-${task.id}`"
          :data-status="String(task.status)"
        >
          <td>{{ task['任务名称'] }}</td>
          <td>{{ task['源检查点'] }}</td>
          <td>{{ task['回放模式'] }}</td>
          <td>{{ task._lineage_metric || '—' }}</td>
          <td>{{ task._lineage_version || '—' }}</td>
          <td>{{ task['结果验证'] }}</td>
          <td><span :class="['status-pill', `status-${task.status}`]">{{ task.status }}</span></td>
          <td>{{ task._executable === false ? '否（断链阻断）' : task._executable ? '是' : '—' }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`replay-execute-${task.id}`" @click="onExecute(task)">
              执行回放
            </button>
            <button class="link" type="button" :data-testid="`replay-report-${task.id}`" @click="onReport(task)">
              查看报告
            </button>
          </td>
        </tr>
        <tr v-if="!tasks.length">
          <td colspan="9" class="empty-state">暂无回放任务</td>
        </tr>
      </tbody>
    </table>

    <section v-if="report" class="report-panel" data-testid="replay-report">
      <header class="panel-head">
        <h3>回放验证报告 #{{ report.task_id }}</h3>
        <button class="link" type="button" @click="report = null">关闭</button>
      </header>
      <dl class="dag-detail">
        <div><dt>结论</dt><dd data-testid="report-verdict">{{ report.verdict }}</dd></div>
        <div><dt>可执行</dt><dd>{{ report.executable ? '是' : '否' }}</dd></div>
        <div><dt>引用血缘指标</dt><dd>{{ report.lineage_metric || '—' }}</dd></div>
        <div><dt>引用血缘版本</dt><dd>{{ report.lineage_version || '—' }}</dd></div>
        <div><dt>断链数</dt><dd>{{ report.broken_links ?? 0 }}</dd></div>
        <div><dt>生成时间</dt><dd>{{ report.generated_at }}</dd></div>
      </dl>
      <p v-if="report.reason" class="error-text" data-testid="report-reason">{{ report.reason }}</p>
      <ul v-if="(report.broken ?? []).length" class="broken-list">
        <li v-for="item in report.broken" :key="`${item.stage}-${item.name}`">
          {{ stageLabel(item.stage) }}「{{ item.name }}」缺失
        </li>
      </ul>
    </section>

    <section v-if="consistency" class="panel" data-testid="replay-consistency">
      <header class="panel-head">
        <h3>血缘状态 ↔ 回放报告一致性</h3>
        <span class="muted">{{ consistency.checked }} 条任务 · 一致 {{ consistency.consistent }} 条</span>
      </header>
      <table class="data-table compact">
        <thead>
          <tr><th>任务</th><th>血缘指标</th><th>任务版本</th><th>当前版本</th><th>血缘状态</th><th>任务状态</th><th>一致</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in consistency.items" :key="String(item.task_id)" :data-testid="`consistency-row-${item.task_id}`">
            <td>{{ item['任务名称'] }}</td>
            <td>{{ item.lineage_metric }}</td>
            <td>{{ item.lineage_version }}</td>
            <td>{{ item.current_version }}</td>
            <td>{{ item.lineage_status }}</td>
            <td>{{ item.task_status }}</td>
            <td>{{ item.consistent ? '是' : '否' }}</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>回放引擎引用指标血缘当前版本，断链任务只生成验证失败报告</span>
      <span class="muted">{{ lastLoadedAt }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createReplayJob,
  executeReplayJob,
  listEntries,
  loadReplayConsistency,
  type ConsistencyReport,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type ReplayTask = EntryRow & {
  _lineage_metric?: string
  _lineage_version?: string
  _executable?: boolean
  _blocked_reason?: string
  _replay_report?: Record<string, unknown>
}

type ReplayReport = {
  task_id: number
  verdict: string
  executable: boolean
  reason?: string
  lineage_metric?: string
  lineage_version?: string
  broken_links?: number
  broken?: { stage: string; name: string }[]
  generated_at?: string
}

const tasks = ref<ReplayTask[]>([])
const lineageMetrics = ref<string[]>([])
const consistency = ref<ConsistencyReport | null>(null)
const report = ref<ReplayReport | null>(null)
const message = ref('')
const errorMessage = ref('')
const lastLoadedAt = ref('')

const form = ref({
  job_name: '序号23迁移回放',
  checkpoint_id: '2',
  topology_name: '任务拓扑样例2',
  lineage_metric: '',
})

const executableCount = computed(() => tasks.value.filter((task) => task._executable).length)
const blockedCount = computed(() => tasks.value.filter((task) => task._executable === false).length)

function stageLabel(stage: string): string {
  return { source: '上游源端', operator: '中间算子', output: '输出目标' }[stage] ?? stage
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    const [taskPayload, lineagePayload, consistencyPayload] = await Promise.all([
      listEntries('replay_engine'),
      listEntries('metric_lineage'),
      loadReplayConsistency(),
    ])
    tasks.value = taskPayload.items as ReplayTask[]
    lineageMetrics.value = (lineagePayload.items as EntryRow[]).map((row) => String(row['指标名称']))
    consistency.value = consistencyPayload
    if (!form.value.lineage_metric && lineageMetrics.value.length) {
      form.value.lineage_metric = lineageMetrics.value[0]
    }
    lastLoadedAt.value = new Date().toLocaleString()
    if (report.value) {
      const current = tasks.value.find((task) => Number(task.id) === report.value?.task_id)
      if (current?._replay_report) report.value = current._replay_report as unknown as ReplayReport
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回放数据读取失败'
  }
}

async function onCreate() {
  errorMessage.value = ''
  try {
    const result = await createReplayJob({ ...form.value, lineage_metric: form.value.lineage_metric || undefined })
    message.value = result.executable
      ? `回放任务 ${result.task_id} 已创建，引用血缘版本 ${result.lineage_version}，可执行`
      : `回放任务 ${result.task_id} 被阻断：${result.blocked_reason}，已生成验证失败报告`
    report.value = (result.verification_report as unknown as ReplayReport) ?? null
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '创建回放任务失败'
  }
}

async function onExecute(task: ReplayTask) {
  errorMessage.value = ''
  try {
    const result = await executeReplayJob(Number(task.id))
    message.value = result.ok
      ? `回放任务 ${task.id} 执行完成：${JSON.stringify(result.verification ?? {})}`
      : `回放任务 ${task.id} 未执行：${result.message ?? '被阻断'}`
    if (result.verification_report) report.value = result.verification_report as unknown as ReplayReport
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '执行回放失败'
  }
}

function onReport(task: ReplayTask) {
  report.value = (task._replay_report as unknown as ReplayReport) ?? {
    task_id: Number(task.id),
    verdict: String(task['结果验证'] ?? '待验证'),
    executable: task._executable !== false,
    reason: task._blocked_reason,
    lineage_metric: task._lineage_metric,
    lineage_version: task._lineage_version,
  }
}

onMounted(reloadAll)
</script>
