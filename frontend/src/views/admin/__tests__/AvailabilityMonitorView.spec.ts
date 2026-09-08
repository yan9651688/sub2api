import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub, type VueWrapper } from '@vue/test-utils'
import AvailabilityMonitorView from '../AvailabilityMonitorView.vue'

const api = vi.hoisted(() => ({
  getDashboardOverview: vi.fn(),
  listSystemLogs: vi.fn(),
  listRequestErrors: vi.fn()
}))
vi.mock('@/api/admin/ops', () => ({ opsAPI: api, default: api }))
vi.mock('vue-i18n', async (importOriginal) => ({
  ...await importOriginal<typeof import('vue-i18n')>(),
  useI18n: () => ({
    t: (key: string, values?: Record<string, unknown>) => key + (values ? JSON.stringify(values) : '')
  })
}))

const switchEvent = { id: 1, created_at: '2026-09-05T04:59:00Z', message: 'openai.upstream_failover_switching', account_id: 3, request_id: 'req-one', model: 'test-model', extra: { upstream_status: 502 } }
const cooldownEvent = { id: 2, created_at: '2026-09-05T04:59:01Z', message: 'openai_model_transient_state', account_id: 3, platform: null, model: 'test-model', extra: { cooldown_ms: 10000, failure_streak: 1 } }
const page = (items: unknown[], total = items.length) => ({ items, total, page: 1, page_size: 20, pages: 1 })
let wrapper: VueWrapper | undefined
let hidden: ReturnType<typeof vi.spyOn>

function render() {
  wrapper = mount(AvailabilityMonitorView, { global: { stubs: {
    AppLayout: { template: '<div><slot /></div>' },
    RouterLink: RouterLinkStub,
    Select: { props: ['modelValue', 'options'], emits: ['update:modelValue'], template: '<select :value="modelValue" @change="$emit(\'update:modelValue\', $event.target.value)"><option v-for="o in options" :key="o.value" :value="o.value">{{ o.label }}</option></select>' },
    BaseDialog: { props: ['show'], template: '<div v-if="show" data-testid="dialog"><slot /></div>' },
    OpsErrorDetailModal: { props: ['show', 'errorId'], template: '<div v-if="show" data-testid="error-detail">{{ errorId }}</div>' }
  } } })
  return wrapper
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'] })
  vi.setSystemTime(new Date('2026-09-05T05:00:00Z'))
  vi.clearAllMocks()
  hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(false)
  api.getDashboardOverview.mockResolvedValue({ ttft: { p95_ms: 6000 } })
  api.listSystemLogs.mockImplementation(async (params) => page(params.event === 'openai.upstream_failover_switching' ? [switchEvent] : [cooldownEvent]))
  api.listRequestErrors.mockResolvedValue(page([{ id: 9, created_at: '2026-09-05T04:59:02Z', status_code: 503, message: 'Upstream failed', type: 'upstream_error', model: 'test-model', account_id: 3, account_name: 'Fixture account', phase: 'upstream' }]))
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = undefined
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('AvailabilityMonitorView', () => {
  it('uses one window and exact event filters, retaining cooldowns with no platform or request ID', async () => {
    const view = render()
    await flushPromises()
    expect(api.listSystemLogs).toHaveBeenCalledTimes(2)
    const params = api.listSystemLogs.mock.calls[0][0]
    expect(params).toMatchObject({ start_time: '2026-09-05T04:30:00.000Z', end_time: '2026-09-05T05:00:00.000Z', event: 'openai.upstream_failover_switching', page_size: 20 })
    expect(params).not.toHaveProperty('platform')
    expect(params).not.toHaveProperty('q')
    expect(api.getDashboardOverview.mock.calls[0][0]).toMatchObject({ start_time: params.start_time, end_time: params.end_time, platform: 'openai' })
    expect(view.get('[data-testid="switches"]').text()).toBe('1')
    expect(view.text()).toContain('cooldownEvent')
    expect(view.findAll('[data-testid="trace"]')).toHaveLength(1)
    expect(view.text()).toContain('ttftNote')
    await view.get('[data-testid="request-error"]').trigger('click')
    expect(view.get('[data-testid="error-detail"]').text()).toBe('9')
  })

  it('does not present missing TTFT or an initial fetch failure as healthy zero values', async () => {
    api.getDashboardOverview.mockResolvedValue({ ttft: { p95_ms: null } })
    const view = render()
    await flushPromises()
    expect(view.get('[data-testid="ttft"]').text()).toBe('—')
    view.unmount()
    api.getDashboardOverview.mockRejectedValue(new Error('unavailable'))
    const failed = render()
    await flushPromises()
    expect(failed.find('[role="alert"]').exists()).toBe(true)
    expect(failed.find('[data-testid="switches"]').exists()).toBe(false)
    expect(failed.text()).not.toContain('emptyEvents')
  })

  it('retains the last coherent snapshot when one refresh query fails', async () => {
    const view = render()
    await flushPromises()
    api.listSystemLogs.mockRejectedValueOnce(new Error('timeout'))
    api.listRequestErrors.mockResolvedValue(page([], 0))
    await view.get('[data-testid="refresh"]').trigger('click')
    await flushPromises()
    expect(view.find('[role="alert"]').exists()).toBe(true)
    expect(view.get('[data-testid="errors"]').text()).toBe('1')
    expect(view.get('[data-testid="switches"]').text()).toBe('1')
  })

  it('ignores a stale response after the time window changes', async () => {
    const view = render()
    await flushPromises()
    let resolveOld: (value: unknown) => void = () => {}
    api.getDashboardOverview.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    await view.get('[data-testid="refresh"]').trigger('click')
    const oldSignal = api.getDashboardOverview.mock.calls.at(-1)![1].signal
    await view.get('select').setValue('5m')
    await flushPromises()
    expect(oldSignal.aborted).toBe(true)
    resolveOld({ ttft: { p95_ms: 99000 } })
    await flushPromises()
    expect(view.get('[data-testid="ttft"]').text()).toContain('6.00')
    expect(view.get('[data-testid="ttft"]').text()).not.toContain('99.00')
  })

  it('pauses automatic reads while hidden and stops them when unmounted', async () => {
    const view = render()
    await flushPromises()
    hidden.mockReturnValue(true)
    document.dispatchEvent(new Event('visibilitychange'))
    vi.advanceTimersByTime(120_000)
    await flushPromises()
    expect(api.getDashboardOverview).toHaveBeenCalledTimes(1)
    hidden.mockReturnValue(false)
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(api.getDashboardOverview).toHaveBeenCalledTimes(2)
    view.unmount()
    vi.advanceTimersByTime(120_000)
    expect(api.getDashboardOverview).toHaveBeenCalledTimes(2)
  })

  it('shows the correlated final account and HTTP status without claiming stream success or displaying raw extra data', async () => {
    const view = render()
    await flushPromises()
    api.listSystemLogs.mockResolvedValueOnce(page([
      { id: 20, request_id: 'different', message: 'http request completed', account_id: 99, created_at: '2026-09-05T05:00:00Z', extra: { status_code: 500 } },
      { id: 21, request_id: 'req-one', message: 'http request completed', account_id: 4, created_at: '2026-09-05T04:59:03Z', extra: { status_code: 200, token: 'do-not-render-extra' } }
    ]))
    await view.get('[data-testid="trace"]').trigger('click')
    await flushPromises()
    expect(view.get('[data-testid="final-response"]').text()).toContain('200')
    expect(view.get('[data-testid="final-response"]').text()).toContain('#4')
    expect(view.text()).toContain('streamNote')
    expect(view.text()).not.toContain('do-not-render-extra')
    expect(view.text()).not.toContain('#99')
  })
})
