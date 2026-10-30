import { render } from 'vitest-browser-react'
import { WorkflowManagedStrip } from '@/app/components/aiops-workflow-managed'

const status = {
  managed: true,
  app_id: 'app-c',
  platform: false,
  model: 'deepseek-v4-flash',
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

vi.mock('next/navigation', () => ({ useParams: () => ({ appId: 'app-c' }) }))

afterEach(() => {
  vi.unstubAllGlobals()
})

it('renders the managed strip on the real Chatflow editor page and publishes with the draft digest', async () => {
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      calls.push(body)
      if (body.action === 'status') return Response.json(status)
      if (body.action === 'readiness') return Response.json({ can_diagnose: true, reason: '' })
      return Response.json({ version_no: 5, action: 'synced', app_id: 'app-c' })
    }),
  )
  const screen = await render(<WorkflowManagedStrip />)
  await expect.element(screen.getByRole('region', { name: '受管发布' })).toBeVisible()
  await expect.element(screen.getByText('客户端客服 · 正式 V4')).toBeVisible()
  await expect.element(screen.getByText('AI 已准备，渠道待接入').first()).toBeVisible()
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect.element(screen.getByRole('status')).toHaveTextContent('已发布到客户端客服')
  const publish = calls.find((c) => c.action === 'publish')
  // 发布必须带上页面看到的那份草稿摘要
  expect(publish).toMatchObject({ app_id: 'app-c', revision: 2, draft: 'a'.repeat(64) })
})

it('renders nothing for an app this platform does not manage', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ managed: false })),
  )
  const screen = await render(<WorkflowManagedStrip />)
  await expect.element(screen.getByRole('region', { name: '受管发布' })).not.toBeInTheDocument()
})
