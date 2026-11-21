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
    .element(screen.getByText('注册未执行 · 业务未验收 · 正式入口未启用', { exact: true }))
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
