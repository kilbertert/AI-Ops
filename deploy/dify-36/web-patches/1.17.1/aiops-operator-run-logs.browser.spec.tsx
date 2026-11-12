import { render } from 'vitest-browser-react'
import AppLogs from '@/app/components/aiops-operator-run-logs'

vi.mock('@/app/components/app/store', () => ({ useStore: () => 'app-a' }))
vi.mock('@/app/components/app/log-annotation', () => ({ default: () => <p>原生日志</p> }))

const run = {
  run_id: 'run-a',
  qa_id: 'qa-a',
  created_at: '2026-11-10T10:00:00Z',
  state: 'succeeded',
  elapsed_ms: 1500,
  result_expired: true,
  source: 'java',
  gateway_version: '1.0',
  node_build: 'build-a',
  agent_version: null,
  trace: [
    {
      phase: 'tool',
      tool: 'cpms_order_detail',
      status: 'succeeded',
      at: '2026-11-10T10:00:01Z',
      elapsed_ms: 1000,
      duration_ms: 250,
    },
  ],
}

afterEach(() => vi.unstubAllGlobals())

function stub(routes: (body: Record<string, unknown>) => Response) {
  const requests: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      requests.push(body)
      if (body.action === 'status') return Response.json({ managed: true, entry: 'operator' })
      if (body.action === 'readiness') return Response.json({ can_diagnose: true })
      return routes(body)
    }),
  )
  return requests
}

it('opens actual run metadata from the existing logs page and clears it on revocation', async () => {
  let revoked = false
  const requests = stub((body) => {
    if (revoked)
      return Response.json({ error: { code: 'OPERATOR_RUN_NOT_FOUND' } }, { status: 403 })
    return Response.json(body.action === 'run-log' ? run : { items: [run], has_more: false })
  })
  const screen = await render(<AppLogs />)
  await expect.element(screen.getByRole('heading', { name: 'Java 运行日志' })).toBeVisible()
  await expect.element(screen.getByText('成功（结果已过期）')).toBeVisible()
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect.element(screen.getByText(/cpms_order_detail/)).toBeVisible()
  await expect.element(screen.getByText(/工具耗时 0.25 秒/)).toBeVisible()
  await expect.element(screen.getByText(/累计 1.00 秒/)).toBeVisible()
  expect(requests).toContainEqual({ action: 'run-log', app_id: 'app-a', run_id: 'run-a' })
  revoked = true
  await screen.getByRole('button', { name: '刷新日志' }).click()
  await expect.element(screen.getByRole('alert')).toBeVisible()
  await expect.element(screen.getByText(/cpms_order_detail/)).not.toBeInTheDocument()
  await expect.element(screen.getByText('原生日志')).not.toBeInTheDocument()
})

it('paginates and explicitly labels old runs without trace', async () => {
  const requests = stub((body) =>
    Response.json(
      body.action === 'run-log'
        ? { ...run, trace: [] }
        : { items: [run], has_more: body.page === 1 },
    ),
  )
  const screen = await render(<AppLogs />)
  await screen.getByRole('button', { name: '下一页' }).click()
  await expect.element(screen.getByText('第 2 页')).toBeVisible()
  expect(requests).toContainEqual({ action: 'run-logs', app_id: 'app-a', page: 2, limit: 20 })
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect.element(screen.getByText('阶段耗时未采集')).toBeVisible()
})

it('distinguishes evidence delivery from a completed diagnosis and shows the bounded reason', async () => {
  const partial = { ...run, delivery_mode: 'evidence_only', review_reason: 'unsupported_currency' }
  stub((body) =>
    Response.json(body.action === 'run-log' ? partial : { items: [partial], has_more: false }),
  )
  const screen = await render(<AppLogs />)
  await expect.element(screen.getByText('仅交付证据（诊断未完成）（结果已过期）')).toBeVisible()
  await expect.element(screen.getByText('成功（结果已过期）')).not.toBeInTheDocument()
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect.element(screen.getByText('诊断未完成原因：答复添加了来源未提供的币种')).toBeVisible()
})

it('shows an expired inflight run and does not infer missing tool duration from elapsed time', async () => {
  const expired = { ...run, state: 'expired', trace: [{ ...run.trace[0], duration_ms: undefined }] }
  stub((body) =>
    Response.json(body.action === 'run-log' ? expired : { items: [expired], has_more: false }),
  )
  const screen = await render(<AppLogs />)
  await expect.element(screen.getByText('已过期（结果已过期）')).toBeVisible()
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect.element(screen.getByText(/工具耗时 未采集/)).toBeVisible()
})

it.each([{ managed: false }, { managed: true, entry: 'consumer' }])(
  'preserves native logs for %j',
  async (status) => {
    const requests: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url, options) => {
        requests.push(JSON.parse(options.body).action)
        return Response.json(status)
      }),
    )
    const screen = await render(<AppLogs />)
    await expect.element(screen.getByText('原生日志')).toBeVisible()
    expect(requests).not.toContain('run-logs')
  },
)

it('does not fall back to native logs when app authorization fails', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ error: { code: 'SETUP_APP_FORBIDDEN' } }, { status: 403 })),
  )
  const screen = await render(<AppLogs />)
  await expect.element(screen.getByRole('alert')).toBeVisible()
  await expect.element(screen.getByText('原生日志')).not.toBeInTheDocument()
})
