import { fetchJson, request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 序号 23：指标血缘 ↔ 回放引擎联动 ----

export type LineageCheck = {
  stage: string
  name: string
  found: boolean
  module?: string | null
  reason?: string
  detail?: string
}

export type LineageGraph = {
  ok: boolean
  metric: string
  version: string
  current_version: string
  current_verified: boolean
  status: string
  signature: string
  nodes: { id: string; stage: string; label: string; found: boolean; module?: string | null }[]
  edges: { from: string; to: string; broken: boolean }[]
  broken: LineageCheck[]
  broken_links: number
  total_links: number
  sources: string[]
  operators: string[]
  outputs: string[]
}

export type RefreshResult = {
  ok: boolean
  batch_id: string
  refreshed: number
  version_bumped: number
  normal: number
  broken: number
  records: { id: number; metric: string; version: string; status: string; bumped: boolean; broken_links: number }[]
}

export type SingleRefreshResult = {
  ok: boolean
  bumped: boolean
  idempotent: boolean
  batch_id: string
  metric: string
  version: string
  current_version: string
  status: string
  verified: boolean
  broken_links: number
  broken: LineageCheck[]
  checks: LineageCheck[]
  path: { sources: string[]; operators: string[]; outputs: string[] }
}

export type SwitchResult = {
  ok: boolean
  applied: boolean
  idempotent: boolean
  rolled_back: boolean
  switch_key: string
  metric: string
  mode: string
  status: string
  version: string
  current_version: string
  current_verified: boolean
  broken_links: number
  broken: LineageCheck[]
  before?: { 版本号: string; current_version: string; status: string }
  after?: { 版本号: string; current_version: string; status: string }
  message: string
}

export type MigrationLedger = {
  ok: boolean
  sequence: number
  ledger: Record<string, string | number | boolean>[]
  versions: { 指标名称: string; 版本号: string; 环境: string; 状态: string; current: boolean; verified: boolean }[]
  revisions: Record<string, unknown>[]
  switches: number
  applied: number
  retained: number
  rolled_back: number
}

export type DeploymentPanel = {
  ok: boolean
  sequence: number
  environment: string
  database_deployment: {
    artifact: string
    records: number
    current_versions: number
    broken: number
    ledger_entries: number
  }
  lineage_versions: { 指标名称: string; 版本号: string; 环境: string; current: boolean; verified: boolean; status: string }[]
  replay_build: {
    tasks: number
    executable: number
    blocked: number
    blocked_tasks: { task_id: number; 任务名称: string; lineage_metric: string; blocked_reason: string }[]
  }
  pages: string[]
  timestamp: string
}

export type ReplayJobResult = {
  ok: boolean
  task_id: number
  task_name: string
  lineage_metric: string
  lineage_version: string
  lineage_verified: boolean
  executable: boolean
  status: string
  blocked_reason: string
  broken_links: number
  broken: LineageCheck[]
  verification_report: Record<string, unknown> | null
}

export type ConsistencyReport = {
  ok: boolean
  checked: number
  consistent: number
  mismatches: Record<string, unknown>[]
  items: Record<string, unknown>[]
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  const payload = (await response.json()) as T & { detail?: string; error?: string }
  if (!response.ok) {
    throw new Error(payload.detail ?? payload.error ?? `接口返回 ${response.status}`)
  }
  return payload
}

export async function loadLineageGraph(entryId: number): Promise<LineageGraph> {
  return fetchJson<LineageGraph>(`/api/lineage/${entryId}/graph`)
}

export async function refreshLineage(entryId: number, batchId?: string): Promise<SingleRefreshResult> {
  return postJson<SingleRefreshResult>(`/api/lineage/${entryId}/refresh`, { batch_id: batchId ?? null })
}

export async function refreshAllLineages(batchId?: string): Promise<RefreshResult> {
  return postJson<RefreshResult>('/api/lineage/refresh-all', { batch_id: batchId ?? null })
}

export async function switchEnvironment(payload: {
  metric_name: string
  switch_key?: string
  mode: 'replace' | 'retain_old'
  source_env?: string
  target_env?: string
  removed_upstreams?: string[]
  added_upstreams?: string[]
  removed_operators?: string[]
  added_operators?: string[]
  removed_outputs?: string[]
  added_outputs?: string[]
}): Promise<SwitchResult> {
  return postJson<SwitchResult>('/api/lineage/switch-env', payload)
}

export async function loadMigrationLedger(): Promise<MigrationLedger> {
  return fetchJson<MigrationLedger>('/api/lineage/migration-ledger')
}

export async function loadDeploymentPanel(): Promise<DeploymentPanel> {
  return fetchJson<DeploymentPanel>('/api/lineage/deployment-panel')
}

export async function createReplayJob(payload: {
  job_name: string
  checkpoint_id: string
  topology_name?: string
  mode?: string
  lineage_metric?: string
}): Promise<ReplayJobResult> {
  return postJson<ReplayJobResult>('/api/replay/jobs', payload)
}

export async function executeReplayJob(taskId: number): Promise<{
  ok: boolean
  task_id: number
  executable: boolean
  message?: string
  verification?: Record<string, unknown>
  verification_report?: Record<string, unknown>
}> {
  return postJson(`/api/replay/${taskId}/execute`)
}

export async function loadReplayConsistency(): Promise<ConsistencyReport> {
  return fetchJson<ConsistencyReport>('/api/replay/consistency')
}
