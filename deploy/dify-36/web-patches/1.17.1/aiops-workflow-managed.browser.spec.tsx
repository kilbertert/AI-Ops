import { render } from 'vitest-browser-react'
import { WorkflowManagedStrip } from '@/app/components/aiops-workflow-managed'
import { publishManagedWorkflow } from '@/service/aiops-management'

const draft = vi.hoisted(() => ({ hash: 'saved-hash' }))
vi.mock('@/service/workflow', () => ({ fetchWorkflowDraft: async () => draft }))
const status = {
  managed: true,
  app_id: 'app-c',
  platform: false,
  model: '',
  profile: '',
  model_options: {},
  version_no: 4,
  route_revision: 2,
  published: true,
  published_prompt: '',
  published_tools: [],
  entry: 'consumer' as const,
  knowledge_base_ids: ['kb-1'],
  channel: 'pending',
  channel_note: 'AI 已准备，渠道待接入',
  draft_sha256: 'a'.repeat(64),
}
afterEach(() => vi.unstubAllGlobals())

it('publishes the newly saved digest instead of the initial page digest', async () => {
  const calls: Record<string, unknown>[] = []
  draft.hash = 'saved-hash'
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      calls.push(body)
      return Response.json(
        body.action === 'status' ? { ...status, draft_sha256: 'b'.repeat(64) } : { version_no: 5 },
      )
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip
      status={status}
      sync={async () => true}
      save={() => publishManagedWorkflow('app-c', 2, 'saved-hash')}
    />,
  )
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect.element(screen.getByRole('status').first()).toHaveTextContent('已发布到客户端客服')
  expect(calls.find((c) => c.action === 'publish')).toMatchObject({
    app_id: 'app-c',
    revision: 2,
    draft: 'b'.repeat(64),
  })
})

it('does not publish after another tab changes the saved graph', async () => {
  const calls: Record<string, unknown>[] = []
  draft.hash = 'another-tab'
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      calls.push(JSON.parse(options.body))
      return Response.json(status)
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip
      status={status}
      sync={async () => true}
      save={() => publishManagedWorkflow('app-c', 2, 'saved-hash')}
    />,
  )
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect.element(screen.getByRole('status').first()).toHaveTextContent('草稿已被其他操作修改')
  expect(calls.some((c) => c.action === 'publish')).toBe(false)
})

it('renders nothing for an unmanaged app', async () => {
  const screen = await render(
    <WorkflowManagedStrip
      status={{ ...status, managed: false }}
      sync={async () => true}
      save={async () => {}}
    />,
  )
  await expect.element(screen.getByRole('region', { name: '受管发布' })).not.toBeInTheDocument()
})

it('saves before preview and stops the same trial', async () => {
  const events: string[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      events.push(body.action)
      return Response.json({
        trial_id: 'tr_' + 'a'.repeat(32),
        status: body.action === 'trial-cancel' ? 'cancelled' : 'running',
      })
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip
      status={status}
      save={async () => {}}
      sync={async () => {
        events.push('save')
        return true
      }}
    />,
  )
  await screen.getByText('预览客户端客服', { exact: true }).click()
  await screen.getByRole('textbox', { name: '预览问题', exact: true }).fill('怎么充电？')
  await screen.getByRole('button', { name: '开始预览', exact: true }).click()
  await expect.element(screen.getByRole('button', { name: '停止预览', exact: true })).toBeVisible()
  expect(events.slice(0, 2)).toEqual(['save', 'trial'])
  await screen.getByRole('button', { name: '停止预览', exact: true }).click()
  await expect
    .element(screen.getByRole('status', { name: '预览结果' }))
    .toHaveTextContent('预览已停止')
  expect(events).toContain('trial-cancel')
})

it('manages customer knowledge access without technical identifiers', async () => {
  const changes: unknown[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      if (body.action === 'knowledge')
        return Response.json({
          libraries: [{ id: 'own-kb', name: '客户操作手册', allowed: false }],
        })
      changes.push(body)
      return Response.json({ id: 'own-kb', allowed: body.allowed })
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip status={status} save={async () => {}} sync={async () => true} />,
  )
  await screen.getByText('知识库使用范围', { exact: true }).click()
  await screen.getByRole('checkbox', { name: '客户操作手册' }).click()
  await expect
    .element(screen.getByRole('status', { name: '知识授权结果' }))
    .toHaveTextContent('已允许客户端使用')
  expect(changes).toEqual([
    { action: 'knowledge-access', app_id: 'app-c', knowledge_base_id: 'own-kb', allowed: true },
  ])
})
