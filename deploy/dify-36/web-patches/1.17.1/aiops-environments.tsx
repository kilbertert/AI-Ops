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
  plan_id: string
  state: string
  cancel_requested: boolean
  error_code: string
  result: { checks?: { id: string; state: string }[] }
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
const checkLabels: Record<string, string> = {
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
  const [tasks, setTasks] = useState<SiteTask[]>([])
  const [taskError, setTaskError] = useState('')
  const taskView = useRef(0)
  const [pending, setPending] = useState<(() => void) | null>(null)
  const saved = data?.drafts.find((item) => item.environment === selected)
  const live = data?.effective.find((item) => item.id === selected)
  const dirty = draft !== null && JSON.stringify(draft) !== original

  function select(catalogue: Catalogue, id: string) {
    const candidate = catalogue.drafts.find((item) => item.environment === id)
    const current = catalogue.effective.find((item) => item.id === id)
    const value = candidate?.draft ?? { ...empty(), ...current }
    setPlan(null)
    setTasks([])
    taskView.current += 1
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
      taskView.current += 1
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
    const view = taskView.current
    try {
      const result = await manage('environment-task', payload)
      if (view !== taskView.current) return
      if ('tasks' in result) setTasks(result.tasks)
      else setTasks((current) => [result, ...current.filter((item) => item.id !== result.id)])
    } catch (e) {
      if (view !== taskView.current) return
      setTasks([])
      setTaskError(e instanceof Error ? e.message : '任务读取未完成，请刷新核对')
    }
  }
  useEffect(() => {
    if (selected) void siteTasks({ environment: selected })
  }, [selected])
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
                  保存只更新草稿。预检核对受控文件与版本；现场验证、自动安装和激活尚未开放。
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
                    : '计划已冻结；仍缺现场验证，不能执行安装或激活。'}
                </p>
                <ul className="space-y-1">
                  {plan.checks.map((check) => (
                    <li key={check.id}>
                      {checkLabels[check.id] || check.id}：
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
                  disabled={busy}
                  onClick={() => void preflight(true)}
                >
                  重新核对计划
                </Button>
              </section>
            )}
            {selected && (
              <section aria-label="现场预检任务" className="mt-5 space-y-2 border-t pt-4">
                <h2 className="system-md-semibold">现场预检任务</h2>
                <p className="system-xs-regular text-text-tertiary">
                  仅检查已安装的自有模块；不安装、不准备应用、不激活租户。结果待核对时请运行原任务的受管执行端。
                </p>
                {taskError && <p role="alert">{taskError}</p>}
                <Button type="button" variant="secondary" onClick={() => void siteTasks()}>
                  刷新任务
                </Button>
                {tasks.map((item) => (
                  <article key={item.id} className="space-y-1 rounded border p-3">
                    <p className="system-xs-regular break-all">{item.id}</p>
                    <p>
                      {taskLabels[item.state] || item.state}
                      {item.cancel_requested && item.state === 'running' ? ' · 等待安全取消' : ''}
                    </p>
                    {item.error_code && <p>{item.error_code}</p>}
                    {item.result.checks?.map((check) => (
                      <p key={check.id} className="system-xs-regular">
                        {checkLabels[check.id] || check.id}：
                        {check.state === 'passed'
                          ? '通过'
                          : check.state === 'blocked'
                            ? '阻塞'
                            : '未验证'}
                      </p>
                    ))}
                    {['queued', 'running', 'reconcile'].includes(item.state) && (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => void siteTasks({ id: item.id, cancel: true })}
                      >
                        取消现场预检
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
