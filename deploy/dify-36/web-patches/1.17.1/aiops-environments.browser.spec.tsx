import { afterEach, expect, it, vi } from 'vite-plus/test'
import { render } from 'vitest-browser-react'
import { page } from 'vitest/browser'
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
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('未保存')
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
  await screen.getByRole('button', { name: '准备候选：甲租户（same）', exact: true }).click()
  expect(calls.find((body) => body.candidate_plan)).toEqual({
    action: 'environment-task',
    candidate_plan: plan.id,
    tenant_id: 'same',
    source_app_id: 'source-a',
    source_version: 7,
  })
  await expect
    .element(screen.getByText('安装未执行 · 业务未验收 · 正式入口未启用', { exact: true }))
    .toBeVisible()
  await page.getByRole('region', { name: '候选准备进度：same' })
  await screen.getByRole('button', { name: '刷新目录', exact: true }).click()
  await expect.element(screen.getByText(task.result.steps[0].id, { exact: true })).toBeVisible()
  await screen.getByRole('textbox', { name: '环境名称', exact: true }).fill('未保存')
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
  // 提交回执只有父任务；初装子任务经「刷新任务」同列表读回。
  const refresh = () => screen.getByRole('button', { name: '刷新任务', exact: true }).click()
  await refresh()
  const region = '空机初装进度：same'
  await expect.element(page.getByRole('region', { name: region })).toBeVisible()
  await expect
    .element(screen.getByText('空机初装 · same：待安装', { exact: true }))
    .toBeVisible()
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
  await expect
    .element(screen.getByText('空机初装 · same：正在取证', { exact: true }))
    .toBeVisible()
  cold.state = 'reconcile'
  await refresh()
  await expect
    .element(screen.getByText('空机初装 · same：结果待核对', { exact: true }))
    .toBeVisible()
  cold.state = 'cancelled'
  await refresh()
  await expect
    .element(screen.getByText('空机初装 · same：已取消', { exact: true }))
    .toBeVisible()
  cold.state = 'observed'
  cold.result = { ...cold.result, installation: 'installed', version: '1.40.0' }
  await refresh()
  await expect
    .element(screen.getByText('空机初装 · same：已装', { exact: true }))
    .toBeVisible()
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
  await screen.getByRole('button', { name: '主机身份验收', exact: true }).click()
  expect(calls.at(-1)).toEqual({
    action: 'environment-task',
    verify_task: 'managed-host-identity',
    plan_id: plan.id,
  })
  await expect
    .element(screen.getByText('主机身份验收 · a：本地观察通过', { exact: true }))
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
    .element(screen.getByText('主机身份验收 · a：本地观察通过', { exact: true }))
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
  await expect
    .element(screen.getByRole('button', { name: '登记已安装候选：same', exact: true }))
    .toBeDisabled()
  await screen.getByRole('button', { name: '配置与制品预检', exact: true }).click()
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
  const validation = {
    id: `task_${'a'.repeat(32)}`,
    action: 'candidate-validate',
    tenant_id: 'same',
    state: 'observed',
    cancel_requested: false,
    error_code: '',
    result: {},
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
  await page.screenshot({ path: './.vitest-browser/publication-render.png' })
})
