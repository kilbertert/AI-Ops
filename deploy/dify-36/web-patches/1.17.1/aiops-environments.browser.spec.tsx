import { afterEach, expect, it, vi } from 'vite-plus/test'
import { render } from 'vitest-browser-react'
import { page, userEvent } from 'vitest/browser'
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

it('prepares the selected saved tenant and keeps business pending after reload', async () => {
  const site = {
    id: `task_${'a'.repeat(32)}`,
    action: 'site-preflight',
    state: 'observed',
    result: {},
    cancel_requested: false,
    error_code: '',
  }
  const workspace = {
    ...site,
    id: `task_${'b'.repeat(32)}`,
    action: 'workspace-prepare',
    tenant_id: 'same',
    result: {
      workspace_id: 'ws-a',
      checks: [
        { id: 'workspace', state: 'passed' },
        { id: 'business', state: 'pending' },
      ],
    },
  }
  const calls: Record<string, unknown>[] = []
  let prepared = false
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-drafts') return Response.json(data)
      calls.push(body)
      if (body.site_task) {
        prepared = true
        return Response.json(workspace)
      }
      return Response.json({ tasks: prepared ? [workspace, site] : [site] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('button', { name: '准备候选工作区：甲租户（same）' }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    site_task: site.id,
    tenant_id: 'same',
  })
  await expect.element(screen.getByText('候选工作区已准备', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('业务能力验证：未验证', { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByText('工作区编号：ws-a', { exact: true })).toBeVisible()
  await screen.getByRole('tab', { name: /连接配置/ }).click()
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('未保存')
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  expect(
    screen.getByRole('button', { name: '准备候选工作区：甲租户（same）' }).elements(),
  ).toHaveLength(0)
})

it('prepares knowledge and its scoped connection from verified task cards', async () => {
  const workspace = {
    id: `task_${'a'.repeat(32)}`,
    action: 'workspace-prepare',
    tenant_id: 'same',
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: { workspace_id: 'ws-a' },
  }
  const knowledge = {
    ...workspace,
    id: `task_${'b'.repeat(32)}`,
    action: 'knowledge-prepare',
    result: {
      workspace_id: 'ws-a',
      knowledge_base_id: 'kb-a',
      checks: [
        { id: 'knowledge', state: 'passed' },
        { id: 'business', state: 'pending' },
      ],
    },
  }
  const calls: Record<string, unknown>[] = []
  let prepared = false
  let connected = false
  let appPrepared = false
  let consumerPrepared = false
  const connection = {
    ...knowledge,
    id: `task_${'c'.repeat(32)}`,
    action: 'knowledge-connect',
    result: { ...knowledge.result, connection_id: 'connection-a' },
  }
  const application = {
    ...connection,
    id: `task_${'d'.repeat(32)}`,
    action: 'operator-prepare',
    result: { ...connection.result, app_id: 'app-a' },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-drafts') return Response.json(data)
      calls.push(body)
      if (body.consumer_sources)
        return Response.json({ sources: [{ app_id: 'source-a', name: '正式客服', version_no: 7 }] })
      if (body.workspace_task) {
        prepared = true
        return Response.json(knowledge)
      }
      if (body.knowledge_task) {
        connected = true
        return Response.json(connection)
      }
      if (body.connection_task && body.source_app_id) {
        consumerPrepared = true
        return Response.json({
          ...application,
          id: `task_${'e'.repeat(32)}`,
          action: 'consumer-prepare',
          result: { ...application.result, app_id: 'consumer-a' },
        })
      }
      if (body.connection_task) {
        appPrepared = true
        return Response.json(application)
      }
      return Response.json({
        tasks: consumerPrepared
          ? [
              {
                ...application,
                id: `task_${'e'.repeat(32)}`,
                action: 'consumer-prepare',
                result: { ...application.result, app_id: 'consumer-a' },
              },
              application,
              connection,
              knowledge,
              workspace,
            ]
          : appPrepared
            ? [application, connection, knowledge, workspace]
            : connected
              ? [connection, knowledge, workspace]
              : prepared
                ? [knowledge, workspace]
                : [workspace],
      })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('button', { name: '准备候选知识资源：same' }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    workspace_task: workspace.id,
    tenant_id: 'same',
  })
  await expect.element(screen.getByText('候选知识资源已准备', { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByText('知识库编号：kb-a', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('业务能力验证：未验证', { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '准备候选知识连接：same' }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    knowledge_task: knowledge.id,
    tenant_id: 'same',
  })
  await expect.element(screen.getByText('候选知识连接已准备', { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '读取正式消费者模板' }).click()
  await screen.getByRole('combobox', { name: '消费者来源模板：same' }).selectOptions('source-a')
  await screen.getByRole('button', { name: '准备候选消费者应用：same' }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    connection_task: connection.id,
    tenant_id: 'same',
    source_app_id: 'source-a',
    source_version: 7,
  })
  await expect
    .element(screen.getByText('候选消费者应用已准备（入口停用）', { exact: true }))
    .toBeVisible()
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByText('应用编号：consumer-a', { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '准备候选管家应用：same' }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    connection_task: connection.id,
    tenant_id: 'same',
  })
  await expect
    .element(screen.getByText('候选管家应用已准备（入口停用）', { exact: true }))
    .toBeVisible()
  await screen.getByRole('button', { name: '刷新目录' }).click()
  await expect.element(screen.getByText('应用编号：app-a', { exact: true })).toBeVisible()
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
  await screen.getByRole('tab', { name: /连接配置/ }).click()
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
  await expect.element(screen.getByText(/当前生效目录：R4 · 允许准入 · 1 个租户/)).toBeVisible()
  await expect.element(screen.getByText(/配置草稿：V3 · 未预检/)).toBeVisible()
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
  await screen.getByRole('tab', { name: /连接配置/ }).click()
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
  await screen.getByRole('tab', { name: /连接配置/ }).click()
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
  await screen.getByRole('tab', { name: /连接配置/ }).click()
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
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect.element(screen.getByText('待核对', { exact: true })).toBeVisible()
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

it('submits one candidate preparation and restores durable child progress', async () => {
  const plan = {
    id: `plan_${'e'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  const task = {
    id: `task_${'f'.repeat(32)}`,
    action: 'candidate-prepare',
    tenant_id: 'same',
    plan_id: plan.id,
    state: 'queued',
    cancel_requested: false,
    error_code: '',
    result: {
      scope: 'candidate-resources-only',
      steps: [
        {
          id: `task_${'a'.repeat(32)}`,
          action: 'site-preflight',
          state: 'observed',
          error_code: '',
        },
      ],
    },
  }
  const calls: Record<string, unknown>[] = []
  let submitted = false
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.consumer_sources)
        return Response.json({ sources: [{ app_id: 'source-a', name: '正式客服', version_no: 7 }] })
      if (body.candidate_plan) {
        submitted = true
        return Response.json(task)
      }
      return Response.json({ tasks: submitted ? [task] : [] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('button', { name: '选择准备模板', exact: true }).click()
  await screen
    .getByRole('combobox', { name: '候选客服来源版本', exact: true })
    .selectOptions('source-a')
  await screen.getByText(/高级操作与计划校验摘要/).click()
  await screen.getByRole('button', { name: '准备候选：甲租户（same）', exact: true }).click()
  expect(calls.find((body) => body.candidate_plan)).toEqual({
    action: 'environment-task',
    candidate_plan: plan.id,
    tenant_id: 'same',
    source_app_id: 'source-a',
    source_version: 7,
  })
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect
    .element(screen.getByText('安装未执行 · 业务未验收 · 正式入口未启用', { exact: true }))
    .toBeVisible()
  await page.getByRole('region', { name: '候选准备进度：same' })
  await screen.getByRole('button', { name: '刷新目录', exact: true }).click()
  await expect.element(screen.getByText(task.result.steps[0].id, { exact: true })).toBeVisible()
  await screen.getByRole('tab', { name: /连接配置/ }).click()
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('未保存')
  await screen.getByRole('tab', { name: /开通与租户/ }).click()
  expect(
    screen.getByRole('button', { name: '准备候选：甲租户（same）', exact: true }).elements(),
  ).toHaveLength(0)
})

it('submits cold initial installation only after explicit confirm and reads back its states', async () => {
  const plan = {
    id: `plan_${'e'.repeat(32)}`,
    state: 'passed',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  const parent = {
    id: `task_${'f'.repeat(32)}`,
    action: 'candidate-prepare',
    tenant_id: 'same',
    plan_id: plan.id,
    state: 'queued',
    cancel_requested: false,
    error_code: '',
    result: {
      scope: 'candidate-resources-only',
      steps: [
        {
          id: `task_${'a'.repeat(32)}`,
          action: 'site-install',
          state: 'queued',
          error_code: '',
        },
      ],
      // 真实提交落库即带该字段；queued父摘要据此显示初装未执行而非纯运行安装。
      initial_installation: 'not_executed',
    },
  }
  const cold = {
    id: `task_${'a'.repeat(32)}`,
    action: 'site-install',
    tenant_id: 'same',
    plan_id: plan.id,
    state: 'queued',
    cancel_requested: false,
    error_code: '',
    result: { scope: 'site-cold-installation-only', installation: 'not_executed' },
  }
  const calls: Record<string, unknown>[] = []
  const confirm = vi.fn(() => false)
  vi.stubGlobal('confirm', confirm)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.consumer_sources)
        return Response.json({ sources: [{ app_id: 'source-a', name: '正式客服', version_no: 7 }] })
      if (body.candidate_plan) return Response.json(parent)
      return Response.json({
        tasks: calls.some((entry) => entry.candidate_plan) ? [parent, cold] : [],
      })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('button', { name: '选择准备模板', exact: true }).click()
  await screen
    .getByRole('combobox', { name: '候选客服来源版本', exact: true })
    .selectOptions('source-a')
  await screen.getByText(/高级操作与计划校验摘要/).click()
  const button = screen.getByRole('button', {
    name: '空机初装并准备：甲租户（same）',
    exact: true,
  })
  // 取消确认：一次同源POST都不发出。
  await button.click()
  expect(confirm).toHaveBeenCalled()
  expect(calls.some((body) => body.candidate_plan)).toBe(false)
  // 显式确认后同源载荷带上初装声明；缺省旧「准备」路径的四字段用例保持不变。
  confirm.mockReturnValue(true)
  await button.click()
  expect(calls.find((body) => body.candidate_plan)).toEqual({
    action: 'environment-task',
    candidate_plan: plan.id,
    tenant_id: 'same',
    source_app_id: 'source-a',
    source_version: 7,
    initial_installation: true,
  })
  // 提交回执只有父任务；初装子任务经「运行与排查」->「刷新任务」同列表读回。
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  const refresh = () => screen.getByRole('button', { name: '刷新任务', exact: true }).click()
  await refresh()
  const region = '空机初装进度：same'
  await expect.element(page.getByRole('region', { name: region })).toBeVisible()
  await expect.element(screen.getByText('空机初装 · same：待安装', { exact: true })).toBeVisible()
  // 初装结果未回写前父任务按真实queued摘要显示基础初装未执行。
  await expect
    .element(
      screen.getByText('基础初装：未执行 · 运行安装未执行 · 业务未验收 · 正式入口未启用', {
        exact: true,
      }),
    )
    .toBeVisible()
  cold.state = 'running'
  await refresh()
  await expect.element(screen.getByText('空机初装 · same：执行中', { exact: true })).toBeVisible()
  cold.state = 'reconcile'
  await refresh()
  await expect.element(screen.getByText('空机初装 · same：待核对', { exact: true })).toBeVisible()
  cold.state = 'cancelled'
  await refresh()
  await expect.element(screen.getByText('空机初装 · same：已取消', { exact: true })).toBeVisible()
  cold.state = 'observed'
  cold.result = { ...cold.result, installation: 'installed', version: '1.40.0' }
  await refresh()
  await expect.element(screen.getByText('空机初装 · same：已装', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('实际版本：1.40.0', { exact: true })).toBeVisible()
  cold.result = { ...cold.result, installation: 'already_installed' }
  await refresh()
  await expect
    .element(screen.getByText('空机初装 · same：已装（原安装保留）', { exact: true }))
    .toBeVisible()
  // 基础初装结果独立成字段显示；父任务自身运行安装字段仍保持原语义。
  parent.result = {
    ...parent.result,
    initial_installation: 'already_installed',
    installation: 'not_executed',
  }
  await refresh()
  await expect
    .element(
      screen.getByText(
        '基础初装：已装（原安装保留） · 运行安装未执行 · 业务未验收 · 正式入口未启用',
        { exact: true },
      ),
    )
    .toBeVisible()
})

it('submits an explicit inactive installation and keeps business activation separate', async () => {
  const parent = {
    id: `task_${'f'.repeat(32)}`,
    action: 'candidate-prepare',
    tenant_id: 'same',
    plan_id: `plan_${'e'.repeat(32)}`,
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: { scope: 'candidate-resources-only', steps: [] },
  }
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.candidate_task)
        return Response.json({
          ...parent,
          id: `task_${'a'.repeat(32)}`,
          action: 'candidate-install',
          state: 'queued',
          result: { scope: 'candidate-installation-only', installation: 'not_executed' },
        })
      return Response.json({ tasks: [parent] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('button', { name: '安装停用候选：same', exact: true }).click()
  expect(calls.find((body) => body.installation)).toEqual({
    action: 'environment-task',
    candidate_task: parent.id,
    installation: true,
  })
  await expect
    .element(
      screen.getByText('安装回执不含运行登记 · 业务未验收 · 正式入口未启用', { exact: true }),
    )
    .toBeVisible()
})

it('requests managed-host verification and shows the collected evidence', async () => {
  const plan = {
    id: `plan_${'b'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  const task = {
    id: `task_${'c'.repeat(32)}`,
    action: 'host-verify',
    environment: 'a',
    plan_id: plan.id,
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: {
      scope: 'managed-host-verification-only',
      evidence: { remote_hostname: 'ecs-2c54', service_user_present: true },
    },
  }
  const calls: Record<string, unknown>[] = []
  let submitted = false
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-drafts') return Response.json(data)
      calls.push(body)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.verify_task) {
        submitted = true
        return Response.json(task)
      }
      return Response.json({ tasks: submitted ? [task] : [] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByText(/高级操作与计划校验摘要/).click()
  await screen.getByRole('button', { name: '主机身份验收', exact: true }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    verify_task: 'managed-host-identity',
    plan_id: plan.id,
  })
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect
    .element(screen.getByText('主机身份验收 · a：已完成', { exact: true }))
    .toBeVisible()
  await expect.element(screen.getByText('现场主机：ecs-2c54', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('服务身份：存在', { exact: true })).toBeVisible()
  await expect
    .element(
      screen.getByText('仅证明任务落在已登记主机，不代替正式网络、容量与业务验收', { exact: true }),
    )
    .toBeVisible()
  await screen.getByRole('button', { name: '刷新目录', exact: true }).click()
  await expect
    .element(screen.getByText('主机身份验收 · a：已完成', { exact: true }))
    .toBeVisible()
})

it('registers an installed candidate with a fresh plan without enabling the tenant', async () => {
  const installed = {
    id: `task_${'d'.repeat(32)}`,
    action: 'candidate-install',
    tenant_id: 'same',
    plan_id: `plan_${'a'.repeat(32)}`,
    state: 'expired',
    cancel_requested: false,
    error_code: '',
    result: { installation: 'installed', version: '1.35.0' },
  }
  const plan = {
    id: `plan_${'e'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.installation_task)
        return Response.json({
          ...installed,
          id: `task_${'f'.repeat(32)}`,
          action: 'candidate-register',
          state: 'observed',
          result: { result: 'registered', activated: false, business_acceptance: false },
        })
      return Response.json({ tasks: [installed] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect
    .element(screen.getByRole('button', { name: '登记已安装候选：same', exact: true }))
    .toBeDisabled()
  await screen.getByRole('tab', { name: /开通与租户/ }).click()
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('button', { name: '登记已安装候选：same', exact: true }).click()
  expect(calls.find((body) => body.installation_task)).toEqual({
    action: 'environment-task',
    installation_task: installed.id,
    registration_plan: plan.id,
  })
  await expect
    .element(screen.getByText('运行资源已登记 · 业务未验收 · 正式入口未启用', { exact: true }))
    .toBeVisible()
})

it('starts explicit business validation from registration without accepting a verdict', async () => {
  const registration = {
    id: `task_${'a'.repeat(32)}`,
    action: 'candidate-register',
    tenant_id: 'same',
    plan_id: `plan_${'b'.repeat(32)}`,
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: { result: 'registered' },
  }
  const plan = {
    id: `plan_${'c'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.registration_task)
        return Response.json({
          ...registration,
          id: `task_${'d'.repeat(32)}`,
          action: 'candidate-validate',
          state: 'queued',
          result: {
            checks: [
              { capability: 'consumer', state: 'pending' },
              { capability: 'order', state: 'pending' },
              { capability: 'offline', state: 'pending' },
            ],
          },
        })
      return Response.json({ tasks: [registration] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('textbox', { name: '受控验收案例', exact: true }).fill('approved-case')
  await screen.getByRole('button', { name: '验证已登记业务：same', exact: true }).click()
  expect(calls.find((body) => body.registration_task)).toEqual({
    action: 'environment-task',
    registration_task: registration.id,
    validation_plan: plan.id,
    capabilities: ['consumer', 'order', 'offline'],
    case_ref: 'approved-case',
  })
  await expect.element(screen.getByText(/业务准确性待确认，正式入口未启用/)).toBeVisible()
})

it('publishes from verified business and restores the same operation with visible admission', async () => {
  // 与服务端发布闸门同形状：三项能力全部 passed 的验证才可发布（publication.py submit 拒收其他形状）。
  const validation = {
    id: `task_${'a'.repeat(32)}`,
    action: 'candidate-validate',
    tenant_id: 'same',
    plan_id: `plan_${'b'.repeat(32)}`,
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: {
      checks: [
        { capability: 'consumer', state: 'passed' },
        { capability: 'offline', state: 'passed' },
        { capability: 'order', state: 'passed' },
      ],
    },
  }
  const plan = {
    id: `plan_${'b'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  let publication: Record<string, unknown> | null = null
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.validation_task) {
        publication = {
          ...validation,
          id: `task_${'c'.repeat(32)}`,
          action: 'environment-publish',
          state: 'observed',
          result: { phase: 'activated', activated: true, directory: 'synchronized' },
        }
        return Response.json(publication)
      }
      if (body.restore_publication) {
        publication = {
          ...publication,
          state: 'cancelled',
          result: { phase: 'restored', activated: false },
        }
        return Response.json(publication)
      }
      return Response.json({ tasks: publication ? [publication, validation] : [validation] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('button', { name: '发布并开通：same', exact: true }).click()
  expect(calls.find((body) => body.validation_task)).toEqual({
    action: 'environment-task',
    validation_task: validation.id,
    publication_plan: plan.id,
    confirm_readonly: true,
    confirm_customer_knowledge: true,
    acceptance_ref: 'readonly-diagnosis',
  })
  await expect.element(screen.getByText(/正式开通.*准入 已启用/)).toBeVisible()
  await screen.getByRole('button', { name: '刷新目录', exact: true }).click()
  await screen.getByRole('button', { name: '恢复本次开通', exact: true }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    restore_publication: `task_${'c'.repeat(32)}`,
  })
  await expect.element(screen.getByText(/正式开通.*已恢复.*准入 未启用/)).toBeVisible()
})

it('offers validation publish only when all three capabilities passed, and keeps it in the active filter', async () => {
  // 判据来源：publication.py 的 submit 硬闸门——验证范围不等于三项能力时发布必遭拒收，
  // 页面按自身契约不提供该动作；凡就绪验证均保留发布动作（服务端按所选验证的证据链发布，
  // 同租户多条就绪验证互不等价）。「进行中/待发布」筛选按租户取最新就绪代表，
  // 已具备的验证须在其中保持可见，否则操作者找不到发布入口。
  // 负例形状：validation.py 在任一检查未过时把任务终态置为 blocked 而非 observed，
  // 故 observed 而不可发布的真实形状是范围缩窄的验证（checks 只覆盖部分能力且全部 passed）。
  const fullValidation = {
    id: `task_${'5'.repeat(32)}`,
    action: 'candidate-validate',
    tenant_id: 'same',
    plan_id: `plan_${'b'.repeat(32)}`,
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: {
      checks: [
        { capability: 'consumer', state: 'passed' },
        { capability: 'offline', state: 'passed' },
        { capability: 'order', state: 'passed' },
      ],
    },
  }
  const fullValidationOlder = {
    ...fullValidation,
    id: `task_${'7'.repeat(32)}`,
  }
  const scopedValidation = {
    ...fullValidation,
    id: `task_${'6'.repeat(32)}`,
    tenant_id: 'other',
    result: {
      checks: [
        { capability: 'consumer', state: 'passed' },
        { capability: 'order', state: 'passed' },
      ],
    },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-drafts') return Response.json(data)
      return Response.json({ tasks: [scopedValidation, fullValidation, fullValidationOlder] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  // 同租户两条就绪验证各自保留发布动作（证据链不等价，服务端按所选验证发布）
  expect(
    screen.getByRole('button', { name: '发布并开通：same', exact: true }).elements(),
  ).toHaveLength(2)
  expect(
    screen.getByRole('button', { name: '发布并开通：other', exact: true }).elements(),
  ).toHaveLength(0)
  await screen.getByRole('button', { name: '进行中/待发布 (1)', exact: true }).click()
  // 筛选按租户只留最新就绪代表，既不丢发布入口也不堆历史重复项
  await expect.element(screen.getByText(fullValidation.id, { exact: true })).toBeVisible()
  expect(screen.getByText(fullValidationOlder.id, { exact: true }).elements()).toHaveLength(0)
  expect(screen.getByText(scopedValidation.id, { exact: true }).elements()).toHaveLength(0)
})

it('renders environment overview, tenant capability matrix, live vs receipt nodes, and clears on revoked overview refresh', async () => {
  const overview = {
    schema: 'aiops-environment-overview/1',
    control_environment: 'a',
    control_execution_environment: 'a',
    control_version: '1.40.4',
    generated_at: 1795597500,
    registry_revision: 4,
    environments: [
      {
        id: 'a',
        name: '环境甲',
        is_control: true,
        status: 'active',
        enabled: true,
        draft_revision: 2,
        registry_changed: false,
        tenant_count: 1,
        enabled_tenant_count: 1,
        active_route_count: 2,
        nodes: [
          {
            source: 'heartbeat',
            node_id: 'node-41',
            status: 'online',
            observed_at: 1795597490,
            age_seconds: 10,
            version: '1.40.4',
            tool_contract: 'request-target/2',
            schemas: ['operator-agent/1'],
            tools_compatible: true,
            available_tools: ['query_order_detail', 'query_station_health'],
          },
        ],
        tenants: [
          {
            id: 'same',
            name: '甲租户',
            admission: 'enabled',
            effective_enabled: true,
            draft_enabled: true,
            workspace: { id: 'ws-a-1', state: 'ready', source: 'runtime' },
            knowledge: {
              knowledge_base_id: '',
              connection_id: '',
              active_grants: 2,
              state: 'ready',
            },
            apps: {
              operator: {
                app_id: '11111111-1111-1111-1111-111111111111',
                version_no: 36,
                state: 'active',
                source: 'runtime',
              },
              consumer: {
                app_id: '22222222-2222-2222-2222-222222222222',
                version_no: 35,
                state: 'active',
                source: 'runtime',
              },
            },
            capabilities: {
              consumer: { state: 'passed', error_code: '', observed_at: 1795597400, task_id: 't1' },
              order: {
                state: 'blocked',
                error_code: 'ORDER_MISSING',
                observed_at: 1795597400,
                task_id: 't1',
              },
              offline: { state: 'unverified', error_code: '', observed_at: null, task_id: '' },
            },
            accuracy_confirmed: false,
            latest_task: null,
          },
        ],
        latest_task: null,
      },
      {
        id: 'b',
        name: '环境乙',
        is_control: false,
        status: 'disabled',
        enabled: false,
        draft_revision: null,
        registry_changed: false,
        tenant_count: 1,
        enabled_tenant_count: 0,
        active_route_count: 0,
        nodes: [
          {
            source: 'task_receipt',
            node_id: 'ecs-95',
            status: 'observed',
            observed_at: 1795597450,
            version: '1.40.4',
            remote_hostname: 'ecs-95',
            service_user_present: true,
            installation: 'installed',
            task_id: `task_${'1'.repeat(32)}`,
            task_action: 'host-verify',
          },
        ],
        tenants: [
          {
            id: 'same',
            name: '乙租户',
            admission: 'disabled',
            effective_enabled: false,
            draft_enabled: null,
            workspace: { id: 'ws-b-1', state: 'created', source: 'runtime' },
            knowledge: {
              knowledge_base_id: 'kb-b-12345678',
              connection_id: 'conn-b-1',
              active_grants: 0,
              state: 'ready',
            },
            apps: {
              operator: {
                app_id: '33333333-3333-3333-3333-333333333333',
                version_no: null,
                state: 'candidate',
                source: 'task_receipt',
              },
              consumer: { app_id: '', version_no: null, state: 'unprepared', source: 'none' },
            },
            capabilities: {
              consumer: {
                state: 'expired',
                error_code: '',
                observed_at: 1795590000,
                task_id: 't2',
              },
              order: { state: 'unverified', error_code: '', observed_at: null, task_id: '' },
              offline: { state: 'unverified', error_code: '', observed_at: null, task_id: '' },
            },
            accuracy_confirmed: false,
            latest_task: null,
          },
        ],
        latest_task: null,
      },
    ],
  }
  const calls: Record<string, unknown>[] = []
  let overviewRevoked = false
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json({ ...data, overview })
      if (body.action === 'environment-overview') {
        if (overviewRevoked) {
          return Response.json(
            { error: { code: 'ENVIRONMENT_MANAGEMENT_FORBIDDEN' } },
            { status: 403 },
          )
        }
        return Response.json(overview)
      }
      return Response.json({ tasks: [] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  // 首屏默认只展示简洁租户状态（不堆砌 UUID / 顶部大表 / 工具列表），点「查看详情」再展开技术标识
  await expect.element(screen.getByRole('region', { name: '租户与能力总览' })).toBeVisible()
  expect(screen.getByRole('region', { name: '环境总览' }).elements()).toHaveLength(0)
  await expect.element(screen.getByText(/智能客服：通过/)).toBeVisible()
  await expect.element(screen.getByText(/异常订单：阻塞（ORDER_MISSING）/)).toBeVisible()
  expect(screen.getByText('ws-a-1', { exact: true }).elements()).toHaveLength(0)
  await screen.getByRole('button', { name: '查看详情', exact: true }).click()
  await expect.element(screen.getByText('ws-a-1', { exact: true })).toBeVisible()
  await expect.element(screen.getByText(/管家：运行中/)).toBeVisible()
  await expect.element(screen.getByText(/业务准确性：待人工确认/)).toBeVisible()

  // 切到「运行与排查」查看节点、工具清单（默认折叠）与全环境总览对比
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect.element(screen.getByRole('region', { name: '环境总览' })).toBeVisible()
  await expect.element(screen.getByRole('region', { name: '运行节点与工具' })).toBeVisible()
  await expect.element(screen.getByText('node-41', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('2 个工具兼容', { exact: true })).toBeVisible()
  await screen.getByText('展开工具清单（2）').click()
  await expect.element(screen.getByText('query_order_detail', { exact: true })).toBeVisible()

  await screen.getByRole('button', { name: '环境乙（b）' }).click()
  await expect.element(screen.getByText('ecs-95', { exact: true })).toBeVisible()
  await expect.element(screen.getByText(/安装：已装/)).toBeVisible()

  await screen.getByRole('button', { name: '刷新运行状态' }).click()
  expect(calls.at(-1)).toEqual({ action: 'environment-overview' })

  overviewRevoked = true
  await screen.getByRole('button', { name: '刷新运行状态' }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('当前账号没有环境管理授权')
  expect(screen.getByRole('region', { name: '环境总览' }).elements()).toHaveLength(0)
  await expect.element(screen.getByRole('button', { name: '重试读取运行状态' })).toBeVisible()
})

it('submits one-click tenant onboarding with validation and keeps explicit human publish confirmation', async () => {
  const plan = {
    id: `plan_${'e'.repeat(32)}`,
    state: 'blocked',
    executable: false,
    draft_revision: 2,
    registry_revision: 4,
    checks: [],
  }
  const validationId = `task_${'9'.repeat(32)}`
  const onboardTask = {
    id: `task_${'8'.repeat(32)}`,
    action: 'tenant-onboard',
    tenant_id: 'same',
    plan_id: plan.id,
    state: 'queued',
    cancel_requested: false,
    error_code: '',
    result: {
      scope: 'tenant-one-click-onboarding',
      ready_to_publish: false,
      validation_task_id: '',
      stages: [
        {
          id: `task_${'1'.repeat(32)}`,
          action: 'candidate-prepare',
          state: 'queued',
          error_code: '',
        },
      ],
    },
  }
  const calls: Record<string, unknown>[] = []
  let submitted = false
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      calls.push(body)
      if (body.action === 'environment-drafts') return Response.json(data)
      if (body.action === 'environment-plan') return Response.json(plan)
      if (body.consumer_sources)
        return Response.json({ sources: [{ app_id: 'source-a', name: '正式客服', version_no: 7 }] })
      if (body.onboard_plan) {
        submitted = true
        return Response.json(onboardTask)
      }
      if (body.validation_task) {
        return Response.json({
          id: `task_${'7'.repeat(32)}`,
          action: 'environment-publish',
          tenant_id: 'same',
          state: 'observed',
          cancel_requested: false,
          error_code: '',
          result: { phase: 'activated', activated: true, directory: 'synchronized' },
        })
      }
      return Response.json({ tasks: submitted ? [onboardTask] : [] })
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('button', { name: '选择准备模板', exact: true }).click()
  await screen
    .getByRole('combobox', { name: '候选客服来源版本', exact: true })
    .selectOptions('source-a')
  await screen.getByRole('textbox', { name: '一键开通验收案例', exact: true }).fill('case-01')
  await screen.getByRole('button', { name: '一键开通：甲租户（same）', exact: true }).click()
  expect(calls.find((body) => body.onboard_plan)).toEqual({
    action: 'environment-task',
    onboard_plan: plan.id,
    tenant_id: 'same',
    source_app_id: 'source-a',
    source_version: 7,
    case_ref: 'case-01',
    capabilities: ['consumer', 'order', 'offline'],
  })
  await expect.element(screen.getByRole('region', { name: '一键开通进度：same' })).toBeVisible()
  await expect
    .element(screen.getByRole('button', { name: '取消一键开通', exact: true }))
    .toBeVisible()

  const renewedPlanId = `plan_${'f'.repeat(32)}`
  onboardTask.plan_id = renewedPlanId
  onboardTask.state = 'observed'
  onboardTask.result = {
    scope: 'tenant-one-click-onboarding',
    ready_to_publish: true,
    validation_task_id: validationId,
    stages: [
      {
        id: `task_${'1'.repeat(32)}`,
        action: 'candidate-prepare',
        state: 'observed',
        error_code: '',
      },
      {
        id: `task_${'2'.repeat(32)}`,
        action: 'candidate-install',
        state: 'observed',
        error_code: '',
        version: '1.40.4',
      },
      {
        id: `task_${'3'.repeat(32)}`,
        action: 'candidate-register',
        state: 'observed',
        error_code: '',
      },
      { id: validationId, action: 'candidate-validate', state: 'observed', error_code: '' },
    ],
  }
  await screen.getByRole('button', { name: '刷新', exact: true }).click()
  await expect.element(screen.getByText(/技术验收已就绪，待人工确认发布/)).toBeVisible()
  // 切到环境乙再切回环境甲（清空本地 plan state + 重置冷装/案例输入，模拟刷新恢复）
  await screen.getByRole('checkbox', { name: /包含空机初装/ }).click()
  await screen.getByRole('button', { name: '环境乙（b）' }).click()
  await screen.getByRole('button', { name: '环境甲（a）' }).click()
  // 即使页面未重新预检（本地 plan=null），依然在默认首屏用续签后的 item.plan_id 发起显式人工发布确认
  await screen.getByRole('button', { name: '确认发布并开通：same', exact: true }).click()
  expect(calls.find((body) => body.validation_task)).toEqual({
    action: 'environment-task',
    validation_task: validationId,
    publication_plan: renewedPlanId,
    confirm_readonly: true,
    confirm_customer_knowledge: true,
    acceptance_ref: 'readonly-diagnosis',
  })
  // 重新预检并拉取准备模板后确认冷装与案例输入已随切环境重置，不跨环境泄漏
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('button', { name: '选择准备模板', exact: true }).click()
  await expect
    .element(screen.getByRole('textbox', { name: '一键开通验收案例', exact: true }))
    .toHaveValue('')
  await expect.element(screen.getByRole('checkbox', { name: /包含空机初装/ })).not.toBeChecked()
})

it('supports view-mode switching, clickable overview navigation with dirty guard, and task filtering', async () => {
  const overview = {
    schema: 'aiops-environment-overview/1',
    control_environment: 'a',
    control_execution_environment: 'a',
    control_version: '1.40.4',
    generated_at: 1795597500,
    registry_revision: 4,
    environments: [
      {
        id: 'a',
        name: '环境甲',
        is_control: true,
        status: 'active',
        enabled: true,
        draft_revision: 2,
        registry_changed: false,
        tenant_count: 1,
        enabled_tenant_count: 1,
        active_route_count: 2,
        nodes: [
          {
            source: 'heartbeat',
            node_id: 'node-41',
            status: 'online',
            observed_at: 1795597490,
            age_seconds: 10,
            version: '1.40.4',
            tool_contract: 'request-target/2',
            schemas: ['operator-agent/1'],
            tools_compatible: true,
            available_tools: ['cpms_order_detail', 'cpms_device_offline'],
          },
        ],
        tenants: [
          {
            id: 'same',
            name: '甲租户',
            admission: 'enabled',
            effective_enabled: true,
            draft_enabled: true,
            workspace: { id: 'ws-a-1', state: 'ready', source: 'runtime' },
            knowledge: {
              knowledge_base_id: '',
              connection_id: '',
              active_grants: 4,
              state: 'ready',
            },
            apps: {
              operator: {
                app_id: '11111111-1111-1111-1111-111111111111',
                version_no: 36,
                state: 'active',
                source: 'runtime',
              },
              consumer: {
                app_id: '22222222-2222-2222-2222-222222222222',
                version_no: 35,
                state: 'active',
                source: 'runtime',
              },
            },
            capabilities: {
              consumer: { state: 'passed', error_code: '', observed_at: 1795597400, task_id: 't1' },
              order: { state: 'passed', error_code: '', observed_at: 1795597400, task_id: 't1' },
              offline: { state: 'unverified', error_code: '', observed_at: null, task_id: '' },
            },
            accuracy_confirmed: false,
            latest_task: null,
          },
        ],
        latest_task: null,
      },
      {
        id: 'b',
        name: '环境乙',
        is_control: false,
        status: 'disabled',
        enabled: false,
        draft_revision: 5,
        registry_changed: false,
        tenant_count: 1,
        enabled_tenant_count: 0,
        active_route_count: 0,
        nodes: [],
        tenants: [],
        latest_task: null,
      },
    ],
  }
  const activeOnboard = {
    id: `task_${'1'.repeat(32)}`,
    action: 'tenant-onboard',
    tenant_id: 'same',
    plan_id: `plan_${'a'.repeat(32)}`,
    state: 'running',
    cancel_requested: false,
    error_code: '',
    result: {
      scope: 'tenant-one-click-onboarding',
      ready_to_publish: false,
      stages: [
        {
          id: `task_${'2'.repeat(32)}`,
          action: 'candidate-prepare',
          state: 'observed',
          error_code: '',
        },
        {
          id: `task_${'3'.repeat(32)}`,
          action: 'candidate-install',
          state: 'running',
          error_code: '',
        },
      ],
    },
  }
  const oldPreflight = {
    id: `task_${'4'.repeat(32)}`,
    action: 'site-preflight',
    plan_id: `plan_${'a'.repeat(32)}`,
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: { checks: [{ id: 'host-identity', state: 'passed' }] },
  }
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body))
      if (body.action === 'environment-drafts') return Response.json({ ...data, overview })
      if (body.action === 'environment-plan')
        return Response.json({
          id: `plan_${'a'.repeat(32)}`,
          state: 'blocked',
          executable: false,
          draft_revision: 2,
          registry_revision: 4,
          checks: [
            { id: 'references', state: 'passed' },
            { id: 'application', state: 'passed' },
            { id: 'network', state: 'pending' },
          ],
        })
      if (body.consumer_sources)
        return Response.json({
          sources: [{ app_id: 'source-a', name: '正式客服模板', version_no: 7 }],
        })
      return Response.json({ tasks: [activeOnboard, oldPreflight] })
    }),
  )
  await page.viewport(1440, 960)
  const screen = await render(<EnvironmentManagement />)
  // 默认首屏「开通与租户」简洁呈现：无大表/配置表单/单步历史干扰
  await expect.element(screen.getByRole('region', { name: '下一步操作' })).toBeVisible()
  expect(screen.getByRole('textbox', { name: '环境名称', exact: true }).elements()).toHaveLength(0)

  // 预检并选择模板后呈现极简一键开通面板
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('button', { name: '选择准备模板', exact: true }).click()

  // 切到「运行与排查」检查全量任务筛选与全环境总览
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect.element(screen.getByRole('region', { name: '环境总览' })).toBeVisible()
  await screen.getByRole('button', { name: '进行中/待发布 (1)', exact: true }).click()
  await expect.element(screen.getByText(activeOnboard.id, { exact: true })).toBeVisible()
  expect(screen.getByText(oldPreflight.id, { exact: true }).elements()).toHaveLength(0)
  await screen.getByRole('button', { name: '单步与核验 (1)', exact: true }).click()
  await expect.element(screen.getByText(oldPreflight.id, { exact: true })).toBeVisible()
  expect(screen.getByText(activeOnboard.id, { exact: true }).elements()).toHaveLength(0)
  await screen.getByRole('button', { name: '全部 (2)', exact: true }).click()

  // 切到「连接配置」检查表单与脏表单切换确认弹窗
  await screen.getByRole('tab', { name: /连接配置/ }).click()
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('脏编辑不丢失')
  await screen.getByRole('button', { name: '环境乙（b）' }).click()
  await expect.element(screen.getByRole('alertdialog')).toBeVisible()
  await screen.getByRole('button', { name: '继续编辑', exact: true }).click()
  await expect
    .element(screen.getByRole('textbox', { name: '环境名称', exact: true }))
    .toHaveValue('脏编辑不丢失')
})

it('guards enter-key submit in case input, expired plan re-preflight, view transitions and clipboard failure', async () => {
  const calls: Array<{ action: string; body: Record<string, unknown> }> = []
  let planCall = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>
      const action = String(body.action || '')
      calls.push({ action, body })
      if (action === 'environment-drafts') return Response.json(data)
      if (action === 'environment-overview') {
        return Response.json({
          registry_revision: 4,
          control_version: '1.40.4',
          environments: [
            {
              id: 'a',
              name: '环境甲',
              status: 'disabled',
              is_control: false,
              enabled_tenant_count: 0,
              tenant_count: 1,
              draft_revision: 2,
              registry_changed: false,
              active_route_count: 0,
              nodes: [],
              tenants: [],
              latest_task: null,
            },
          ],
        })
      }
      if (action === 'environment-task') {
        if (body.consumer_sources) {
          return Response.json({
            sources: [{ app_id: 'source-a', name: '正式客服模板', version_no: 7 }],
          })
        }
        return Response.json({
          tasks: [
            {
              id: `task_${'9'.repeat(32)}`,
              action: 'tenant-onboard',
              tenant_id: 'same',
              plan_id: `plan_${'a'.repeat(32)}`,
              state: 'observed',
              cancel_requested: false,
              error_code: '',
              result: {
                scope: 'tenant-one-click-onboarding',
                ready_to_publish: true,
                stages: [],
              },
            },
          ],
        })
      }
      if (action === 'environment-plan') {
        planCall += 1
        if (body.id) {
          return Response.json({
            id: `plan_${'a'.repeat(32)}`,
            draft_revision: 2,
            registry_revision: 4,
            state: 'expired',
            executable: false,
            checks: [],
          })
        }
        // 服务端计划状态只有 blocked（冻结有效）与 expired 两种（plans.py 的 _public）。
        return Response.json({
          id: `plan_${String(planCall).padStart(32, 'a')}`,
          draft_revision: 2,
          registry_revision: 4,
          state: 'blocked',
          executable: false,
          checks: [],
        })
      }
      return Response.json({})
    }),
  )

  const writeTextMock = vi.fn().mockRejectedValue(new Error('clipboard blocked'))
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: writeTextMock },
  })

  const screen = await render(<EnvironmentManagement />)
  await expect.element(screen.getByRole('heading', { name: '环境与租户' })).toBeVisible()

  // 1. config Tab 下点击预检自动切回「开通与租户」并展示计划
  await screen.getByRole('tab', { name: /连接配置/ }).click()
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await expect.element(screen.getByRole('region', { name: '开通计划' })).toBeVisible()

  // 2. 在一键开通验收案例输入并按 Enter 绝不误触发 environment-draft-save
  await screen.getByRole('button', { name: '选择准备模板', exact: true }).click()
  const caseInput = screen.getByRole('textbox', { name: '一键开通验收案例', exact: true })
  await caseInput.fill('case-enter-guard')
  await userEvent.keyboard('{Enter}')
  expect(calls.some((c) => c.action === 'environment-draft-save')).toBe(false)

  // 3. 计划过期后直接展示并可点击「重新预检」恢复为有效计划
  await screen.getByRole('button', { name: '重新核对计划', exact: true }).click()
  await expect.element(screen.getByText('输入或有效期已变化，请重新预检。')).toBeVisible()
  await screen.getByRole('button', { name: '重新预检', exact: true }).click()
  await expect
    .element(
      screen.getByRole('region', { name: '开通计划' }).getByText('计划已冻结', { exact: true }),
    )
    .toBeVisible()

  // 4. 在「运行与排查」验证待发布筛选保留、空筛选切回全部与 clipboard reject 降级
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  const taskId = `task_${'9'.repeat(32)}`
  await screen.getByRole('button', { name: '进行中/待发布 (1)', exact: true }).click()
  await expect.element(screen.getByText(taskId, { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: '单步与核验 (0)', exact: true }).click()
  await expect.element(screen.getByText('当前筛选下无任务记录')).toBeVisible()
  await screen.getByRole('button', { name: '查看全部任务（1）', exact: true }).click()
  await expect.element(screen.getByText(taskId, { exact: true })).toBeVisible()
  await screen.getByRole('button', { name: `复制 ${taskId}`, exact: true }).click()
  expect(writeTextMock).toHaveBeenCalledWith(taskId)

  // 5. 任意 Tab 点击「新增环境」自动切入「连接配置」表单；再切到「运行与排查」不白屏
  await screen.getByRole('button', { name: '新增环境', exact: true }).click()
  await expect.element(screen.getByRole('heading', { name: '连接配置' })).toBeVisible()
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await expect.element(screen.getByRole('region', { name: '运行排查状态' })).toBeVisible()
})

it('deduplicates hero publish CTA across repeated ready onboards, uses fresh plan id, and guards overview row click when dirty', async () => {
  const calls: Array<{ action: string; body: Record<string, unknown> }> = []
  vi.stubGlobal(
    'confirm',
    vi.fn(() => true),
  )
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, init) => {
      const body = JSON.parse(String(init?.body)) as Record<string, unknown>
      const action = String(body.action || '')
      calls.push({ action, body })
      if (action === 'environment-drafts') {
        return Response.json({
          ...data,
          overview: {
            schema: 'aiops-environment-overview/1',
            control_environment: 'a',
            control_execution_environment: 'a',
            control_version: '1.40.4',
            generated_at: 1795597500,
            registry_revision: 4,
            environments: [
              {
                id: 'a',
                name: '环境甲',
                is_control: false,
                status: 'disabled',
                enabled: false,
                draft_revision: 2,
                registry_changed: false,
                tenant_count: 1,
                enabled_tenant_count: 0,
                active_route_count: 0,
                nodes: [],
                tenants: [],
                latest_task: null,
              },
              {
                id: 'b',
                name: '环境乙',
                is_control: false,
                status: 'disabled',
                enabled: false,
                draft_revision: null,
                registry_changed: false,
                tenant_count: 1,
                enabled_tenant_count: 0,
                active_route_count: 0,
                nodes: [],
                tenants: [],
                latest_task: null,
              },
            ],
          },
        })
      }
      if (action === 'environment-plan') {
        return Response.json({
          id: `plan_${'n'.repeat(32)}`,
          draft_revision: 2,
          registry_revision: 4,
          state: 'blocked',
          executable: false,
          checks: [],
        })
      }
      if (action === 'environment-task') {
        if (body.validation_task) {
          return Response.json({
            id: `task_${'p'.repeat(32)}`,
            action: 'environment-publish',
            tenant_id: 'same',
            state: 'observed',
            cancel_requested: false,
            error_code: '',
            result: { phase: 'activated', activated: true, directory: 'synchronized' },
          })
        }
        return Response.json({
          tasks: [
            {
              id: `task_${'2'.repeat(32)}`,
              action: 'tenant-onboard',
              tenant_id: 'same',
              plan_id: `plan_${'o'.repeat(32)}`,
              state: 'observed',
              cancel_requested: false,
              error_code: '',
              result: {
                scope: 'tenant-one-click-onboarding',
                ready_to_publish: true,
                validation_task_id: `task_${'v'.repeat(32)}`,
                stages: [],
              },
            },
            {
              id: `task_${'1'.repeat(32)}`,
              action: 'tenant-onboard',
              tenant_id: 'same',
              plan_id: `plan_${'x'.repeat(32)}`,
              state: 'observed',
              cancel_requested: false,
              error_code: '',
              result: {
                scope: 'tenant-one-click-onboarding',
                ready_to_publish: true,
                validation_task_id: `task_${'w'.repeat(32)}`,
                stages: [],
              },
            },
          ],
        })
      }
      return Response.json({})
    }),
  )

  const screen = await render(<EnvironmentManagement />)
  // 同一租户有 2 条历史 ready_to_publish，首屏 Hero 只渲染 1 个主发布按钮，无重复按钮
  await expect.element(screen.getByText(/待人工确认发布：甲租户（same）/)).toBeVisible()
  expect(
    screen.getByRole('button', { name: '确认发布并开通：same', exact: true }).elements(),
  ).toHaveLength(1)

  // 未被判过期的后台续签计划 plan_o... 不会被旧页面态 plan 覆盖
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
  await screen.getByRole('button', { name: '确认发布并开通：same', exact: true }).click()
  expect(calls.find((c) => c.body.validation_task)?.body).toMatchObject({
    validation_task: `task_${'v'.repeat(32)}`,
    publication_plan: `plan_${'o'.repeat(32)}`,
  })

  // 连接配置中已有租户编号锁定不可改且不可移除；公司网关地址允许留空
  await screen.getByRole('tab', { name: /连接配置/ }).click()
  await expect.element(screen.getByRole('textbox', { name: '租户 1 编号' })).toBeDisabled()
  expect(screen.getByRole('button', { name: '移除', exact: true }).elements()).toHaveLength(0)
  await expect
    .element(screen.getByRole('textbox', { name: '公司网关地址', exact: true }))
    .not.toBeRequired()

  // 脏表单下在「运行与排查」点击「环境总览」行同样触发未保存确认弹窗
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('脏配置')
  await screen.getByRole('tab', { name: /运行与排查/ }).click()
  await screen.getByRole('region', { name: '环境总览' }).getByText('环境乙（b）').click()
  await expect.element(screen.getByRole('alertdialog')).toBeVisible()
  await screen.getByRole('button', { name: '继续编辑', exact: true }).click()
})
