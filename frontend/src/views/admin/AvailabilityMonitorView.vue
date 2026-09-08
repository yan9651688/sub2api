<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { RouterLink } from 'vue-router'
import AppLayout from '@/components/layout/AppLayout.vue'
import BaseDialog from '@/components/common/BaseDialog.vue'
import Select from '@/components/common/Select.vue'
import OpsErrorDetailModal from './ops/components/OpsErrorDetailModal.vue'
import { opsAPI, type OpsDashboardOverview, type OpsErrorLogsResponse, type OpsSystemLog, type OpsSystemLogListResponse } from '@/api/admin/ops'

const SWITCH_EVENT = 'openai.upstream_failover_switching'
const COOLDOWN_EVENT = 'openai_model_transient_state'
type WindowRange = '5m' | '30m' | '1h'
interface Snapshot {
  range: WindowRange
  start: string
  end: string
  overview: OpsDashboardOverview
  switches: OpsSystemLogListResponse
  cooldowns: OpsSystemLogListResponse
  errors: OpsErrorLogsResponse
}

const { t } = useI18n()
const label = (key: string) => t(`admin.ops.availability.${key}`)
const range = ref<WindowRange>('30m')
const autoRefresh = ref(true)
const loading = ref(false)
const error = ref(false)
const snapshot = ref<Snapshot | null>(null)
const selectedErrorId = ref<number | null>(null)
const showError = ref(false)
const traceRequest = ref('')
const traceLogs = ref<OpsSystemLog[]>([])
const traceLoading = ref(false)
const traceError = ref(false)
let refreshController: AbortController | null = null
let traceController: AbortController | null = null
let timer: ReturnType<typeof setInterval> | undefined
let disposed = false

const rangeOptions = computed(() => (['5m', '30m', '1h'] as const).map(value => ({
  value, label: t(`admin.ops.timeRange.${value}`)
})))
const events = computed(() => {
  if (!snapshot.value) return []
  return [...snapshot.value.switches.items, ...snapshot.value.cooldowns.items]
    .filter(row => row.message === SWITCH_EVENT || row.message === COOLDOWN_EVENT)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
})
const finalResponse = computed(() => traceLogs.value.find(row =>
  row.message === 'http request completed' && row.request_id === traceRequest.value
))

function numberField(value: unknown): number | null {
  if (value === null || value === undefined || value === '') return null
  const result = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : NaN
  return Number.isFinite(result) ? result : null
}

function seconds(value: unknown): string {
  const ms = numberField(value)
  return ms === null ? '—' : t('admin.ops.availability.seconds', { value: (ms / 1000).toFixed(2) })
}

function time(value: string): string {
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : '—'
}

function eventDetail(row: OpsSystemLog): string {
  if (row.message === COOLDOWN_EVENT) {
    const duration = numberField(row.extra?.cooldown_ms)
    return t('admin.ops.availability.cooldownDuration', {
      seconds: duration === null ? '—' : (duration / 1000).toFixed(0),
      streak: numberField(row.extra?.failure_streak) ?? '—'
    })
  }
  return t('admin.ops.availability.upstreamStatus', { status: numberField(row.extra?.upstream_status) ?? '—' })
}

async function refresh(): Promise<void> {
  if (disposed || document.hidden) return
  refreshController?.abort()
  const controller = new AbortController()
  refreshController = controller
  loading.value = true
  error.value = false
  const selectedRange = range.value
  if (snapshot.value && snapshot.value.range !== selectedRange) snapshot.value = null
  const end = new Date().toISOString()
  const minutes = { '5m': 5, '30m': 30, '1h': 60 }[selectedRange]
  const start = new Date(Date.parse(end) - minutes * 60_000).toISOString()
  const window = { start_time: start, end_time: end }
  const options = { signal: controller.signal }
  try {
    // All four queries use the same window. Event logs deliberately omit the
    // platform filter because these OpenAI events may have a null platform.
    const [overview, switches, cooldowns, errors] = await Promise.all([
      opsAPI.getDashboardOverview({ ...window, platform: 'openai', mode: 'raw' }, options),
      opsAPI.listSystemLogs({ ...window, event: SWITCH_EVENT, page: 1, page_size: 20 }, options),
      opsAPI.listSystemLogs({ ...window, event: COOLDOWN_EVENT, page: 1, page_size: 20 }, options),
      opsAPI.listRequestErrors({ ...window, platform: 'openai', view: 'all', page: 1, page_size: 20 }, options)
    ])
    if (disposed || refreshController !== controller || controller.signal.aborted) return
    snapshot.value = { range: selectedRange, start, end, overview, switches, cooldowns, errors }
  } catch {
    if (refreshController === controller && !controller.signal.aborted && !disposed) {
      error.value = true
      controller.abort()
    }
  } finally {
    if (refreshController === controller && !disposed) loading.value = false
  }
}

async function openTrace(row: OpsSystemLog): Promise<void> {
  if (!row.request_id) return
  traceController?.abort()
  const controller = new AbortController()
  traceController = controller
  traceRequest.value = row.request_id
  traceLogs.value = []
  traceLoading.value = true
  traceError.value = false
  try {
    const result = await opsAPI.listSystemLogs({
      request_id: row.request_id, time_range: '24h', page: 1, page_size: 100
    }, { signal: controller.signal })
    if (disposed || traceController !== controller || controller.signal.aborted) return
    traceLogs.value = result.items.filter(item => item.request_id === row.request_id)
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
  } catch {
    if (traceController === controller && !controller.signal.aborted && !disposed) traceError.value = true
  } finally {
    if (traceController === controller && !disposed) traceLoading.value = false
  }
}

function closeTrace(): void {
  traceController?.abort()
  traceRequest.value = ''
}

function openError(id: number): void {
  selectedErrorId.value = id
  showError.value = true
}

function visibilityChanged(): void {
  if (document.hidden) {
    refreshController?.abort()
    if (traceLoading.value && traceRequest.value) traceError.value = true
    traceController?.abort()
    loading.value = false
  } else if (autoRefresh.value) {
    void refresh()
  }
}

watch(range, () => { void refresh() })
onMounted(() => {
  void refresh()
  timer = setInterval(() => {
    if (autoRefresh.value && !loading.value && !document.hidden) void refresh()
  }, 60_000)
  document.addEventListener('visibilitychange', visibilityChanged)
})
onUnmounted(() => {
  disposed = true
  refreshController?.abort()
  traceController?.abort()
  clearInterval(timer)
  document.removeEventListener('visibilitychange', visibilityChanged)
})
</script>

<template>
  <AppLayout>
    <div class="space-y-6 pb-10">
      <header class="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div class="mb-2 text-xs font-medium text-primary-600 dark:text-primary-400">{{ label('scope') }}</div>
          <h1 class="text-2xl font-semibold text-gray-900 dark:text-white">{{ label('title') }}</h1>
          <p class="mt-2 text-sm text-gray-500 dark:text-gray-400">{{ label('description') }}</p>
        </div>
        <div class="flex flex-wrap items-center gap-3">
          <Select v-model="range" :options="rangeOptions" class="w-32" />
          <button class="btn btn-secondary" :disabled="loading" data-testid="refresh" @click="refresh">{{ label('refresh') }}</button>
          <RouterLink to="/admin/ops" class="text-sm text-primary-600 hover:underline">{{ label('openOps') }}</RouterLink>
        </div>
      </header>

      <div class="flex flex-wrap items-center justify-between gap-3 text-xs text-gray-500 dark:text-gray-400">
        <label class="flex cursor-pointer items-center gap-2"><input v-model="autoRefresh" type="checkbox" class="rounded border-gray-300" />{{ label('autoRefresh') }}</label>
        <span v-if="snapshot">{{ t('admin.ops.availability.updatedAt', { time: time(snapshot.end) }) }}</span>
      </div>
      <div v-if="error" role="alert" class="rounded-xl bg-amber-50 p-4 text-sm text-amber-800 dark:bg-amber-950 dark:text-amber-200">{{ label('refreshFailed') }}</div>
      <div v-if="loading && !snapshot" role="status" class="card p-10 text-center text-sm text-gray-500">{{ label('loading') }}</div>

      <template v-if="snapshot">
        <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
          <section v-for="metric in [
            { key: 'switches', value: snapshot.switches.total },
            { key: 'cooldowns', value: snapshot.cooldowns.total },
            { key: 'errors', value: snapshot.errors.total }
          ]" :key="metric.key" class="card p-5">
            <h2 class="text-sm font-medium text-gray-500 dark:text-gray-400">{{ label(metric.key) }}</h2>
            <div class="mt-3 text-3xl font-semibold tabular-nums text-gray-900 dark:text-white" :data-testid="metric.key">{{ metric.value }}</div>
            <p class="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">{{ label(metric.key + 'Hint') }}</p>
          </section>
          <section class="card p-5">
            <h2 class="text-sm font-medium text-gray-500 dark:text-gray-400">{{ label('ttft') }}</h2>
            <div data-testid="ttft" class="mt-3 text-3xl font-semibold tabular-nums" :class="(snapshot.overview.ttft.p95_ms ?? 0) > 5000 ? 'text-amber-600 dark:text-amber-400' : 'text-gray-900 dark:text-white'">{{ seconds(snapshot.overview.ttft.p95_ms) }}</div>
            <p class="mt-3 text-xs leading-5 text-gray-500 dark:text-gray-400">{{ label('ttftHint') }}</p>
          </section>
        </div>
        <p class="text-xs leading-6 text-gray-500 dark:text-gray-400">{{ label('ttftNote') }}</p>

        <section class="card overflow-hidden">
          <div class="border-b border-gray-100 p-5 dark:border-dark-700">
            <h2 class="font-semibold text-gray-900 dark:text-white">{{ label('eventsTitle') }}</h2>
            <p class="mt-1 text-xs text-gray-500">{{ label('eventsHint') }}</p>
          </div>
          <div v-if="!events.length" class="p-10 text-center text-sm text-gray-500">{{ label('emptyEvents') }}</div>
          <div v-else class="overflow-x-auto">
            <table class="w-full text-left text-sm">
              <thead class="bg-gray-50 text-xs text-gray-500 dark:bg-dark-900"><tr><th v-for="key in ['time', 'event', 'account', 'model', 'detail', 'requestTrace']" :key="key" class="whitespace-nowrap px-5 py-3">{{ label(key) }}</th></tr></thead>
              <tbody class="divide-y divide-gray-100 dark:divide-dark-700">
                <tr v-for="row in events" :key="row.id">
                  <td class="whitespace-nowrap px-5 py-4 text-xs text-gray-500">{{ time(row.created_at) }}</td>
                  <td class="whitespace-nowrap px-5 py-4"><span class="rounded-full px-2 py-1 text-xs" :class="row.message === COOLDOWN_EVENT ? 'bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300' : 'bg-blue-50 text-blue-700 dark:bg-blue-950 dark:text-blue-300'">{{ label(row.message === COOLDOWN_EVENT ? 'cooldownEvent' : 'switchEvent') }}</span></td>
                  <td class="px-5 py-4 tabular-nums">{{ row.account_id == null ? '—' : '#' + row.account_id }}</td>
                  <td class="px-5 py-4">{{ row.model || '—' }}</td>
                  <td class="whitespace-nowrap px-5 py-4 text-xs">{{ eventDetail(row) }}</td>
                  <td class="px-5 py-4"><button v-if="row.request_id" class="whitespace-nowrap text-primary-600 hover:underline" data-testid="trace" @click="openTrace(row)">{{ label('requestTrace') }}</button><span v-else :title="label('noTrace')" class="text-gray-400">—</span></td>
                </tr>
              </tbody>
            </table>
          </div>
        </section>

        <section class="card overflow-hidden">
          <div class="flex flex-wrap items-center justify-between gap-3 border-b border-gray-100 p-5 dark:border-dark-700">
            <div><h2 class="font-semibold text-gray-900 dark:text-white">{{ label('errorsTitle') }}</h2><p class="mt-1 text-xs text-gray-500">{{ label('errorsListHint') }}</p></div>
            <RouterLink to="/admin/usage" class="text-xs text-primary-600 hover:underline">{{ label('openErrors') }}</RouterLink>
          </div>
          <div v-if="!snapshot.errors.items.length" class="p-10 text-center text-sm text-gray-500">{{ label('emptyErrors') }}</div>
          <div v-else class="divide-y divide-gray-100 dark:divide-dark-700">
            <button v-for="row in snapshot.errors.items" :key="row.id" class="flex w-full flex-wrap items-start gap-3 px-5 py-4 text-left hover:bg-gray-50 dark:hover:bg-dark-800" data-testid="request-error" @click="openError(row.id)">
              <span class="rounded-lg bg-red-50 px-2 py-1 text-xs font-semibold tabular-nums text-red-700 dark:bg-red-950 dark:text-red-300">{{ row.status_code }}</span>
              <div class="min-w-0 flex-1"><div class="break-words text-sm text-gray-900 dark:text-white">{{ row.message.slice(0, 320) || row.type }}</div><div class="mt-2 flex flex-wrap gap-3 text-xs text-gray-500"><span>{{ row.account_name || (row.account_id == null ? '—' : '#' + row.account_id) }}</span><span>{{ row.model || '—' }}</span><span>{{ row.phase }}</span></div></div>
              <time class="text-xs text-gray-500">{{ time(row.created_at) }}</time>
            </button>
          </div>
        </section>
        <footer class="space-y-1 text-xs leading-6 text-gray-500 dark:text-gray-400"><p>{{ t('admin.ops.availability.windowNote', { start: time(snapshot.start), end: time(snapshot.end) }) }}</p><p>{{ label('recordsNote') }}</p></footer>
      </template>
    </div>

    <OpsErrorDetailModal v-model:show="showError" :error-id="selectedErrorId" error-type="request" />
    <BaseDialog :show="!!traceRequest" :title="label('traceTitle')" width="extra-wide" @close="closeTrace">
      <div class="space-y-4 p-4">
        <p class="break-all font-mono text-xs text-gray-500">{{ label('requestId') }}: {{ traceRequest }}</p>
        <p class="text-xs text-gray-500">{{ label('traceHint') }}</p>
        <p v-if="traceLoading" role="status">{{ label('loading') }}</p>
        <p v-else-if="traceError" role="alert" class="text-amber-700">{{ label('traceFailed') }}</p>
        <template v-else>
          <div class="rounded-xl bg-gray-50 p-4 dark:bg-dark-900">
            <p v-if="finalResponse" data-testid="final-response" class="font-medium">{{ label('finalResponse') }}: {{ numberField(finalResponse.extra?.status_code) ?? '—' }} · {{ label('account') }} #{{ finalResponse.account_id ?? '—' }}</p>
            <p v-else>{{ label('pendingResponse') }}</p>
            <p class="mt-2 text-xs text-gray-500">{{ label('streamNote') }}</p>
          </div>
          <p v-if="!traceLogs.length" class="text-sm text-gray-500">{{ label('noTraceLogs') }}</p>
          <div v-for="row in traceLogs" :key="row.id" class="border-b border-gray-100 pb-3 text-xs dark:border-dark-700">
            <div class="flex flex-wrap gap-3 text-gray-500"><span>{{ time(row.created_at) }}</span><span>{{ row.level }}</span><span v-if="row.account_id">{{ label('account') }} #{{ row.account_id }}</span></div>
            <p class="mt-2 break-words font-mono">{{ row.message.slice(0, 500) }}</p>
          </div>
        </template>
      </div>
    </BaseDialog>
  </AppLayout>
</template>
