import { render } from 'vitest-browser-react'
import TenantSetup, { ManagedPublish } from '@/app/components/aiops-management'
import { publishManaged } from '@/service/aiops-management'

const status = {
  managed: true,
  app_id: 'app',
  platform: false,
  model: 'actual-java-model',
  profile: 'default',
  model_options: { max_tokens: 4096 },
  version_no: 2,
  route_revision: 3,
  published: true,
  published_prompt: 'SOP',
  published_tools: [],
}

const catalogue = {
  platform: true,
  tenants: [{ id: 'tenant', name: '客户甲' }],
  sources: [{ app_id: 'source', name: '客户甲 / 标准管家', version_no: 2 }],
  entries: [
    {
      entry: 'consumer',
      label: '客户端客服',
      scenario: 'consumer_service',
      sources: [{ app_id: 'source-c', name: '客户甲 / 现役客服', version_no: 5 }],
    },
    {
      entry: 'operator',
      label: '管家',
      scenario: 'operator_order_diagnosis',
      sources: [{ app_id: 'source', name: '客户甲 / 标准管家', version_no: 2 }],
    },
  ],
  operations: [],
}

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

function stub(routes: (body: Record<string, unknown>) => Response | Promise<Response>) {
  const requests: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      requests.push(body)
      return routes(body)
    }),
  )
  return requests
}

it('retries the same operation after a lost response without asking for technical IDs', async () => {
  let creates = 0
  sessionStorage.setItem('aiops-setup-request', 'corrupt-json')
  const requests = stub((body) => {
    if (body.action === 'catalogue') return Response.json(catalogue)
    if (body.action === 'create' && ++creates === 1) throw new Error('网络中断')
    return Response.json({ id: 'operation' })
  })
  const screen = await render(<TenantSetup />)
  await screen.getByRole('button', { name: '复制并开通客户 AI 服务' }).click()
  await screen.getByRole('combobox', { name: '客户端客服来源' }).selectOptions('source-c')
  await screen.getByRole('combobox', { name: '管家来源' }).selectOptions('source')
  await screen.getByRole('combobox', { name: '客户', exact: true }).selectOptions('tenant')
  await screen.getByRole('button', { name: '复制并开通', exact: true }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('网络中断')
  await screen.getByRole('button', { name: '复制并开通', exact: true }).click()
  const submitted = requests.filter((x) => x.action === 'create')
  expect(submitted).toHaveLength(2)
  expect(submitted[0]).toEqual(submitted[1])
  expect(submitted[0]).toMatchObject({
    scope: 'both',
    tenant_id: 'tenant',
    name: '客户甲－客服与管家',
    sources: {
      consumer: { app_id: 'source-c', version_no: 5 },
      operator: { app_id: 'source', version_no: 2 },
    },
  })
})

it('opens both ends by default and can open only one of them', async () => {
  const requests = stub((body) =>
    body.action === 'catalogue' ? Response.json(catalogue) : Response.json({ id: 'operation' }),
  )
  const screen = await render(<TenantSetup />)
  await screen.getByRole('button', { name: '复制并开通客户 AI 服务' }).click()
  const consumer = screen.getByRole('checkbox', { name: '客户端客服' })
  const operator = screen.getByRole('checkbox', { name: '管家' })
  await expect.element(consumer).toBeChecked()
  await expect.element(operator).toBeChecked()
  await operator.click()
  await expect.element(operator).not.toBeChecked()
  await screen.getByRole('combobox', { name: '客户端客服来源' }).selectOptions('source-c')
  await screen.getByRole('combobox', { name: '客户', exact: true }).selectOptions('tenant')
  await screen.getByRole('button', { name: '复制并开通', exact: true }).click()
  const created = requests.find((x) => x.action === 'create')
  expect(created).toMatchObject({
    scope: 'consumer',
    name: '客户甲－客服',
    sources: { consumer: { app_id: 'source-c', version_no: 5 } },
  })
})

it('does not preselect an end without a published source', async () => {
  const requests = stub((body) =>
    body.action === 'catalogue'
      ? Response.json({
          ...catalogue,
          entries: [
            { ...catalogue.entries[0], sources: [] },
            catalogue.entries[1],
          ],
        })
      : Response.json({ id: 'operation' }),
  )
  const screen = await render(<TenantSetup />)
  await screen.getByRole('button', { name: '复制并开通客户 AI 服务' }).click()
  await expect.element(screen.getByRole('checkbox', { name: '客户端客服' })).not.toBeChecked()
  await expect.element(screen.getByRole('checkbox', { name: '客户端客服' })).toBeDisabled()
  expect(requests.filter((x) => x.action === 'create')).toHaveLength(0)
})

it('reports each end separately and never claims a pending channel is usable', async () => {
  stub((body) =>
    body.action === 'catalogue'
      ? Response.json({
          ...catalogue,
          operations: [
            {
              id: 'op-1',
              name: '客户甲－客服与管家',
              tenant_name: '客户甲',
              state: 'partial',
              step: 'complete',
              scope: 'both',
              published: false,
              error_code: 'SETUP_PARTIAL',
              entries: {
                consumer: {
                  app_id: 'app-c',
                  version_no: 4,
                  published: true,
                  draft_only: false,
                  channel: 'pending',
                  channel_note: 'AI 已准备，渠道待接入',
                  state: 'ready',
                },
                operator: {
                  published: false,
                  draft_only: false,
                  state: 'failed',
                  error_code: 'SETUP_MODEL_CONFLICT',
                },
              },
            },
          ],
        })
      : Response.json({ id: 'operation' }),
  )
  const screen = await render(<TenantSetup />)
  await screen.getByRole('button', { name: '复制并开通客户 AI 服务' }).click()
  await expect.element(screen.getByText('客户端客服：已发布 · AI 已准备，渠道待接入')).toBeVisible()
  await expect.element(screen.getByText('管家：模型配置与平台模板不一致')).toBeVisible()
  await expect.element(screen.getByRole('status')).toHaveTextContent('部分完成')
})

it('still works against a backend that only knows the operator entry', async () => {
  const requests = stub((body) =>
    body.action === 'catalogue'
      ? Response.json({ ...catalogue, entries: undefined })
      : Response.json({ id: 'operation' }),
  )
  const screen = await render(<TenantSetup />)
  await screen.getByRole('button', { name: '复制并开通客户 AI 服务' }).click()
  await expect.element(screen.getByRole('checkbox', { name: '管家' })).toBeChecked()
  // 旧后端只认识管家：页面不凭空造一个客户端选项，避免提交出后端不认识的形状
  expect(screen.getByRole('checkbox', { name: '客户端客服' }).elements()).toHaveLength(0)
  await screen.getByRole('combobox', { name: '管家来源' }).selectOptions('source')
  await screen.getByRole('combobox', { name: '客户', exact: true }).selectOptions('tenant')
  await screen.getByRole('button', { name: '复制并开通', exact: true }).click()
  expect(requests.find((x) => x.action === 'create')).toMatchObject({
    scope: 'operator',
    sources: { operator: { app_id: 'source', version_no: 2 } },
  })
})

it('shows save failure and preserves the displayed formal version', async () => {
  const screen = await render(
    <ManagedPublish
      status={status}
      save={async () => {
        throw new Error('保存失败')
      }}
    />,
  )
  await expect.element(screen.getByText('actual-java-model', { exact: false })).toBeVisible()
  await screen.getByRole('button', { name: '发布到管家' }).click()
  await expect.element(screen.getByRole('status')).toHaveTextContent('保存失败')
  await expect.element(screen.getByText('正式 V2', { exact: false })).toBeVisible()
})

it('labels the consumer end and shows the pending channel beside it', async () => {
  const screen = await render(
    <ManagedPublish
      status={{
        ...status,
        entry: 'consumer',
        knowledge_base_ids: ['kb-1', 'kb-2'],
        channel: 'pending',
        model: 'deepseek-v4-flash',
      }}
      save={async () => true}
    />,
  )
  await expect.element(screen.getByText('客户端客服', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('对客知识库 2 个')).toBeVisible()
  await expect.element(screen.getByText('AI 已准备，渠道待接入')).toBeVisible()
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect.element(screen.getByRole('status')).toHaveTextContent('已发布到客户端客服')
})

it('publishes with the loaded route revision and reports concurrent publication conflict', async () => {
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      calls.push(JSON.parse(options.body))
      return Response.json({ error: { code: 'SETUP_ROUTE_CHANGED' } }, { status: 409 })
    }),
  )
  await expect(
    publishManaged('app', { pre_prompt: 'SOP', agent_mode: { tools: [] } }, 3),
  ).rejects.toThrow('正式版本已变化')
  expect(calls).toHaveLength(1)
  expect(calls[0]).toMatchObject({ action: 'publish', app_id: 'app', revision: 3 })
  expect(calls[0]?.draft).toMatch(/^[a-f0-9]{64}$/)
})

it('shows the model default when Java has no explicit output cap', async () => {
  const screen = await render(
    <ManagedPublish status={{ ...status, model_options: {} }} save={async () => true} />,
  )
  await expect.element(screen.getByText('输出上限：模型默认', { exact: false })).toBeVisible()
})

it('requires authorization before entering a customer workspace', async () => {
  const requests = stub((body) => {
    if (body.action === 'catalogue')
      return Response.json({
        platform: true,
        tenants: [],
        sources: [],
        operations: [],
        workspaces: [{ workspace_id: 'ws-customer', tenant_id: 'customer', name: '客户甲' }],
      })
    return Response.json({ error: { code: 'SETUP_TENANT_FORBIDDEN' } }, { status: 403 })
  })
  const screen = await render(<TenantSetup />)
  await screen.getByRole('combobox', { name: '已开通客户' }).selectOptions('ws-customer')
  await screen.getByRole('button', { name: '进入工作区', exact: true }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('当前账号无权管理这个客户')
  expect(requests.at(-1)).toEqual({ action: 'workspace', workspace_id: 'ws-customer' })
})
