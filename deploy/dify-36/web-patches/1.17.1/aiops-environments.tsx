'use client'

import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogCancelButton,
  AlertDialogConfirmButton,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from '@langgenius/dify-ui/alert-dialog'
import { Button } from '@langgenius/dify-ui/button'
import { Checkbox } from '@langgenius/dify-ui/checkbox'
import { Input } from '@langgenius/dify-ui/input'
import { useEffect, useRef, useState } from 'react'
import { useRouter } from '@/next/navigation'
import { manage } from '@/service/aiops-management'

type Tenant = { id: string; name: string; enabled: boolean }
type Draft = {
  id: string
  name: string
  enabled: boolean
  gateway_url: string
  cpms_base_url: string
  company_gateway_url: string
  deployment_target: string
  credential_ref: string
  timezone: string
  tenants: Tenant[]
}
type Effective = Omit<Draft, 'deployment_target' | 'credential_ref' | 'timezone'>
type SavedDraft = {
  environment: string
  revision: number
  draft: Draft
  registry_changed: boolean
}
type Catalogue = {
  registry_revision: number
  can_create: boolean
  effective: Effective[]
  drafts: SavedDraft[]
}
type Plan = {
  id: string
  state: 'blocked' | 'expired'
  executable: false
  draft_revision: number
  registry_revision: number
  config_sha256?: string
  projection_sha256?: string
  source_revision?: string
  checks: { id: string; state: 'passed' | 'blocked' | 'pending' }[]
}
type SiteTask = {
  id: string
  action?: string
  tenant_id?: string
  environment?: string
  plan_id: string
  state: string
  cancel_requested: boolean
  error_code: string
  result: {
    workspace_id?: string
    knowledge_base_id?: string
    connection_id?: string
    app_id?: string
    scope?: string
    phase?: string
    activated?: boolean
    installation?: string
    result?: string
    version?: string
    evidence?: {
      remote_hostname?: string
      machine_id_sha256?: string
      service_user_present?: boolean
    }
    steps?: { id: string; action: string; state: string; error_code: string }[]
    checks?: { id?: string; capability?: string; state: string; error_code?: string }[]
  }
}
const publicationPhases: Record<string, string> = {
  readiness: '核验前置',
  capacity: '并发容量验证',
  central: '共享应用发布',
  target: '目标配置准备',
  staged: '目标配置已准备',
  verify: '正式接线验证',
  source: '同步配置来源',
  activate: '切换准入',
  activated: '准入已启用',
  restoring: '正在恢复',
  restored: '已恢复',
}

const taskLabels: Record<string, string> = {
  queued: '等待执行端',
  running: '正在取证',
  reconcile: '结果待核对',
  observed: '本地观察通过',
  blocked: '现场检查阻塞',
  cancelled: '已取消',
  expired: '已过期',
}
const preparationSteps: Record<string, string> = {
  'site-preflight': '现场预检',
  'workspace-prepare': '工作区',
  'knowledge-prepare': '知识资源',
  'knowledge-connect': '知识连接',
  'operator-prepare': '管家候选',
  'consumer-prepare': '客服候选',
}
const checkLabels: Record<string, string> = {
  workspace: '候选工作区',
  knowledge: '知识资源',
  connection: '候选知识连接',
  references: '部署与配置引用',
  application: '应用制品摘要',
  installer: '安装器摘要',
  configuration: '配置文件摘要',
  'declared-revision': '制品声明修订一致',
  'application-contents': '应用归档成员与清单',
  'installer-contents': '安装器归档成员与清单',
  'embedded-revision': '包内修订与声明一致',
  'config-projection': '配置与目录一致性',
  'host-identity': '目标主机身份',
  'local-path-identity': '本地安装归属',
  'installed-artifact': '已安装制品',
  registry: '目录一致性',
  recovery: '待恢复状态',
  'service-readiness': '服务版本与监听',
  'schema-inflight': '数据库与在飞任务',
  'resource-headroom': '启动资源余量',
  'managed-host-identity': '独立主机身份验收',
  'formal-network': '正式网络',
  capacity: '现场容量',
  network: '正式双向网络',
  business: '业务能力验证',
}
const empty = (): Draft => ({
  id: '',
  name: '',
  enabled: false,
  gateway_url: '',
  cpms_base_url: '',
  company_gateway_url: '',
  deployment_target: '',
  credential_ref: '',
  timezone: '',
  tenants: [],
})
const fields = [
  ['id', '环境编号', 32],
  ['name', '环境名称', 128],
  ['company_gateway_url', '公司网关地址', 2048],
  ['gateway_url', 'AI-Ops 入口地址', 2048],
  ['cpms_base_url', 'CPMS API 地址', 2048],
  ['deployment_target', '部署目标引用', 64],
  ['credential_ref', '凭据引用', 64],
  ['timezone', '时区', 64],
] as const

export default function EnvironmentManagement() {
  const router = useRouter()
  const [data, setData] = useState<Catalogue | null>(null)
  const [selected, setSelected] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [original, setOriginal] = useState('')
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [plan, setPlan] = useState<Plan | null>(null)
  const [caseRef, setCaseRef] = useState('')
  const [capabilities, setCapabilities] = useState(['consumer', 'order', 'offline'])
  const [tasks, setTasks] = useState<SiteTask[]>([])
  const [consumerSources, setConsumerSources] = useState<
    { app_id: string; name: string; version_no: number }[]
  >([])
  const [consumerSource, setConsumerSource] = useState('')
  const [taskError, setTaskError] = useState('')
  const taskViewRef = useRef(0)
  const [taskGeneration, setTaskGeneration] = useState(0)
  const [pending, setPending] = useState<(() => void) | null>(null)
  const saved = data?.drafts.find((item) => item.environment === selected)
  const live = data?.effective.find((item) => item.id === selected)
  const dirty = draft !== null && JSON.stringify(draft) !== original

  function select(catalogue: Catalogue, id: string) {
    const candidate = catalogue.drafts.find((item) => item.environment === id)
    const current = catalogue.effective.find((item) => item.id === id)
    const value = candidate?.draft ?? { ...empty(), ...current }
    setPlan(null)
    setConsumerSources([])
    setConsumerSource('')
    setTasks([])
    taskViewRef.current += 1
    setTaskGeneration((value) => value + 1)
    setSelected(id)
    setDraft(value)
    setOriginal(JSON.stringify(value))
    setError('')
    setNotice('')
  }
  function navigate(action: () => void) {
    if (dirty) setPending(() => action)
    else action()
  }
  async function load(preferred: string) {
    setBusy(true)
    setError('')
    try {
      const catalogue: Catalogue = await manage('environment-drafts')
      const ids = [
        ...catalogue.effective.map((item) => item.id),
        ...catalogue.drafts.map((item) => item.environment),
      ]
      setData(catalogue)
      select(catalogue, ids.includes(preferred) ? preferred : ids[0] || '')
    } catch (e) {
      setPlan(null)
      taskViewRef.current += 1
      setTasks([])
      setData(null)
      setDraft(null)
      setOriginal('')
      setNotice('')
      setError(e instanceof Error ? e.message : '读取环境目录失败')
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    void load('')
  }, [])
  useEffect(() => {
    if (!dirty) return
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    const leave = (event: MouseEvent) => {
      if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)
        return
      const link = event.target instanceof Element ? event.target.closest('a[href]') : null
      if (
        !(link instanceof HTMLAnchorElement) ||
        link.target === '_blank' ||
        link.hasAttribute('download')
      )
        return
      const target = new URL(link.href)
      if (target.origin !== location.origin || target.pathname === location.pathname) return
      event.preventDefault()
      event.stopPropagation()
      setPending(() => () => router.push(target.pathname + target.search + target.hash))
    }
    window.addEventListener('beforeunload', warn)
    document.addEventListener('click', leave, true)
    return () => {
      window.removeEventListener('beforeunload', warn)
      document.removeEventListener('click', leave, true)
    }
  }, [dirty, router])

  async function save() {
    if (!data || !draft || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await manage('environment-draft-save', {
        revision: saved?.revision ?? 0,
        registry_revision: data.registry_revision,
        draft,
      })
      // 使用保存回执更新版本；回读失败不能使下一次提交继续带旧版本。
      const row: SavedDraft = {
        environment: draft.id,
        revision: result.revision,
        draft,
        registry_changed: false,
      }
      setData({
        ...data,
        drafts: [...data.drafts.filter((item) => item.environment !== draft.id), row],
      })
      setPlan(null)
      setSelected(draft.id)
      setOriginal(JSON.stringify(draft))
      setNotice('草稿已保存，当前生效设置未改变。')
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存未完成，请刷新核对')
    } finally {
      setBusy(false)
    }
  }
  async function preflight(readback = false) {
    if (!data || !saved || busy || dirty) return
    setBusy(true)
    setError('')
    try {
      const result: Plan = await manage(
        'environment-plan',
        readback && plan
          ? { id: plan.id }
          : {
              environment: selected,
              revision: saved.revision,
              registry_revision: data.registry_revision,
            },
      )
      setPlan(result)
    } catch (e) {
      setPlan(null)
      setError(e instanceof Error ? e.message : '计划核对未完成')
    } finally {
      setBusy(false)
    }
  }
  async function siteTasks(payload: Record<string, unknown> = { environment: selected }) {
    setTaskError('')
    const view = taskViewRef.current
    try {
      const result = await manage('environment-task', payload)
      if (view !== taskViewRef.current) return
      if ('tasks' in result) setTasks(result.tasks)
      else if ('sources' in result) setConsumerSources(result.sources)
      else setTasks((current) => [result, ...current.filter((item) => item.id !== result.id)])
    } catch (e) {
      if (view !== taskViewRef.current) return
      setTasks([])
      setTaskError(e instanceof Error ? e.message : '任务读取未完成，请刷新核对')
    }
  }
  useEffect(() => {
    if (selected) void siteTasks({ environment: selected })
  }, [selected, taskGeneration])
  const preparing = tasks.some(
    (item) =>
      [
        'candidate-prepare',
        'candidate-install',
        'candidate-register',
        'candidate-validate',
        'environment-publish',
      ].includes(item.action || '') && ['queued', 'running', 'reconcile'].includes(item.state),
  )
  useEffect(() => {
    if (!selected || !preparing) return
    const timer = setInterval(() => void siteTasks({ environment: selected }), 5000)
    return () => clearInterval(timer)
  }, [selected, preparing])
  const rows = data
    ? [
        ...data.effective,
        ...data.drafts
          .filter((item) => !data.effective.some((value) => value.id === item.environment))
          .map((item) => item.draft),
      ]
    : []

  return (
    <section aria-label="环境与租户管理" className="h-full overflow-y-auto px-8 py-6">
      <AlertDialog
        open={pending !== null}
        onOpenChange={(open) => {
          if (!open) setPending(null)
        }}
      >
        <AlertDialogContent>
          <div className="p-6">
            <AlertDialogTitle>放弃未保存修改？</AlertDialogTitle>
            <AlertDialogDescription>
              继续后将重新读取所选环境，当前输入不会保存。
            </AlertDialogDescription>
          </div>
          <AlertDialogActions>
            <AlertDialogCancelButton>继续编辑</AlertDialogCancelButton>
            <AlertDialogConfirmButton
              onClick={() => {
                pending?.()
                setPending(null)
              }}
            >
              放弃并继续
            </AlertDialogConfirmButton>
          </AlertDialogActions>
        </AlertDialogContent>
      </AlertDialog>
      <header className="mb-5 flex flex-wrap items-center justify-between gap-3 border-b pb-4">
        <div>
          <h1 className="system-lg-semibold text-text-primary">环境与租户</h1>
          <p className="system-sm-regular text-text-secondary">维护业务环境连接与公司租户映射</p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={busy}
            onClick={() => {
              navigate(() => {
                void load(selected)
              })
            }}
          >
            刷新目录
          </Button>
          {data?.can_create && (
            <Button
              disabled={busy}
              onClick={() => {
                navigate(() => select(data, ''))
              }}
            >
              新增环境
            </Button>
          )}
        </div>
      </header>
      {error && (
        <p role="alert" className="mb-3 text-text-destructive">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="mb-3 text-text-secondary">
          {notice}
        </p>
      )}
      {!data && !error && <p role="status">正在读取环境目录…</p>}
      {data && draft && (
        <div className="grid gap-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
          <nav aria-label="业务环境列表" className="space-y-2">
            {rows.map((row) => (
              <Button
                key={row.id}
                variant="secondary"
                className="w-full justify-start"
                disabled={busy}
                aria-current={selected === row.id ? 'true' : undefined}
                onClick={() => {
                  navigate(() => select(data, row.id))
                }}
              >
                {row.name}（{row.id}）
              </Button>
            ))}
            {!rows.length && <p className="system-sm-regular text-text-secondary">尚未登记环境</p>}
          </nav>
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void save()
            }}
          >
            <p className="mb-2 system-sm-regular text-text-secondary">
              当前生效目录：
              {live
                ? `R${data.registry_revision} · ${live.enabled ? '允许准入' : '已停用'} · ${live.tenants.length} 个租户`
                : '此环境尚未生效'}
            </p>
            <p className="mb-4 system-sm-regular text-text-secondary">
              配置草稿：{saved ? `V${saved.revision}` : '未保存'} ·{' '}
              {plan && !dirty
                ? plan.state === 'expired'
                  ? '计划已过期'
                  : '已有预检计划'
                : '未预检'}
              {dirty ? ' · 有未保存修改' : ''}
            </p>
            {saved?.registry_changed && (
              <p className="mb-3 text-text-warning">
                生效目录在草稿保存后已变化，请核对连接和租户后重新保存。
              </p>
            )}
            <fieldset disabled={busy || (!selected && !data.can_create)} className="space-y-5">
              <legend className="mb-3 system-md-semibold">连接配置</legend>
              <div className="grid gap-3 sm:grid-cols-2">
                {fields.map(([key, label, maxLength]) => (
                  <label key={key} className="flex min-w-0 flex-col gap-1 system-sm-regular">
                    {label}
                    <Input
                      aria-label={label}
                      value={draft[key]}
                      required={key !== 'company_gateway_url'}
                      maxLength={maxLength}
                      disabled={key === 'id' && Boolean(selected)}
                      onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
                    />
                  </label>
                ))}
              </div>
              <p className="system-xs-regular text-text-tertiary">
                地址使用
                HTTPS；凭据填写已登记引用，不填写口令或令牌。时区示例：Asia/Shanghai、Asia/Singapore。
              </p>
              <label className="flex items-center gap-2 system-sm-regular">
                <Checkbox
                  aria-label="草稿环境准入"
                  checked={draft.enabled}
                  onCheckedChange={(value) => setDraft({ ...draft, enabled: value === true })}
                />
                预期允许环境准入（保存后仍不生效）
              </label>
              <section aria-label="租户草稿" className="space-y-3 border-t pt-4">
                <h2 className="system-md-semibold">租户（{draft.tenants.length}）</h2>
                {/* 顺序固定且只允许追加，不能以正在编辑的编号为key重挂输入。 */}
                {draft.tenants.map((tenant, index) => (
                  // oxlint-disable-next-line react/no-array-index-key -- Rows only append; editable ids must not remount inputs.
                  <div key={index} className="flex flex-wrap items-center gap-3">
                    <label
                      htmlFor={`tenant-id-${index}`}
                      className="flex flex-col gap-1 system-xs-regular"
                    >
                      公司租户编号
                      <Input
                        id={`tenant-id-${index}`}
                        aria-label={`租户 ${index + 1} 编号`}
                        required
                        maxLength={64}
                        value={tenant.id}
                        disabled={Boolean((saved?.draft.tenants ?? live?.tenants)?.[index])}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            tenants: draft.tenants.map((item, i) =>
                              i === index ? { ...item, id: event.target.value } : item,
                            ),
                          })
                        }
                      />
                    </label>
                    <label
                      htmlFor={`tenant-name-${index}`}
                      className="flex flex-col gap-1 system-xs-regular"
                    >
                      租户名称
                      <Input
                        id={`tenant-name-${index}`}
                        aria-label={`租户 ${index + 1} 名称`}
                        required
                        maxLength={128}
                        value={tenant.name}
                        onChange={(event) =>
                          setDraft({
                            ...draft,
                            tenants: draft.tenants.map((item, i) =>
                              i === index ? { ...item, name: event.target.value } : item,
                            ),
                          })
                        }
                      />
                    </label>
                    <label className="flex items-center gap-2 system-xs-regular">
                      <Checkbox
                        aria-label={`租户 ${index + 1} 准入`}
                        checked={tenant.enabled}
                        onCheckedChange={(value) =>
                          setDraft({
                            ...draft,
                            tenants: draft.tenants.map((item, i) =>
                              i === index ? { ...item, enabled: value === true } : item,
                            ),
                          })
                        }
                      />
                      预期准入
                    </label>
                  </div>
                ))}
                <Button
                  type="button"
                  variant="secondary"
                  onClick={() =>
                    setDraft({
                      ...draft,
                      tenants: [...draft.tenants, { id: '', name: '', enabled: false }],
                    })
                  }
                >
                  新增租户
                </Button>
                <p className="system-xs-regular text-text-tertiary">
                  租户编号按公司系统填写；登记不表示已核实租户或已开通能力。
                </p>
              </section>
              <footer className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
                <p className="max-w-xl system-sm-regular text-text-secondary">
                  保存只更新草稿；执行对应计划并通过各项核验后，才会改变资源和准入。
                </p>
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={!saved || dirty || saved.registry_changed}
                    onClick={() => void preflight()}
                  >
                    配置与制品预检
                  </Button>
                  <Button type="submit" variant="primary">
                    保存草稿
                  </Button>
                </div>
              </footer>
            </fieldset>
            {plan && !dirty && (
              <section aria-label="开通计划" className="mt-5 space-y-2 border-t pt-4">
                <h2 className="system-md-semibold">开通计划</h2>
                <p className="system-xs-regular break-all">
                  {plan.id} · 草稿V{plan.draft_revision} / 目录R{plan.registry_revision}
                </p>
                <p role="status">
                  {plan.state === 'expired'
                    ? '输入或有效期已变化，请重新预检。'
                    : '计划已冻结；执行前将核对各项条件，以对应任务结果为准。'}
                </p>
                <ul className="space-y-1">
                  {plan.checks.map((check) => (
                    <li key={check.id}>
                      {checkLabels[check.id || ''] ||
                        (
                          {
                            consumer: '智能客服',
                            order: '异常订单',
                            offline: '离线分析',
                          } as Record<string, string>
                        )[check.capability || ''] ||
                        check.id}
                      ：
                      {check.state === 'passed'
                        ? '通过'
                        : check.state === 'blocked'
                          ? '阻塞'
                          : '未验证'}
                    </li>
                  ))}
                </ul>
                {plan.config_sha256 && (
                  <p className="system-xs-regular break-all">配置摘要：{plan.config_sha256}</p>
                )}
                {plan.projection_sha256 && (
                  <p className="system-xs-regular break-all">
                    停用候选投影摘要：{plan.projection_sha256}
                  </p>
                )}
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy || plan.state === 'expired'}
                  onClick={() => void siteTasks({ plan_id: plan.id })}
                >
                  请求现场预检
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy || plan.state === 'expired'}
                  onClick={() =>
                    void siteTasks({ verify_task: 'managed-host-identity', plan_id: plan.id })
                  }
                >
                  主机身份验收
                </Button>
                <section aria-label="一次准备候选" className="space-y-2 pt-3">
                  <h3 className="system-sm-semibold">一次准备候选</h3>
                  <p>自动串联现场预检、工作区、知识连接与双端应用；不安装或启用正式服务。</p>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={busy || plan.state === 'expired'}
                    onClick={() => void siteTasks({ consumer_sources: true })}
                  >
                    选择准备模板
                  </Button>
                  {consumerSources.length > 0 && (
                    <>
                      <label className="block">
                        候选客服来源版本
                        <select
                          aria-label="候选客服来源版本"
                          className="block w-full rounded border p-2"
                          value={consumerSource}
                          onChange={(event) => setConsumerSource(event.target.value)}
                        >
                          <option value="">请选择已发布模板</option>
                          {consumerSources.map((source) => (
                            <option key={source.app_id} value={source.app_id}>
                              {source.name} · v{source.version_no}
                            </option>
                          ))}
                        </select>
                      </label>
                      {draft.tenants.map((tenant) => (
                        <Button
                          key={tenant.id}
                          type="button"
                          variant="secondary"
                          disabled={busy || plan.state === 'expired' || !consumerSource}
                          onClick={() =>
                            void siteTasks({
                              candidate_plan: plan.id,
                              tenant_id: tenant.id,
                              source_app_id: consumerSource,
                              source_version: consumerSources.find(
                                (source) => source.app_id === consumerSource,
                              )?.version_no,
                            })
                          }
                        >
                          准备候选：{tenant.name}（{tenant.id}）
                        </Button>
                      ))}
                    </>
                  )}
                </section>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={() => void preflight(true)}
                >
                  重新核对计划
                </Button>
              </section>
            )}
            {selected && (
              <section aria-label="现场预检任务" className="mt-5 space-y-2 border-t pt-4">
                <h2 className="system-md-semibold">环境准备任务</h2>
                <p className="system-xs-regular text-text-tertiary">
                  后台自动预检并准备所选租户的候选工作区。结果未知时先核对，不重复创建；准备完成不表示已开通业务。
                </p>
                {taskError && <p role="alert">{taskError}</p>}
                <Button type="button" variant="secondary" onClick={() => void siteTasks()}>
                  刷新任务
                </Button>
                {tasks.map((item) => (
                  <article key={item.id} className="space-y-1 rounded border p-3">
                    <p className="system-xs-regular break-all">{item.id}</p>
                    {item.action === 'workspace-prepare' && <p>候选工作区 · {item.tenant_id}</p>}
                    {item.action === 'knowledge-prepare' && <p>候选知识资源 · {item.tenant_id}</p>}
                    {item.action === 'knowledge-connect' && <p>候选知识连接 · {item.tenant_id}</p>}
                    {item.action === 'operator-prepare' && <p>候选管家应用 · {item.tenant_id}</p>}
                    {item.action === 'consumer-prepare' && <p>候选消费者应用 · {item.tenant_id}</p>}
                    {item.action === 'host-verify' && (
                      <section aria-label={`主机身份验收：${item.environment}`}>
                        <p>
                          主机身份验收 · {item.environment}：{taskLabels[item.state] || item.state}
                        </p>
                        {item.result.evidence && (
                          <>
                            <p className="system-xs-regular break-all">
                              现场主机：{item.result.evidence.remote_hostname || '未采集'}
                            </p>
                            <p className="system-xs-regular">
                              服务身份：
                              {item.result.evidence.service_user_present ? '存在' : '缺失'}
                            </p>
                          </>
                        )}
                        <p>仅证明任务落在已登记主机，不代替正式网络、容量与业务验收</p>
                      </section>
                    )}
                    {item.action === 'candidate-install' && (
                      <section aria-label={`候选安装进度：${item.tenant_id}`}>
                        <p>
                          停用候选安装 · {item.tenant_id}：{taskLabels[item.state] || item.state}
                        </p>
                        {item.result.version && <p>实际版本：{item.result.version}</p>}
                        <p>安装回执不含运行登记 · 业务未验收 · 正式入口未启用</p>
                      </section>
                    )}
                    {item.action === 'candidate-install' &&
                      ['installed', 'unchanged'].includes(item.result.installation || '') &&
                      !item.cancel_requested && (
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={
                            busy ||
                            dirty ||
                            saved?.registry_changed ||
                            !plan ||
                            plan.state === 'expired'
                          }
                          onClick={() => {
                            if (
                              plan &&
                              window.confirm(
                                '登记该已安装候选的工作区与双端应用？不重启、不启用正式入口；失联后只核对。',
                              )
                            )
                              void siteTasks({
                                installation_task: item.id,
                                registration_plan: plan.id,
                              })
                          }}
                        >
                          登记已安装候选：{item.tenant_id}
                        </Button>
                      )}
                    {item.action === 'candidate-register' && (
                      <section aria-label={`候选登记进度：${item.tenant_id}`}>
                        <p>
                          候选运行登记 · {item.tenant_id}：{taskLabels[item.state] || item.state}
                        </p>
                        <p>
                          {['registered', 'unchanged'].includes(item.result.result || '')
                            ? '运行资源已登记'
                            : '运行登记尚未确认'}{' '}
                          · 业务未验收 · 正式入口未启用
                        </p>
                      </section>
                    )}
                    {item.action === 'candidate-register' &&
                      ['registered', 'unchanged'].includes(item.result.result || '') && (
                        <section aria-label={`业务验证选择：${item.tenant_id}`}>
                          <label htmlFor={`case-${item.id}`}>受控验收案例</label>
                          <Input
                            id={`case-${item.id}`}
                            value={caseRef}
                            onChange={(e) => setCaseRef(e.target.value)}
                            placeholder="填写已登记案例编号"
                          />
                          <fieldset>
                            <legend>本次验证范围</legend>
                            {Object.entries({
                              consumer: '智能客服（知识与本人订单）',
                              order: '异常订单分析',
                              offline: '离线分析',
                            }).map(([key, label]) => (
                              <label key={key}>
                                <input
                                  type="checkbox"
                                  checked={capabilities.includes(key)}
                                  onChange={(e) =>
                                    setCapabilities((values) =>
                                      e.target.checked
                                        ? [...values, key]
                                        : values.filter((value) => value !== key),
                                    )
                                  }
                                />
                                {label}
                              </label>
                            ))}
                          </fieldset>
                          <Button
                            type="button"
                            variant="secondary"
                            disabled={
                              busy ||
                              dirty ||
                              saved?.registry_changed ||
                              !plan ||
                              plan.state === 'expired' ||
                              !/^[A-Za-z0-9_-]{1,64}$/.test(caseRef) ||
                              !capabilities.length
                            }
                            onClick={() => {
                              if (
                                plan &&
                                window.confirm(
                                  '使用受控测试身份运行所选业务验证？不修改业务数据，不启用正式入口。',
                                )
                              )
                                void siteTasks({
                                  registration_task: item.id,
                                  validation_plan: plan.id,
                                  capabilities,
                                  case_ref: caseRef,
                                })
                            }}
                          >
                            验证已登记业务：{item.tenant_id}
                          </Button>
                        </section>
                      )}
                    {item.action === 'candidate-validate' && item.state === 'observed' && (
                      <Button
                        type="button"
                        variant="secondary"
                        disabled={busy || dirty || !plan || plan.state === 'expired'}
                        onClick={() => {
                          if (
                            plan &&
                            window.confirm(
                              '按只读诊断范围发布并开通，并授权本应用所选知识库对客使用？系统会重新核验三项业务、权限、网络和容量；缺项不会启用，业务准确性仍待人工确认。',
                            )
                          )
                            void siteTasks({
                              validation_task: item.id,
                              publication_plan: plan.id,
                              confirm_readonly: true,
                              confirm_customer_knowledge: true,
                              acceptance_ref: 'readonly-diagnosis',
                            })
                        }}
                      >
                        发布并开通：{item.tenant_id}
                      </Button>
                    )}
                    {item.action === 'environment-publish' && (
                      <section>
                        <p>
                          正式开通 · {item.tenant_id}：{taskLabels[item.state] || item.state}；阶段{' '}
                          {publicationPhases[String(item.result?.phase || '')] ||
                            String(item.result?.phase || '')}
                          ，准入 {item.result?.activated ? '已启用' : '未启用'}
                          ，业务准确性待人工确认
                        </p>
                        {['observed', 'reconcile', 'blocked'].includes(item.state) &&
                          item.result?.phase !== 'restored' && (
                            <Button
                              type="button"
                              variant="secondary"
                              disabled={busy}
                              onClick={() => {
                                if (
                                  window.confirm(
                                    '关闭本次租户准入，按原记录恢复路由和配置？其他租户保持，恢复推进新修订，不删除运行历史。',
                                  )
                                )
                                  void siteTasks({ restore_publication: item.id })
                              }}
                            >
                              恢复本次开通
                            </Button>
                          )}
                      </section>
                    )}
                    {item.action === 'candidate-validate' && (
                      <p>
                        业务技术验证 · {item.tenant_id}：{taskLabels[item.state] || item.state}
                        ；业务准确性待确认，正式入口未启用
                      </p>
                    )}
                    {item.action === 'candidate-prepare' && item.state === 'observed' && (
                      <Button
                        type="button"
                        variant="secondary"
                        disabled={busy || dirty || saved?.registry_changed}
                        onClick={() => {
                          if (
                            window.confirm(
                              '安装该停用候选的配置与运行接线？不会启用正式入口；在飞取消会等待安全结果。',
                            )
                          )
                            void siteTasks({ candidate_task: item.id, installation: true })
                        }}
                      >
                        安装停用候选：{item.tenant_id}
                      </Button>
                    )}
                    {item.action === 'candidate-prepare' && (
                      <section aria-label={`候选准备进度：${item.tenant_id}`}>
                        <p>
                          候选准备 · {item.tenant_id}：
                          {item.state === 'observed'
                            ? '资源已准备，尚未开通'
                            : taskLabels[item.state] || item.state}
                        </p>
                        <ul>
                          {(item.result.steps || []).map((step) => (
                            <li key={step.action}>
                              {preparationSteps[step.action] || step.action}：
                              {taskLabels[step.state] || step.state}
                              <span className="block break-all">{step.id}</span>
                              {step.error_code && <span role="alert">{step.error_code}</span>}
                            </li>
                          ))}
                        </ul>
                        <p>安装未执行 · 业务未验收 · 正式入口未启用</p>
                      </section>
                    )}
                    <p>
                      {item.action === 'workspace-prepare' && item.state === 'observed'
                        ? '候选工作区已准备'
                        : item.action === 'knowledge-prepare' && item.state === 'observed'
                          ? '候选知识资源已准备'
                          : item.action === 'knowledge-connect' && item.state === 'observed'
                            ? '候选知识连接已准备'
                            : item.action === 'operator-prepare' && item.state === 'observed'
                              ? '候选管家应用已准备（入口停用）'
                              : item.action === 'consumer-prepare' && item.state === 'observed'
                                ? '候选消费者应用已准备（入口停用）'
                                : taskLabels[item.state] || item.state}
                      {item.cancel_requested && item.state === 'running' ? ' · 等待安全取消' : ''}
                    </p>
                    {item.error_code && <p>{item.error_code}</p>}
                    {item.result.workspace_id && (
                      <p className="break-all">工作区编号：{item.result.workspace_id}</p>
                    )}
                    {item.result.knowledge_base_id && (
                      <p className="break-all">知识库编号：{item.result.knowledge_base_id}</p>
                    )}
                    {item.result.connection_id && (
                      <p className="break-all">连接编号：{item.result.connection_id}</p>
                    )}
                    {item.result.app_id && (
                      <p className="break-all">应用编号：{item.result.app_id}</p>
                    )}
                    {item.action === 'knowledge-connect' &&
                      item.state === 'observed' &&
                      !dirty &&
                      !saved?.registry_changed && (
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={busy}
                          onClick={() =>
                            void siteTasks({ connection_task: item.id, tenant_id: item.tenant_id })
                          }
                        >
                          准备候选管家应用：{item.tenant_id}
                        </Button>
                      )}
                    {item.action === 'knowledge-connect' &&
                      item.state === 'observed' &&
                      !dirty &&
                      !saved?.registry_changed && (
                        <div className="space-y-2">
                          <Button
                            type="button"
                            variant="secondary"
                            onClick={() => void siteTasks({ consumer_sources: true })}
                          >
                            读取正式消费者模板
                          </Button>
                          {consumerSources.length > 0 && (
                            <>
                              <label className="block">
                                消费者来源模板：{item.tenant_id}
                                <select
                                  className="block w-full rounded border p-2"
                                  aria-label={`消费者来源模板：${item.tenant_id}`}
                                  value={consumerSource}
                                  onChange={(event) => setConsumerSource(event.target.value)}
                                >
                                  <option value="">请选择已发布模板</option>
                                  {consumerSources.map((source) => (
                                    <option key={source.app_id} value={source.app_id}>
                                      {source.name} · v{source.version_no}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <Button
                                type="button"
                                variant="secondary"
                                disabled={busy || !consumerSource}
                                onClick={() =>
                                  void siteTasks({
                                    connection_task: item.id,
                                    tenant_id: item.tenant_id,
                                    source_app_id: consumerSource,
                                    source_version: consumerSources.find(
                                      (source) => source.app_id === consumerSource,
                                    )?.version_no,
                                  })
                                }
                              >
                                准备候选消费者应用：{item.tenant_id}
                              </Button>
                            </>
                          )}
                        </div>
                      )}
                    {item.action === 'knowledge-prepare' &&
                      item.state === 'observed' &&
                      !dirty &&
                      !saved?.registry_changed && (
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={busy}
                          onClick={() =>
                            void siteTasks({ knowledge_task: item.id, tenant_id: item.tenant_id })
                          }
                        >
                          准备候选知识连接：{item.tenant_id}
                        </Button>
                      )}
                    {item.action === 'workspace-prepare' &&
                      item.state === 'observed' &&
                      !dirty &&
                      !saved?.registry_changed && (
                        <Button
                          type="button"
                          variant="secondary"
                          disabled={busy}
                          onClick={() =>
                            void siteTasks({ workspace_task: item.id, tenant_id: item.tenant_id })
                          }
                        >
                          准备候选知识资源：{item.tenant_id}
                        </Button>
                      )}
                    {item.state === 'observed' &&
                      (!item.action || item.action === 'site-preflight') &&
                      saved &&
                      !dirty &&
                      !saved.registry_changed &&
                      saved.draft.tenants.map((tenant) => (
                        <Button
                          key={tenant.id}
                          type="button"
                          variant="secondary"
                          disabled={busy}
                          onClick={() =>
                            void siteTasks({ site_task: item.id, tenant_id: tenant.id })
                          }
                        >
                          准备候选工作区：{tenant.name}（{tenant.id}）
                        </Button>
                      ))}
                    {item.result.checks?.map((check) => (
                      <p key={check.id || check.capability} className="system-xs-regular">
                        {checkLabels[check.id || ''] ||
                          (
                            {
                              consumer: '智能客服',
                              order: '异常订单',
                              offline: '离线分析',
                            } as Record<string, string>
                          )[check.capability || ''] ||
                          check.id}
                        ：
                        {check.state === 'passed'
                          ? '通过'
                          : check.state === 'blocked'
                            ? '阻塞'
                            : '未验证'}{' '}
                        {check.error_code || ''}
                      </p>
                    ))}
                    {['queued', 'running', 'reconcile'].includes(item.state) && (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => void siteTasks({ id: item.id, cancel: true })}
                      >
                        {item.action === 'environment-publish'
                          ? '停止开通并恢复'
                          : item.action === 'candidate-validate'
                            ? '取消业务验证（等待本轮结束）'
                            : item.action === 'candidate-register'
                              ? '取消运行登记（等待安全结果）'
                              : item.action === 'candidate-install'
                                ? '取消候选安装（等待安全结果）'
                                : item.action === 'consumer-prepare'
                                  ? '取消消费者准备'
                                  : item.action === 'operator-prepare'
                                    ? '取消管家准备'
                                    : item.action === 'knowledge-connect'
                                      ? '取消知识连接准备'
                                      : item.action === 'knowledge-prepare'
                                        ? '取消知识准备'
                                        : item.action === 'workspace-prepare'
                                          ? '取消工作区准备'
                                          : '取消现场预检'}
                      </Button>
                    )}
                  </article>
                ))}
              </section>
            )}
          </form>
        </div>
      )}
    </section>
  )
}
