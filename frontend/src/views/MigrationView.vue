<template>
  <section class="page migration-page">
    <header class="page-head">
      <div>
        <h2>迁移部署 · 序号 23</h2>
        <p class="page-desc">
          序号 23 依赖与开发环境切换：数据库部署写入指标血缘数据，回放引擎在详情面板检查构建流水线，
          上线后接口读取迁移前后结果。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="deploy-reload" type="button" @click="reloadAll">重新加载</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="deploy-error">{{ errorMessage }}</p>

    <section class="stat-row">
      <article class="stat-card">
        <span class="stat-label">部署环境</span>
        <strong class="stat-value small">{{ panel?.environment ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">血缘数据记录</span>
        <strong class="stat-value" data-testid="deploy-records">{{ panel?.database_deployment.records ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">当前版本</span>
        <strong class="stat-value" data-testid="deploy-current">{{ panel?.database_deployment.current_versions ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">回放被阻断</span>
        <strong class="stat-value" data-testid="deploy-blocked">{{ panel?.replay_build.blocked ?? 0 }}</strong>
      </article>
    </section>

    <div class="two-column">
      <section class="panel" data-testid="deploy-lineage">
        <header class="panel-head">
          <h3>数据库部署 · 指标血缘存量</h3>
          <span class="muted">台账 {{ panel?.database_deployment.ledger_entries ?? 0 }} 条</span>
        </header>
        <table class="data-table compact">
          <thead><tr><th>指标</th><th>版本</th><th>环境</th><th>状态</th><th>当前</th><th>验证</th></tr></thead>
          <tbody>
            <tr v-for="(version, index) in panel?.lineage_versions ?? []" :key="`${version.指标名称}-${version.版本号}-${index}`">
              <td>{{ version.指标名称 }}</td>
              <td>{{ version.版本号 }}</td>
              <td>{{ version.环境 }}</td>
              <td>{{ version.status }}</td>
              <td>{{ version.current ? '是' : '' }}</td>
              <td>{{ version.verified ? '通过' : '断链' }}</td>
            </tr>
            <tr v-if="!(panel?.lineage_versions ?? []).length">
              <td colspan="6" class="empty-state">尚未部署血缘版本，请先在指标血缘页面刷新</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel" data-testid="deploy-replay-build">
        <header class="panel-head">
          <h3>回放引擎 · 构建流水线详情</h3>
          <span class="muted">
            任务 {{ panel?.replay_build.tasks ?? 0 }} · 可执行 {{ panel?.replay_build.executable ?? 0 }} ·
            阻断 {{ panel?.replay_build.blocked ?? 0 }}
          </span>
        </header>
        <table class="data-table compact">
          <thead><tr><th>任务 ID</th><th>任务名称</th><th>血缘指标</th><th>阻断原因</th></tr></thead>
          <tbody>
            <tr v-for="task in panel?.replay_build.blocked_tasks ?? []" :key="String(task.task_id)" :data-testid="`blocked-task-${task.task_id}`">
              <td>{{ task.task_id }}</td>
              <td>{{ task['任务名称'] }}</td>
              <td>{{ task.lineage_metric }}</td>
              <td class="error-text">{{ task.blocked_reason }}</td>
            </tr>
            <tr v-if="!(panel?.replay_build.blocked_tasks ?? []).length">
              <td colspan="4" class="empty-state">没有因路径不完整被阻断的回放任务</td>
            </tr>
          </tbody>
        </table>
        <p class="muted">同步页面：{{ (panel?.pages ?? []).join(' / ') }}</p>
      </section>
    </div>

    <section class="panel" data-testid="deploy-ledger">
      <header class="panel-head">
        <h3>迁移台账（其他页面同步更新）</h3>
        <span class="muted">
          切换 {{ ledger?.switches ?? 0 }} · 生效 {{ ledger?.applied ?? 0 }} ·
          保留旧路径 {{ ledger?.retained ?? 0 }} · 回滚 {{ ledger?.rolled_back ?? 0 }}
        </span>
      </header>
      <table class="data-table compact">
        <thead>
          <tr><th>时间</th><th>指标</th><th>模式</th><th>版本</th><th>当前版本</th><th>状态</th><th>断链</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="entry in ledger?.ledger ?? []" :key="String(entry.id)" :data-testid="`deploy-ledger-${entry.id}`">
            <td>{{ entry['时间'] }}</td>
            <td>{{ entry['指标名称'] }}</td>
            <td>{{ entry['迁移模式'] }}</td>
            <td>{{ entry['版本号'] }}</td>
            <td>{{ entry['当前消费版本'] }}</td>
            <td>{{ entry['状态'] }}</td>
            <td>{{ entry['断链数'] }}</td>
            <td class="muted">{{ entry['说明'] }}</td>
          </tr>
          <tr v-if="!ledger?.ledger.length">
            <td colspan="8" class="empty-state">暂无迁移台账</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>序号 23 迁移：指标血缘与回放引擎页面现象来自同一个原因（迁移路径不完整）</span>
      <span class="muted">{{ panel?.timestamp ?? '' }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  loadDeploymentPanel,
  loadMigrationLedger,
  type DeploymentPanel,
  type MigrationLedger,
} from '@/api/backend-service'

const panel = ref<DeploymentPanel | null>(null)
const ledger = ref<MigrationLedger | null>(null)
const errorMessage = ref('')

async function reloadAll() {
  errorMessage.value = ''
  try {
    const [panelPayload, ledgerPayload] = await Promise.all([
      loadDeploymentPanel(),
      loadMigrationLedger(),
    ])
    panel.value = panelPayload
    ledger.value = ledgerPayload
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '迁移部署数据读取失败'
  }
}

onMounted(reloadAll)
</script>
