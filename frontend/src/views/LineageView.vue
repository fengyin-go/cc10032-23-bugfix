<template>
  <section class="page lineage-page">
    <header class="page-head">
      <div>
        <h2>指标血缘 · 序号 23 迁移核对</h2>
        <p class="page-desc">
          刷新血缘会重新读取上下游引用并重算路径；存在断链时记录只会标记为「断链」，
          不会因为版本号提升而显示正常。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="refresh-all" type="button" @click="onRefreshAll">
          批量刷新血缘（同批次只提升一次版本）
        </button>
        <button class="btn" data-testid="reload" type="button" @click="reloadAll">重新加载</button>
      </div>
    </header>

    <p v-if="message" class="notice" data-testid="lineage-message">{{ message }}</p>
    <p v-if="errorMessage" class="error-text" data-testid="lineage-error">{{ errorMessage }}</p>

    <section class="stat-row">
      <article class="stat-card">
        <span class="stat-label">当前版本记录</span>
        <strong class="stat-value" data-testid="stat-current">{{ recordsWithVersion }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">可验证当前版本</span>
        <strong class="stat-value" data-testid="stat-verified">{{ verifiedCurrent }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">断链记录</span>
        <strong class="stat-value" data-testid="stat-broken">{{ brokenRecords }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">迁移台账条目</span>
        <strong class="stat-value" data-testid="stat-ledger">{{ ledger?.ledger.length ?? 0 }}</strong>
      </article>
    </section>

    <div class="lineage-layout">
      <div class="lineage-list">
        <table class="data-table">
          <thead>
            <tr>
              <th>指标名称</th>
              <th>版本号</th>
              <th>当前消费版本</th>
              <th>状态</th>
              <th>断链数</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in lineageRows"
              :key="String(row.id)"
              :data-testid="`lineage-row-${row['记录ID']}`"
              :data-status="String(row.status)"
              :class="{ selected: selectedId === Number(row.id) }"
              @click="selectRow(Number(row.id))"
            >
              <td>{{ row['指标名称'] }}</td>
              <td>{{ row['版本号'] }}</td>
              <td>{{ row['_current_version'] ?? '—' }}</td>
              <td>
                <span :class="['status-pill', `status-${row.status}`]">{{ row.status }}</span>
              </td>
              <td>{{ brokenCount(row) }}</td>
              <td class="row-actions">
                <button
                  class="link"
                  type="button"
                  :data-testid="`lineage-refresh-${row['记录ID']}`"
                  @click.stop="onRefreshRow(row)"
                >
                  刷新血缘
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`lineage-switch-${row['记录ID']}`"
                  @click.stop="onSwitch(row, 'replace')"
                >
                  迁移切换（整体替换）
                </button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`lineage-retain-${row['记录ID']}`"
                  @click.stop="onSwitch(row, 'retain_old')"
                >
                  保留旧路径
                </button>
              </td>
            </tr>
            <tr v-if="!lineageRows.length">
              <td colspan="6" class="empty-state">暂无血缘记录</td>
            </tr>
          </tbody>
        </table>

        <header class="panel-head">
          <h3>迁移台账</h3>
          <span class="muted">
            序号 {{ ledger?.sequence ?? 23 }} · 切换 {{ ledger?.switches ?? 0 }} 次 ·
            保留旧路径 {{ ledger?.retained ?? 0 }} · 回滚 {{ ledger?.rolled_back ?? 0 }}
          </span>
        </header>
        <table class="data-table" data-testid="migration-ledger">
          <thead>
            <tr><th>时间</th><th>指标</th><th>模式</th><th>来源 → 目标</th><th>版本</th><th>当前版本</th><th>状态</th><th>断链</th></tr>
          </thead>
          <tbody>
            <tr v-for="entry in ledger?.ledger ?? []" :key="String(entry.id)" :data-testid="`ledger-row-${entry.id}`">
              <td>{{ entry['时间'] }}</td>
              <td>{{ entry['指标名称'] }}</td>
              <td>{{ entry['迁移模式'] }}</td>
              <td>{{ entry['来源环境'] }} → {{ entry['目标环境'] }}</td>
              <td>{{ entry['版本号'] }}</td>
              <td>{{ entry['当前消费版本'] }}</td>
              <td>{{ entry['状态'] }}</td>
              <td>{{ entry['断链数'] }}</td>
            </tr>
            <tr v-if="!ledger?.ledger.length">
              <td colspan="8" class="empty-state">尚无迁移切换记录</td>
            </tr>
          </tbody>
        </table>
      </div>

      <aside class="lineage-detail" data-testid="lineage-detail">
        <h3>断链位置</h3>
        <template v-if="graph">
          <p class="muted">
            版本 {{ graph.current_version || graph.version }} · 签名 {{ graph.signature }} ·
            链接 {{ graph.total_links }} 个 · 断链 {{ graph.broken_links }} 个
          </p>
          <div class="dag-wrap">
            <svg class="dag-svg" :viewBox="`0 0 ${dagWidth} ${dagHeight}`" role="img">
              <defs>
                <marker id="arrowhead-broken" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
                  <path d="M0,0 L8,4 L0,8 z" fill="#ef4444" />
                </marker>
              </defs>
              <g v-for="edge in dagEdges" :key="`${edge.from}-${edge.to}`">
                <line
                  :x1="edge.x1" :y1="edge.y1" :x2="edge.x2" :y2="edge.y2"
                  :class="['dag-edge', { broken: edge.broken }]"
                  :marker-end="edge.broken ? 'url(#arrowhead-broken)' : undefined"
                />
              </g>
              <g
                v-for="node in dagNodes"
                :key="node.id"
                :class="['dag-node', { missing: !node.found }]"
              >
                <rect
                  :x="node.x" :y="node.y" :width="node.w" :height="node.h" rx="6"
                  :fill="node.found ? '#ffffff' : '#fef2f2'"
                  :stroke="node.found ? '#cbd5e1' : '#ef4444'"
                />
                <text :x="node.x + 8" :y="node.y + 18">{{ node.stageLabel }}</text>
                <text :x="node.x + 8" :y="node.y + 34" class="node-name">{{ node.label }}</text>
              </g>
            </svg>
          </div>
          <ul v-if="graph.broken.length" class="broken-list" data-testid="broken-list">
            <li v-for="item in graph.broken" :key="`${item.stage}-${item.name}`">
              {{ stageLabel(item.stage) }}「{{ item.name || '空引用' }}」缺失
              <span class="muted">{{ item.detail }}</span>
            </li>
          </ul>
          <p v-else class="ok-text" data-testid="lineage-ok">全部上下游链接完整，可被回放引擎消费</p>

          <h3>版本存量</h3>
          <table class="data-table compact" data-testid="lineage-versions">
            <thead><tr><th>版本</th><th>环境</th><th>状态</th><th>当前</th><th>验证</th></tr></thead>
            <tbody>
              <tr v-for="(version, index) in selectedVersions" :key="`${version.版本号}-${index}`">
                <td>{{ version.版本号 }}</td>
                <td>{{ version.环境 }}</td>
                <td>{{ version.状态 }}</td>
                <td>{{ version.current ? '是' : '' }}</td>
                <td>{{ version.verified ? '通过' : '断链' }}</td>
              </tr>
              <tr v-if="!selectedVersions.length">
                <td colspan="5" class="empty-state">暂无版本快照</td>
              </tr>
            </tbody>
          </table>
        </template>
        <p v-else class="muted">选择左侧任意指标查看血缘图谱与断链位置。</p>
      </aside>
    </div>

    <footer class="page-foot">
      <span>序号 23 迁移：指标血缘 → 部署页面 → 回放引擎 同源核对</span>
      <span class="muted">{{ ledger?.sequence ? `迁移序号 ${ledger.sequence}` : '' }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  listEntries,
  loadLineageGraph,
  loadMigrationLedger,
  refreshAllLineages,
  refreshLineage,
  switchEnvironment,
  type LineageGraph,
  type MigrationLedger,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

type LineageRow = EntryRow & {
  _current_version?: string
  _current_verified?: boolean
  _broken_links?: { stage: string; name: string }[]
  _versions?: { 版本号: string; 环境: string; 状态: string; current: boolean; verified: boolean }[]
}

const lineageRows = ref<LineageRow[]>([])
const ledger = ref<MigrationLedger | null>(null)
const graph = ref<LineageGraph | null>(null)
const selectedId = ref<number | null>(null)
const message = ref('')
const errorMessage = ref('')

const recordsWithVersion = computed(
  () => lineageRows.value.filter((row) => row._current_version).length,
)
const verifiedCurrent = computed(
  () => lineageRows.value.filter((row) => row._current_verified).length,
)
const brokenRecords = computed(
  () => lineageRows.value.filter((row) => row.status === '断链').length,
)
const selectedRow = computed(
  () => lineageRows.value.find((row) => Number(row.id) === selectedId.value) ?? null,
)
const selectedVersions = computed(() => selectedRow.value?._versions ?? [])

function brokenCount(row: LineageRow): number {
  return row._broken_links?.length ?? 0
}

function stageLabel(stage: string): string {
  return { source: '上游源端', operator: '中间算子', output: '输出目标' }[stage] ?? stage
}

const dagWidth = 360
const dagHeight = 330

const dagNodes = computed(() => {
  if (!graph.value) return []
  const stages: { stage: string; stageLabel: string }[] = [
    { stage: 'source', stageLabel: '源端' },
    { stage: 'operator', stageLabel: '算子' },
    { stage: 'output', stageLabel: '输出' },
  ]
  const nodes: { id: string; label: string; found: boolean; stageLabel: string; x: number; y: number; w: number; h: number }[] = []
  stages.forEach((stage, column) => {
    const items = graph.value!.nodes.filter((node) => node.stage === stage.stage)
    items.forEach((node, index) => {
      nodes.push({
        id: node.id,
        label: node.label,
        found: node.found,
        stageLabel: stage.stageLabel,
        x: 14 + column * 116,
        y: 16 + index * 82,
        w: 108,
        h: 46,
      })
    })
  })
  return nodes
})

const dagEdges = computed(() => {
  if (!graph.value) return []
  const positions = new Map(dagNodes.value.map((node) => [node.id, node]))
  return graph.value.edges
    .map((edge) => {
      const from = positions.get(edge.from)
      const to = positions.get(edge.to)
      if (!from || !to) return null
      return {
        from: edge.from,
        to: edge.to,
        broken: edge.broken,
        x1: from.x + from.w,
        y1: from.y + from.h / 2,
        x2: to.x,
        y2: to.y + to.h / 2,
      }
    })
    .filter((edge): edge is NonNullable<typeof edge> => edge !== null)
})

async function reloadRows() {
  const payload = await listEntries('metric_lineage')
  lineageRows.value = payload.items as LineageRow[]
  if (!selectedId.value && lineageRows.value.length) {
    await selectRow(Number(lineageRows.value[0].id))
  } else if (selectedId.value) {
    await selectRow(selectedId.value)
  }
}

async function reloadAll() {
  errorMessage.value = ''
  try {
    await reloadRows()
    ledger.value = await loadMigrationLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '血缘数据读取失败'
  }
}

async function selectRow(entryId: number) {
  selectedId.value = entryId
  try {
    graph.value = await loadLineageGraph(entryId)
  } catch (error) {
    graph.value = null
    errorMessage.value = error instanceof Error ? error.message : '血缘图谱读取失败'
  }
}

async function onRefreshAll() {
  errorMessage.value = ''
  try {
    const result = await refreshAllLineages()
    message.value = `批次 ${result.batch_id}：刷新 ${result.refreshed} 条，提升版本 ${result.version_bumped} 条，` +
      `正常 ${result.normal} 条，断链 ${result.broken} 条`
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量刷新失败'
  }
}

async function onRefreshRow(row: LineageRow) {
  errorMessage.value = ''
  try {
    const result = await refreshLineage(Number(row.id))
    message.value = result.verified
      ? `「${result.metric}」刷新完成：路径完整，版本 ${result.version}`
      : `「${result.metric}」刷新完成：发现 ${result.broken_links} 处断链，版本 ${result.version} 标记为断链`
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '刷新血缘失败'
  }
}

async function onSwitch(row: LineageRow, mode: 'replace' | 'retain_old') {
  errorMessage.value = ''
  try {
    const result = await switchEnvironment({
      metric_name: String(row['指标名称']),
      mode,
      // 序号 23 的迁移场景：上游删除与输出新增同时改变同一指标路径。
      removed_upstreams: ['流式源端样例2'],
      added_upstreams: ['流式源端样例1'],
      removed_outputs: ['结果落地样例2'],
      added_outputs: ['结果落地样例3'],
      switch_key: `seq23-${row['指标名称']}-${mode}-demo`,
    })
    message.value = `「${result.metric}」${mode === 'replace' ? '整体替换' : '保留旧路径'}：` +
      `${result.idempotent ? '重复切换按幂等返回' : '本次生效'}，状态 ${result.status}，当前版本 ${result.current_version}`
    await reloadAll()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移切换失败'
  }
}

onMounted(reloadAll)
</script>
