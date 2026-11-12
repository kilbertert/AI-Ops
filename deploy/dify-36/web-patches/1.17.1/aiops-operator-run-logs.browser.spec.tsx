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
  question: '这笔订单怎么没结算',
  order_no: 'ORDER-1',
  reason: null,
  language: 'zh',
  environment: 'prod',
  expected_node_id: 'node-a',
  actual_node_id: 'node-a',
  expires_at: '2026-11-11T10:00:00Z',
  evidence_tools: ['cpms_order_detail'],
  tool_summaries: {
    cpms_order_detail: { 入参: 'order_no=ORDER-1', 状态: '已支付', 应付: '8.00' },
  },
  trace: [
    {
      phase: 'tool',
      tool: 'cpms_order_detail',
      status: 'refused',
      at: '2026-11-10T10:00:00Z',
      elapsed_ms: 500,
      duration_ms: 100,
      detail: 'CPMS_SCOPE_MISMATCH',
    },
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
  await expect
    .element(
      screen.getByText(
        /第 1 次调用 订单详情：订单不在可见范围或不存在 · 耗时 0.10 秒 · 累计 0.50 秒/,
      ),
    )
    .toBeVisible()
  await expect
    .element(
      screen.getByText(/第 2 次调用 订单详情：成功 · 耗时 0.25 秒 · 累计 1.00 秒/),
    )
    .toBeVisible()
  await expect.element(screen.getByText(/入参=order_no=ORDER-1 · 状态=已支付 · 应付=8.00/)).toBeVisible()
  // 摘要按工具名覆盖保存，只属于最近一次调用：被拒绝的历史行不重复显示它。
  expect(document.body.textContent?.match(/入参=order_no=ORDER-1/g)).toHaveLength(1)
  expect(requests).toContainEqual({ action: 'run-log', app_id: 'app-a', run_id: 'run-a' })
  revoked = true
  await screen.getByRole('button', { name: '刷新日志' }).click()
  await expect.element(screen.getByRole('alert')).toBeVisible()
  await expect.element(screen.getByText(/cpms_order_detail/)).not.toBeInTheDocument()
  await expect.element(screen.getByText('原生日志')).not.toBeInTheDocument()
})

it('shows question, order, terminal code and per-event refusal codes in detail', async () => {
  const failed = {
    ...run,
    state: 'failed',
    reason: 'scope_unreadable',
    trace: [
      {
        phase: 'tool',
        tool: 'cpms_order_detail',
        status: 'refused',
        at: '2026-11-10T10:00:01Z',
        elapsed_ms: 900,
        duration_ms: 200,
        detail: 'CPMS_SCOPE_MISMATCH',
      },
      {
        phase: 'failed',
        status: 'failed',
        at: '2026-11-10T10:00:02Z',
        elapsed_ms: 1500,
        detail: 'scope_unreadable',
      },
    ],
  }
  stub((body) =>
    Response.json(body.action === 'run-log' ? failed : { items: [failed], has_more: false }),
  )
  const screen = await render(<AppLogs />)
  await expect.element(screen.getByText('这笔订单怎么没结算')).toBeVisible()
  await expect.element(screen.getByText('ORDER-1')).toBeVisible()
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect.element(screen.getByText('问题：这笔订单怎么没结算')).toBeVisible()
  await expect.element(screen.getByText(/终态原因：权限范围读取失败/)).toBeVisible()
  await expect.element(screen.getByText(/订单不在可见范围或不存在/)).toBeVisible()
  await expect.element(screen.getByText(/执行节点：node-a → 实际 node-a/)).toBeVisible()
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

it('labels a delivered diagnosis review reason as observation, not incompletion', async () => {
  // ADR-0252：verdict 只观察不拦截——delivery_mode=diagnosis 时复核原因不能再叫「诊断未完成」。
  const delivered = { ...run, delivery_mode: 'diagnosis', review_reason: 'unsupported_claim' }
  stub((body) =>
    Response.json(body.action === 'run-log' ? delivered : { items: [delivered], has_more: false }),
  )
  const screen = await render(<AppLogs />)
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect
    .element(screen.getByText('复核观察（答复已交付）：答复未通过事实一致性复核'))
    .toBeVisible()
  await expect.element(screen.getByText(/诊断未完成原因/)).not.toBeInTheDocument()
})

it('shows an expired inflight run and does not infer missing tool duration from elapsed time', async () => {
  const expired = { ...run, state: 'expired', trace: [{ ...run.trace[0], duration_ms: undefined }] }
  stub((body) =>
    Response.json(body.action === 'run-log' ? expired : { items: [expired], has_more: false }),
  )
  const screen = await render(<AppLogs />)
  await expect.element(screen.getByText('已过期（结果已过期）')).toBeVisible()
  await screen.getByRole('button', { name: '查看运行' }).click()
  await expect.element(screen.getByText(/耗时 未采集/)).toBeVisible()
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
