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

afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})

it('retries the same operation after a lost response without asking for technical IDs', async () => {
  const requests: Record<string, unknown>[] = []
  let creates = 0
  sessionStorage.setItem('aiops-setup-request', 'corrupt-json')
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      requests.push(body)
      if (body.action === 'catalogue')
        return Response.json({
          platform: true,
          tenants: [{ id: 'tenant', name: '客户甲' }],
          sources: [{ app_id: 'source', name: '标准管家', version_no: 2 }],
          operations: [],
        })
      if (body.action === 'create' && ++creates === 1) throw new Error('网络中断')
      return Response.json({ id: 'operation' })
    }),
  )
  const screen = await render(<TenantSetup />)
  await screen.getByRole('button', { name: '复制并开通客户管家' }).click()
  await screen.getByRole('combobox', { name: '来源版本' }).selectOptions('source')
  await screen.getByRole('combobox', { name: '客户', exact: true }).selectOptions('tenant')
  await screen.getByRole('button', { name: '复制并开通', exact: true }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('网络中断')
  await screen.getByRole('button', { name: '复制并开通', exact: true }).click()
  const submitted = requests.filter((x) => x.action === 'create')
  expect(submitted).toHaveLength(2)
  expect(submitted[0]).toEqual(submitted[1])
  expect(submitted[0]).toMatchObject({
    source_version: 2,
    tenant_id: 'tenant',
    name: '客户甲－订单管家',
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
  const requests: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      requests.push(body)
      if (body.action === 'catalogue')
        return Response.json({
          platform: true,
          tenants: [],
          sources: [],
          operations: [],
          workspaces: [{ workspace_id: 'ws-customer', tenant_id: 'customer', name: '客户甲' }],
        })
      return Response.json({ error: { code: 'SETUP_TENANT_FORBIDDEN' } }, { status: 403 })
    }),
  )
  const screen = await render(<TenantSetup />)
  await screen.getByRole('combobox', { name: '已开通客户' }).selectOptions('ws-customer')
  await screen.getByRole('button', { name: '进入工作区', exact: true }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('当前账号无权管理这个客户')
  expect(requests.at(-1)).toEqual({ action: 'workspace', workspace_id: 'ws-customer' })
})
