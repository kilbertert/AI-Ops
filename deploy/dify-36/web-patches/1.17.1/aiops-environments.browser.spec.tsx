import { afterEach, expect, it, vi } from 'vite-plus/test'
import { render } from 'vitest-browser-react'
import EnvironmentManagement from './aiops-environments'

const push = vi.hoisted(() => vi.fn())
vi.mock('@/next/navigation', () => ({ useRouter: () => ({ push }) }))

const draft = {
  id: 'a',
  name: '环境甲',
  enabled: true,
  gateway_url: 'https://a.example.test',
  cpms_base_url: 'https://a.example.test/cpms',
  company_gateway_url: 'https://company.example.test',
  deployment_target: 'host-a',
  credential_ref: 'credential-a',
  timezone: 'Asia/Singapore',
  tenants: [{ id: 'same', name: '甲租户', enabled: true }],
}
const { deployment_target, credential_ref, timezone, ...effective } = draft
const data = {
  registry_revision: 4,
  can_create: true,
  effective: [
    effective,
    {
      ...effective,
      id: 'b',
      name: '环境乙',
      tenants: [{ id: 'same', name: '乙租户', enabled: false }],
    },
  ],
  drafts: [{ environment: 'a', revision: 2, draft, registry_changed: false }],
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

it('saves only a draft with both revisions and preserves fields across reload', async () => {
  const writes: Record<string, unknown>[] = []
  const catalogue = structuredClone(data)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      if (body.action === 'environment-task') return Response.json({ tasks: [] })
      if (body.action === 'environment-drafts') return Response.json(catalogue)
      writes.push(body)
      catalogue.drafts[0] = {
        environment: 'a',
        revision: 3,
        draft: body.draft,
        registry_changed: false,
      }
      return Response.json({ revision: 3, state: 'draft', acceptance: 'unverified' })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await expect
    .element(screen.getByRole('textbox', { name: '环境名称', exact: true }))
    .toHaveValue('环境甲')
  await screen.getByRole('textbox', { name: '租户 1 名称' }).fill('甲新名称')
  await screen.getByRole('checkbox', { name: '租户 1 准入' }).click()
  await screen.getByRole('button', { name: '保存草稿' }).click()
  expect(writes).toEqual([
    {
      action: 'environment-draft-save',
      revision: 2,
      registry_revision: 4,
      draft: { ...draft, tenants: [{ id: 'same', name: '甲新名称', enabled: false }] },
    },
  ])
  await expect.element(screen.getByRole('status')).toHaveTextContent('草稿已保存')
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByRole('textbox', { name: '租户 1 名称' })).toHaveValue('甲新名称')
  await expect.element(screen.getByText('当前生效目录：R4 · 允许准入 · 1 个租户')).toBeVisible()
  await expect.element(screen.getByText('配置草稿：V3 · 未预检')).toBeVisible()
  expect(screen.getByRole('button', { name: '开通', exact: true }).elements()).toHaveLength(0)
})

it('creates an environment and tenant without enabling either automatically', async () => {
  const writes: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      if (body.action === 'environment-task') return Response.json({ tasks: [] })
      if (body.action === 'environment-drafts')
        return Response.json({ registry_revision: 1, can_create: true, effective: [], drafts: [] })
      writes.push(body)
      return Response.json({ revision: 1 })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  for (const [name, value] of [
    ['环境编号', 'new'],
    ['环境名称', '新环境'],
    ['AI-Ops 入口地址', 'https://new.example.test'],
    ['CPMS API 地址', 'https://new.example.test/cpms'],
    ['公司网关地址', 'https://company.example.test'],
    ['部署目标引用', deployment_target],
    ['凭据引用', credential_ref],
    ['时区', timezone],
  ] as const)
    await screen.getByRole('textbox', { name, exact: true }).fill(value)
  await screen.getByRole('button', { name: '新增租户' }).click()
  await screen.getByRole('textbox', { name: '租户 1 编号' }).fill('same')
  await screen.getByRole('textbox', { name: '租户 1 名称' }).fill('新租户')
  await screen.getByRole('button', { name: '保存草稿' }).click()
  await expect.element(screen.getByRole('status')).toHaveTextContent('草稿已保存')
  expect(writes[0]).toMatchObject({
    action: 'environment-draft-save',
    revision: 0,
    registry_revision: 1,
    draft: { id: 'new', enabled: false, tenants: [{ id: 'same', name: '新租户', enabled: false }] },
  })
  await expect
    .element(screen.getByRole('textbox', { name: '环境编号', exact: true }))
    .toBeDisabled()
})

it('preserves edits after a conflict, warns before switching, and clears data on revoked refresh', async () => {
  let revoked = false
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      if (body.action === 'environment-task') return Response.json({ tasks: [] })
      if (body.action === 'environment-drafts')
        return revoked
          ? Response.json({ error: { code: 'ENVIRONMENT_MANAGEMENT_FORBIDDEN' } }, { status: 403 })
          : Response.json({ ...data, can_create: false })
      return Response.json(
        { error: { code: 'ENVIRONMENT_DRAFT_REVISION_CONFLICT' } },
        { status: 409 },
      )
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('保留输入')
  await screen.getByRole('button', { name: '保存草稿' }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('草稿已被其他管理员更新')
  await screen.getByRole('button', { name: '环境乙（b）' }).click()
  await expect.element(screen.getByRole('alertdialog')).toBeVisible()
  await screen.getByRole('button', { name: '继续编辑' }).click()
  await expect
    .element(screen.getByRole('textbox', { name: '环境名称', exact: true }))
    .toHaveValue('保留输入')
  await screen.getByRole('button', { name: '环境乙（b）' }).click()
  await screen.getByRole('button', { name: '放弃并继续' }).click()
  await expect.element(screen.getByRole('textbox', { name: '租户 1 名称' })).toHaveValue('乙租户')
  expect(screen.getByRole('button', { name: '新增环境' }).elements()).toHaveLength(0)
  revoked = true
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('没有环境管理授权')
  expect(screen.getByRole('textbox').elements()).toHaveLength(0)
  expect(screen.getByRole('navigation', { name: '业务环境列表' }).elements()).toHaveLength(0)
})

it('confirms main-navigation departure before losing unsaved edits', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) =>
      Response.json(JSON.parse(options.body).action === 'environment-task' ? { tasks: [] } : data),
    ),
  )
  const screen = await render(
    <>
      <a href="/apps">工作室</a>
      <EnvironmentManagement />
    </>,
  )
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('未保存名称')
  await screen.getByRole('link', { name: '工作室' }).click()
  await expect.element(screen.getByRole('alertdialog')).toBeVisible()
  expect(push).not.toHaveBeenCalled()
  await screen.getByRole('button', { name: '继续编辑' }).click()
  await expect
    .element(screen.getByRole('textbox', { name: '环境名称', exact: true }))
    .toHaveValue('未保存名称')
  await screen.getByRole('link', { name: '工作室' }).click()
  await screen.getByRole('button', { name: '放弃并继续' }).click()
  expect(push).toHaveBeenCalledWith('/apps')
})

it('freezes saved inputs and shows blockers without offering activation', async () => {
  const calls: { action: string; revision?: number; registry_revision?: number; id?: string }[] = []
  const frozen = {
    id: `plan_${'a'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [
      { id: 'references', state: 'blocked' },
      { id: 'network', state: 'pending' },
    ],
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-task') return Response.json({ tasks: [] })
      calls.push(body)
      return new Response(JSON.stringify(body.action === 'environment-plan' ? frozen : data), {
        status: 200,
      })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检' }).click()
  await expect.element(screen.getByRole('heading', { name: '开通计划' })).toBeVisible()
  expect(calls.at(-1)).toEqual({
    action: 'environment-plan',
    environment: 'a',
    revision: 2,
    registry_revision: 4,
  })
  await expect.element(screen.getByText('部署与配置引用：阻塞')).toBeVisible()
  await expect.element(screen.getByText('正式双向网络：未验证')).toBeVisible()
  await screen.getByRole('button', { name: '重新核对计划' }).click()
  expect(calls.at(-1)).toEqual({ action: 'environment-plan', id: frozen.id })
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('尚未保存')
  await expect.element(screen.getByRole('button', { name: '配置与制品预检' })).toBeDisabled()
})

it('persists site tasks across refresh and safely requests cancellation', async () => {
  const task = {
    id: `task_${'b'.repeat(32)}`,
    plan_id: `plan_${'a'.repeat(32)}`,
    state: 'reconcile',
    cancel_requested: false,
    error_code: '',
    result: {},
  }
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-drafts') return Response.json(data)
      calls.push(body)
      if (body.cancel) {
        task.state = 'cancelled'
        task.cancel_requested = true
        return Response.json(task)
      }
      return Response.json({ tasks: [task] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await expect.element(screen.getByText('结果待核对', { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByText(task.id, { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '环境甲（a）' }).click()
  await expect.element(screen.getByText(task.id, { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '刷新任务' }).click()
  await expect.element(screen.getByText(task.id, { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '取消现场预检' }).click()
  expect(calls.at(-1)).toEqual({ action: 'environment-task', id: task.id, cancel: true })
  await expect.element(screen.getByText('已取消', { exact: true })).toBeVisible()
})
