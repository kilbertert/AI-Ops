import type { ComponentProps } from 'react'
import { useState } from 'react'
import { render } from 'vitest-browser-react'
import { WorkflowManagedStrip } from '@/app/components/aiops-workflow-managed'
import { VarKindType } from '@/app/components/workflow/nodes/_base/types'
import { BlockEnum } from '@/app/components/workflow/types'
import { publishManagedWorkflow } from '@/service/aiops-management'

const draft = vi.hoisted(() => ({ hash: 'saved-hash', graph: { nodes: [] as unknown[] } }))
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
const fixtureNodes = () => [
  {
    id: 'answer',
    position: { x: 0, y: 0 },
    data: {
      type: BlockEnum.Agent,
      title: '客服',
      desc: '',
      agent_parameters: {
        instruction: { type: VarKindType.constant, value: '保留本次提示词' },
        tools: {
          type: VarKindType.constant,
          value: [
            {
              tool_name: 'consumer_knowledge',
              settings: {
                knowledge_base_ids: { type: 'constant', value: ['kb-1'] },
              },
              parameters: { data_grant: { value: 'original' } },
            },
          ],
        },
      },
    },
  },
]
const editing = { nodes: fixtureNodes(), updateNode: vi.fn() }
beforeEach(() => {
  editing.nodes = fixtureNodes()
  editing.updateNode = vi.fn()
})
afterEach(() => vi.unstubAllGlobals())

function Panel(props: Partial<ComponentProps<typeof WorkflowManagedStrip>>) {
  const [nodes, setNodes] = useState(fixtureNodes)
  return (
    <WorkflowManagedStrip
      {...editing}
      status={status}
      save={async () => {}}
      sync={async () => true}
      {...props}
      nodes={nodes}
      updateNode={({ id, data }) => {
        setNodes((current) =>
          current.map((node) =>
            node.id === id ? { ...node, data: { ...node.data, ...data } } : node,
          ),
        )
        editing.updateNode({ id, data })
        draft.graph.nodes = [{ id, data }]
      }}
    />
  )
}

// 受管操作都在「受管发布」抽屉里；每个断言前先开抽屉。
const openStrip = async (screen: Awaited<ReturnType<typeof render>>) => {
  await screen.getByRole('button', { name: '受管发布', exact: true }).click()
}

it('publishes the newly saved digest instead of the initial page digest', async () => {
  const calls: Record<string, unknown>[] = []
  draft.hash = 'saved-hash'
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      if (body.action === 'knowledge')
        return Response.json({ libraries: [{ id: 'kb-1', name: '原资料', allowed: true }] })
      calls.push(body)
      return Response.json(
        body.action === 'status' ? { ...status, draft_sha256: 'b'.repeat(64) } : { version_no: 5 },
      )
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip
      {...editing}
      status={status}
      sync={async () => true}
      save={() => publishManagedWorkflow('app-c', 2, 'saved-hash')}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect
    .element(screen.getByText(/已发布到客户端客服|草稿已被其他操作修改/))
    .toHaveTextContent('已发布到客户端客服')
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
      const body = JSON.parse(options.body)
      if (body.action === 'knowledge')
        return Response.json({ libraries: [{ id: 'kb-1', name: '原资料', allowed: true }] })
      calls.push(body)
      return Response.json(status)
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip
      {...editing}
      status={status}
      sync={async () => true}
      save={() => publishManagedWorkflow('app-c', 2, 'saved-hash')}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect
    .element(screen.getByText(/已发布到客户端客服|草稿已被其他操作修改/))
    .toHaveTextContent('草稿已被其他操作修改')
  expect(calls.some((c) => c.action === 'publish')).toBe(false)
})

it('renders nothing for an unmanaged app', async () => {
  const screen = await render(
    <WorkflowManagedStrip
      {...editing}
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
      if (body.action === 'knowledge')
        return Response.json({ libraries: [{ id: 'kb-1', name: '原资料', allowed: true }] })
      events.push(body.action)
      return Response.json({
        trial_id: `tr_${'a'.repeat(32)}`,
        status: body.action === 'trial-cancel' ? 'cancelled' : 'running',
      })
    }),
  )
  const screen = await render(
    <WorkflowManagedStrip
      {...editing}
      status={status}
      save={async () => {}}
      sync={async () => {
        events.push('save')
        return true
      }}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('textbox', { name: '预览问题', exact: true }).fill('怎么充电？')
  await screen.getByRole('button', { name: '开始预览', exact: true }).click()
  await expect.element(screen.getByRole('button', { name: '停止预览', exact: true })).toBeVisible()
  // save 必须先于 trial；抽屉打开触发的 knowledge 加载与本次断言无关，过滤掉。
  expect(events.filter((e) => e === 'save' || e === 'trial')).toEqual(['save', 'trial'])
  await screen.getByRole('button', { name: '停止预览', exact: true }).click()
  await expect
    .element(screen.getByRole('status', { name: '预览结果' }))
    .toHaveTextContent('预览已停止')
  expect(events).toContain('trial-cancel')
})

it('keeps nested version popover usable inside the drawer', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({ ...status, libraries: [{ id: 'kb-1', name: '原资料', allowed: true }] }),
    ),
  )
  const screen = await render(
    <WorkflowManagedStrip
      {...editing}
      status={status}
      save={async () => {}}
      sync={async () => true}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('button', { name: '版本操作', exact: true }).click()
  // 嵌套 Popover 打开时外层抽屉不能被误关：内部按钮与外层标题都要还在。
  await expect.element(screen.getByText('撤回当前正式版本')).toBeVisible()
  await expect.element(screen.getByText('受管发布').first()).toBeVisible()
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
    <WorkflowManagedStrip
      {...editing}
      status={status}
      save={async () => {}}
      sync={async () => true}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('checkbox', { name: '允许对客：客户操作手册' }).click()
  await expect
    .element(screen.getByRole('status', { name: '知识授权结果' }))
    .toHaveTextContent('已允许客户端使用')
  expect(changes).toEqual([
    { action: 'knowledge-access', app_id: 'app-c', knowledge_base_id: 'own-kb', allowed: true },
  ])
})

it('edits actual application selection separately from customer permission and preserves prompt', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({ libraries: [{ id: 'kb-new', name: '新资料', allowed: true }] }),
    ),
  )
  const saved: string[] = []
  const screen = await render(
    <Panel
      sync={async () => {
        saved.push('saved')
        return true
      }}
    />,
  )
  await openStrip(screen)
  await expect.element(screen.getByText('已失效的知识库引用 1')).toBeVisible()
  await expect.element(screen.getByRole('button', { name: '发布到客户端客服' })).toBeDisabled()
  await screen.getByRole('checkbox', { name: '本应用选用：新资料' }).click()
  await screen.getByRole('button', { name: '移除失效引用 1' }).click()
  await expect.element(screen.getByRole('button', { name: '发布到客户端客服' })).toBeEnabled()
  const data = editing.updateNode.mock.lastCall?.[0].data
  expect(data.agent_parameters.tools.value[0].settings.knowledge_base_ids.value).toEqual(['kb-new'])
  expect(data.agent_parameters.instruction.value).toBe('保留本次提示词')
  expect(data.agent_parameters.tools.value[0].parameters.data_grant.value).toBe('original')
  expect(saved).toHaveLength(2)
})

it('blocks publication after selection save failure and allows explicit retry', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({
        libraries: [
          { id: 'kb-1', name: '原资料', allowed: true },
          { id: 'kb-new', name: '新资料', allowed: true },
        ],
      }),
    ),
  )
  let fails = true
  const screen = await render(
    <Panel
      sync={async () => {
        if (fails) throw new Error('保存冲突')
        return true
      }}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('checkbox', { name: '本应用选用：新资料' }).click()
  await expect.element(screen.getByText('保存冲突')).toBeVisible()
  await expect.element(screen.getByRole('button', { name: '发布到客户端客服' })).toBeDisabled()
  fails = false
  await screen.getByRole('button', { name: '重试保存选库' }).click()
  await expect.element(screen.getByRole('button', { name: '发布到客户端客服' })).toBeEnabled()
})

it('does not claim selection saved when the server retains the old graph', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json({
        libraries: [
          { id: 'kb-1', name: '原资料', allowed: true },
          { id: 'kb-new', name: '新资料', allowed: true },
        ],
      }),
    ),
  )
  const screen = await render(
    <Panel
      sync={async () => {
        draft.graph.nodes = fixtureNodes()
        return true
      }}
    />,
  )
  await openStrip(screen)
  await screen.getByRole('checkbox', { name: '本应用选用：新资料' }).click()
  await expect.element(screen.getByText('选库未保存或已被其他操作修改，请刷新确认')).toBeVisible()
  await expect.element(screen.getByRole('button', { name: '发布到客户端客服' })).toBeDisabled()
})
