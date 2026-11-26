'use client'

import type { StatusDotStatus } from '@langgenius/dify-ui/status-dot'
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
import { StatusDot } from '@langgenius/dify-ui/status-dot'
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
type OverviewCapability = {
  state: 'passed' | 'blocked' | 'pending' | 'expired' | 'unverified'
  error_code: string
  observed_at: number | null
  task_id: string
}
type OverviewApp = {
  app_id: string
  version_no: number | null
  state: 'active' | 'disabled' | 'candidate' | 'unprepared'
  source: 'runtime' | 'task_receipt' | 'none'
}
type OverviewTenant = {
  id: string
  name: string
  admission: 'enabled' | 'disabled' | 'draft_only'
  effective_enabled: boolean
  draft_enabled: boolean | null
  workspace: { id: string; state: string; source: string }
  knowledge: {
    knowledge_base_id: string
    connection_id: string
    active_grants: number
    state: string
  }
  apps: { operator: OverviewApp; consumer: OverviewApp }
  capabilities: {
    consumer: OverviewCapability
    order: OverviewCapability
    offline: OverviewCapability
  }
  accuracy_confirmed: boolean
  latest_task: {
    id: string
    action: string
    state: string
    phase: string
    error_code: string
    updated_at: number
  } | null
}
type OverviewNode =
  | {
      source: 'heartbeat'
      node_id: string
      status: 'online' | 'stale'
      observed_at: number
      age_seconds: number
      version: string
      tool_contract: string
      schemas: string[]
      tools_compatible: boolean
      available_tools: string[]
    }
  | {
      source: 'task_receipt'
      node_id: string
      status: 'observed' | 'reconcile' | 'blocked' | 'expired' | 'unobserved'
      observed_at: number | null
      version: string
      remote_hostname: string
      service_user_present: boolean | null
      installation: string
      task_id: string
      task_action: string
    }
type OverviewEnvironment = {
  id: string
  name: string
  is_control: boolean
  status: 'active' | 'disabled' | 'draft_only'
  enabled: boolean
  draft_revision: number | null
  registry_changed: boolean
  tenant_count: number
  enabled_tenant_count: number
  active_route_count: number
  nodes: OverviewNode[]
  tenants: OverviewTenant[]
  latest_task: {
    id: string
    action: string
    tenant_id: string
    state: string
    phase: string
    error_code: string
    updated_at: number
  } | null
}
type EnvironmentOverview = {
  schema: string
  control_environment: string
  control_execution_environment: string
  control_version: string
  generated_at: number
  registry_revision: number
  environments: OverviewEnvironment[]
}
type Catalogue = {
  registry_revision: number
  can_create: boolean
  effective: Effective[]
  drafts: SavedDraft[]
  overview?: EnvironmentOverview
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
  checks: { id: string; capability?: string; state: 'passed' | 'blocked' | 'pending' }[]
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
    initial_installation?: string
    result?: string
    version?: string
    evidence?: {
      remote_hostname?: string
      machine_id_sha256?: string
      service_user_present?: boolean
    }
    steps?: { id: string; action: string; state: string; error_code: string }[]
    stages?: {
      id: string
      action: string
      state: string
      error_code: string
      version?: string
      installation?: string
      checks?: { id?: string; capability?: string; state: string; error_code?: string }[]
    }[]
    ready_to_publish?: boolean
    validation_task_id?: string
    registration_task_id?: string
    installation_task_id?: string
    preparation_task_id?: string
    checks?: { id?: string; capability?: string; state: string; error_code?: string }[]
  }
}

type ViewMode = 'all' | 'onboard' | 'config' | 'observe'
type TaskFilter = 'all' | 'active' | 'lifecycle' | 'single'
type BadgeTone = 'success' | 'warning' | 'error' | 'info' | 'neutral'

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
  'site-install': '空机初装',
  'site-preflight': '现场预检',
  'workspace-prepare': '工作区',
  'knowledge-prepare': '知识资源',
  'knowledge-connect': '知识连接',
  'operator-prepare': '管家候选',
  'consumer-prepare': '客服候选',
}

const onboardingStageLabels: Record<string, string> = {
  'candidate-prepare': '资源准备',
  'candidate-install': '停用安装',
  'candidate-register': '运行登记',
  'candidate-validate': '业务技术验证',
}

const initialInstallationLabels: Record<string, string> = {
  installed: '已装',
  unchanged: '无变更（已装）',
  already_installed: '已装（原安装保留）',
  not_installed: '未安装',
  unconfirmed: '待核对',
  not_executed: '未执行',
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

const capabilityLabels: Record<string, string> = {
  consumer: '智能客服',
  order: '异常订单',
  offline: '离线分析',
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

const fieldGroups = [
  {
    title: '基础标识',
    description: '环境唯一编号、展示名称与默认业务时区（例：Asia/Shanghai、Asia/Singapore）',
    items: [
      ['id', '环境编号', 32, '如 95、41'],
      ['name', '环境名称', 128, '如 平高测试环境'],
      ['timezone', '时区', 64, 'Asia/Shanghai'],
    ],
  },
  {
    title: 'HTTPS 服务端点',
    description: '全部外部地址须使用 HTTPS，地址变更后须重新保存并预检',
    items: [
      ['company_gateway_url', '公司网关地址', 2048, 'https://api.example.com'],
      ['gateway_url', 'AI-Ops 入口地址', 2048, 'https://gateway.example.com'],
      ['cpms_base_url', 'CPMS API 地址', 2048, 'https://cpms.example.com'],
    ],
  },
  {
    title: '受管部署引用',
    description: '仅填写已登记的逻辑引用名，禁止填写明文口令或令牌',
    items: [
      ['deployment_target', '部署目标引用', 64, '如 host-95'],
      ['credential_ref', '凭据引用', 64, '如 config-95'],
    ],
  },
] as const

const environmentStatusLabels: Record<string, string> = {
  active: '已启用',
  disabled: '已停用',
  draft_only: '仅草稿',
}
const tenantAdmissionLabels: Record<string, string> = {
  enabled: '已启用',
  disabled: '已停用',
  draft_only: '仅草稿',
}
const workspaceStateLabels: Record<string, string> = {
  ready: '就绪',
  created: '已创建',
  creating: '创建中',
  pending: '准备中',
  unknown: '待核对',
  failed: '异常',
  unprepared: '未准备',
}
const appStateLabels: Record<string, string> = {
  active: '运行中',
  disabled: '已停用（路由保留）',
  candidate: '候选停用',
  unprepared: '未准备',
}
const capabilityStateLabels: Record<string, string> = {
  passed: '技术通过',
  blocked: '阻塞',
  pending: '待验证',
  expired: '回执过期',
  unverified: '未验证',
}
const nodeStatusLabels: Record<string, string> = {
  online: '在线',
  stale: '心跳超时',
  observed: '已观察',
  reconcile: '待核对',
  blocked: '受阻',
  expired: '回执过期',
  unobserved: '未观察',
}

const badgeToneStyles: Record<BadgeTone, { wrap: string; dot: StatusDotStatus }> = {
  success: {
    wrap: 'bg-components-badge-bg-green-soft text-text-success border-state-success-active',
    dot: 'success',
  },
  warning: {
    wrap: 'bg-components-badge-bg-orange-soft text-text-warning border-divider-regular',
    dot: 'warning',
  },
  error: {
    wrap: 'bg-components-badge-bg-red-soft text-text-destructive border-state-destructive-border',
    dot: 'error',
  },
  info: {
    wrap: 'bg-components-badge-bg-blue-light-soft text-text-accent border-divider-accent',
    dot: 'normal',
  },
  neutral: {
    wrap: 'bg-components-badge-bg-gray-soft text-text-tertiary border-divider-subtle',
    dot: 'disabled',
  },
}

function envTone(status: string): BadgeTone {
  if (status === 'active' || status === 'enabled') return 'success'
  if (status === 'draft_only') return 'warning'
  return 'neutral'
}

function taskTone(state: string): BadgeTone {
  if (state === 'observed') return 'success'
  if (state === 'queued' || state === 'running') return 'info'
  if (state === 'reconcile') return 'warning'
  if (state === 'blocked' || state === 'expired') return 'error'
  return 'neutral'
}

function capabilityTone(state: string): BadgeTone {
  if (state === 'passed') return 'success'
  if (state === 'blocked') return 'error'
  if (state === 'pending' || state === 'expired') return 'warning'
  return 'neutral'
}

function nodeTone(status: string): BadgeTone {
  if (status === 'online' || status === 'observed') return 'success'
  if (status === 'stale' || status === 'reconcile' || status === 'expired') return 'warning'
  if (status === 'blocked') return 'error'
  return 'neutral'
}

function appTone(state: string): BadgeTone {
  if (state === 'active') return 'success'
  if (state === 'candidate') return 'info'
  if (state === 'disabled') return 'warning'
  return 'neutral'
}

function formatTimestamp(ts: number | null | undefined): string {
  if (!ts || !Number.isFinite(ts)) return '—'
  const date = new Date(ts * 1000)
  return Number.isNaN(date.getTime())
    ? '—'
    : `${date.toISOString().replace('T', ' ').slice(0, 19)}Z`
}

function confirmAction(message: string): boolean {
  // oxlint-disable-next-line eslint/no-alert -- Explicit confirmation gate preserved for onboarding safety & browser specs.
  return window.confirm(message)
}

function summarizeNodeCell(env: OverviewEnvironment): string {
  const primary =
    env.nodes.find((node) => node.source === 'heartbeat' && node.status === 'online') ??
    env.nodes[0]
  if (!primary) return env.is_control ? '无在线心跳' : '待受管观察'
  if (primary.source === 'heartbeat') {
    const state = nodeStatusLabels[primary.status] || primary.status
    const compat = primary.tools_compatible
      ? ` · ${primary.available_tools.length} 工具`
      : ' · 工具待升级'
    return `${primary.node_id}（${state}${compat}）`
  }
  const state = nodeStatusLabels[primary.status] || primary.status
  return primary.version ? `v${primary.version} · ${state}（回执）` : `${state}（回执）`
}

function StatusPill({
  tone,
  children,
  dot = true,
  className = '',
}: {
  tone: BadgeTone
  children: React.ReactNode
  dot?: boolean
  className?: string
}) {
  const style = badgeToneStyles[tone]
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 system-xs-medium ${style.wrap} ${className}`}
    >
      {dot && <StatusDot status={style.dot} size="small" />}
      <span>{children}</span>
    </span>
  )
}

function CopyableChip({ label, value }: { label?: string; value: string }) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timerRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    }
  }, [])

  const scheduleReset = () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      setStatus('idle')
    }, 1500)
  }

  const copyFallback = (text: string): boolean => {
    try {
      const textarea = document.createElement('textarea')
      textarea.value = text
      textarea.setAttribute('readonly', '')
      textarea.style.position = 'fixed'
      textarea.style.opacity = '0'
      document.body.appendChild(textarea)
      textarea.select()
      const ok = document.execCommand('copy')
      document.body.removeChild(textarea)
      return ok
    } catch {
      return false
    }
  }

  const copy = () => {
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard
        .writeText(value)
        .then(() => {
          setStatus('copied')
          scheduleReset()
        })
        .catch(() => {
          setStatus(copyFallback(value) ? 'copied' : 'failed')
          scheduleReset()
        })
      return
    }
    setStatus(copyFallback(value) ? 'copied' : 'failed')
    scheduleReset()
  }

  const text = status === 'copied' ? '已复制' : status === 'failed' ? '复制失败' : '复制'
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md border border-divider-subtle bg-background-section-burn px-2 py-0.5 font-mono text-[11px] text-text-secondary">
      {label && <span className="font-sans text-text-tertiary">{label}</span>}
      <span className="break-all">{value}</span>
      <button
        type="button"
        onClick={copy}
        aria-label={`复制 ${value}`}
        title={status === 'idle' ? `复制 ${value}` : text}
        className="ml-0.5 shrink-0 cursor-pointer rounded px-1 font-sans text-[10px] text-text-tertiary transition-colors hover:bg-components-button-ghost-bg-hover hover:text-text-primary"
      >
        {text}
      </button>
    </span>
  )
}

function WorkflowStepper({
  hasSaved,
  dirty,
  registryChanged,
  hasValidPlan,
  hasReadyOnboard,
  hasActivated,
}: {
  hasSaved: boolean
  dirty: boolean
  registryChanged: boolean
  hasValidPlan: boolean
  hasReadyOnboard: boolean
  hasActivated: boolean
}) {
  const draftReady = hasSaved && !dirty && !registryChanged
  const planDone = hasValidPlan || hasReadyOnboard || hasActivated
  const steps = [
    {
      num: '1',
      title: '保存配置草稿',
      hint: dirty
        ? '待保存修改'
        : registryChanged
          ? '目录已变，请重存'
          : hasSaved
            ? '草稿已就绪'
            : '填写环境与租户',
      done: draftReady,
      active: !draftReady,
    },
    {
      num: '2',
      title: '配置与制品预检',
      hint: hasValidPlan ? '计划已冻结' : planDone ? '已完成校验' : '冻结校验计划',
      done: draftReady && planDone,
      active: draftReady && !planDone,
    },
    {
      num: '3',
      title: '一键开通与验证',
      hint: hasReadyOnboard ? '技术验证就绪' : '准备→安装→登记→验证',
      done: hasReadyOnboard || hasActivated,
      active: draftReady && hasValidPlan && !hasReadyOnboard && !hasActivated,
    },
    {
      num: '4',
      title: '人工确认发布',
      hint: hasActivated ? '准入已启用' : '复核后显式启用',
      done: hasActivated,
      active: draftReady && hasReadyOnboard && !hasActivated,
    },
  ]

  return (
    <ol aria-label="开通流程指引" className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
      {steps.map((step) => (
        <li
          key={step.num}
          className={`flex items-center gap-2.5 rounded-xl border px-3 py-2 transition-colors ${
            step.active
              ? 'border-state-accent-solid bg-state-accent-hover'
              : step.done
                ? 'border-state-success-active bg-state-success-hover/60'
                : 'border-divider-subtle bg-components-card-bg'
          }`}
        >
          <span
            className={`flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold ${
              step.done
                ? 'bg-state-success-solid text-text-primary-on-surface'
                : step.active
                  ? 'bg-state-accent-solid text-text-primary-on-surface'
                  : 'bg-components-badge-bg-gray-soft text-text-tertiary'
            }`}
          >
            {step.done ? '✓' : step.num}
          </span>
          <div className="min-w-0">
            <div className="truncate system-xs-semibold text-text-primary">{step.title}</div>
            <div className="truncate text-[11px] text-text-tertiary">{step.hint}</div>
          </div>
        </li>
      ))}
    </ol>
  )
}

export default function EnvironmentManagement() {
  const router = useRouter()
  const [data, setData] = useState<Catalogue | null>(null)
  const [overview, setOverview] = useState<EnvironmentOverview | null>(null)
  const [overviewBusy, setOverviewBusy] = useState(false)
  const [overviewError, setOverviewError] = useState('')
  const [overviewCollapsed, setOverviewCollapsed] = useState(false)
  const overviewViewRef = useRef(0)
  const [selected, setSelected] = useState('')
  const [draft, setDraft] = useState<Draft | null>(null)
  const [original, setOriginal] = useState('')
  const [busy, setBusy] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [plan, setPlan] = useState<Plan | null>(null)
  const [caseRef, setCaseRef] = useState('')
  const [capabilities, setCapabilities] = useState(['consumer', 'order', 'offline'])
  const [onboardCaseRef, setOnboardCaseRef] = useState('')
  const [onboardInitial, setOnboardInitial] = useState(false)
  const [tasks, setTasks] = useState<SiteTask[]>([])
  const [consumerSources, setConsumerSources] = useState<
    { app_id: string; name: string; version_no: number }[]
  >([])
  const [consumerSource, setConsumerSource] = useState('')
  const [taskError, setTaskError] = useState('')
  const taskViewRef = useRef(0)
  const [taskGeneration, setTaskGeneration] = useState(0)
  const [pending, setPending] = useState<(() => void) | null>(null)
  const [viewMode, setViewMode] = useState<ViewMode>('all')
  const [taskFilter, setTaskFilter] = useState<TaskFilter>('all')

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
    setOnboardInitial(false)
    setOnboardCaseRef('')
    setCaseRef('')
    setCapabilities(['consumer', 'order', 'offline'])
    setTasks([])
    setTaskError('')
    setTaskFilter('all')
    taskViewRef.current += 1
    setTaskGeneration((v) => v + 1)
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
    setOverviewBusy(false)
    setError('')
    const view = ++overviewViewRef.current
    try {
      const catalogue: Catalogue = await manage('environment-drafts')
      if (view !== overviewViewRef.current) return
      const ids = [
        ...catalogue.effective.map((item) => item.id),
        ...catalogue.drafts.map((item) => item.environment),
      ]
      setData(catalogue)
      setOverview(catalogue.overview ?? null)
      setOverviewError('')
      select(catalogue, ids.includes(preferred) ? preferred : ids[0] || '')
    } catch (e) {
      if (view !== overviewViewRef.current) return
      setPlan(null)
      taskViewRef.current += 1
      setTasks([])
      setData(null)
      setOverview(null)
      setOverviewError('')
      setDraft(null)
      setOriginal('')
      setNotice('')
      setError(e instanceof Error ? e.message : '读取环境目录失败')
    } finally {
      setBusy(false)
    }
  }

  async function refreshOverview() {
    if (overviewBusy) return
    setOverviewBusy(true)
    setOverviewError('')
    const view = ++overviewViewRef.current
    try {
      const result: EnvironmentOverview = await manage('environment-overview')
      if (view !== overviewViewRef.current) return
      setOverview(result)
    } catch (e) {
      if (view !== overviewViewRef.current) return
      setOverview(null)
      setOverviewError(e instanceof Error ? e.message : '读取运行状态失败，请稍后重试')
    } finally {
      if (view === overviewViewRef.current) setOverviewBusy(false)
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
      setTaskGeneration((value) => value + 1)
      if (overview !== null) void refreshOverview()
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
    setNotice('')
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
      if (!readback && viewMode === 'config') setViewMode('onboard')
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
      else {
        const isLifecycleTask = ['tenant-onboard', 'environment-publish'].includes(
          result.action || '',
        )
        const isActiveTask = ['queued', 'running', 'reconcile'].includes(result.state)
        setTaskFilter((current) => {
          if (current === 'active' && !isActiveTask) return 'all'
          if (current === 'lifecycle' && !isLifecycleTask) return 'all'
          if (current === 'single' && isLifecycleTask) return 'all'
          return current
        })
        setTasks((current) => [result, ...current.filter((item) => item.id !== result.id)])
      }
    } catch (e) {
      if (view !== taskViewRef.current) return
      const isListLoad = 'environment' in payload && Object.keys(payload).length === 1
      if (isListLoad) setTasks([])
      setTaskError(e instanceof Error ? e.message : '任务读取未完成，请刷新核对')
    }
  }

  useEffect(() => {
    if (selected) void siteTasks({ environment: selected })
  }, [selected, taskGeneration])

  const preparing = tasks.some(
    (item) =>
      [
        'tenant-onboard',
        'candidate-prepare',
        'site-install',
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
  const selectedOverview = overview?.environments.find((item) => item.id === selected) ?? null
  const activeEnvsCount = overview?.environments.filter((e) => e.status === 'active').length ?? 0
  const totalTenantsCount = overview?.environments.reduce((sum, e) => sum + e.tenant_count, 0) ?? 0
  const enabledTenantsCount =
    overview?.environments.reduce((sum, e) => sum + e.enabled_tenant_count, 0) ?? 0

  const hasValidPlan = Boolean(plan && !dirty && plan.state !== 'expired')
  const hasReadyOnboard = tasks.some(
    (item) =>
      (item.action === 'tenant-onboard' &&
        item.state === 'observed' &&
        Boolean(item.result.ready_to_publish)) ||
      (item.action === 'candidate-validate' && item.state === 'observed'),
  )
  const hasActivated = Boolean(live?.enabled && live.tenants.some((t) => t.enabled))
  const onboardCaseInvalid = Boolean(onboardCaseRef) && !/^[\w-]{1,64}$/.test(onboardCaseRef)

  const activeTasksCount = tasks.filter((item) =>
    ['queued', 'running', 'reconcile'].includes(item.state),
  ).length
  const lifecycleTasksCount = tasks.filter((item) =>
    ['tenant-onboard', 'environment-publish'].includes(item.action || ''),
  ).length
  const singleTasksCount = tasks.length - lifecycleTasksCount

  const visibleTasks = tasks.filter((item) => {
    if (taskFilter === 'active') return ['queued', 'running', 'reconcile'].includes(item.state)
    if (taskFilter === 'lifecycle')
      return ['tenant-onboard', 'environment-publish'].includes(item.action || '')
    if (taskFilter === 'single')
      return !['tenant-onboard', 'environment-publish'].includes(item.action || '')
    return true
  })

  const showPlanSection = viewMode === 'all' || viewMode === 'onboard'
  const showConfigSection = viewMode === 'all' || viewMode === 'config'
  const showObserveSection = viewMode === 'all' || viewMode === 'observe'
  const showTasksSection = viewMode === 'all' || viewMode === 'onboard'

  return (
    <section aria-label="环境与租户管理" className="h-full overflow-y-auto bg-background-body">
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

      <div className="mx-auto max-w-[1440px] space-y-5 px-6 py-5">
        {/* 顶部页面头栏 */}
        <header className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-components-panel-border bg-components-panel-bg px-6 py-4 shadow-xs">
          <div className="flex items-center gap-3.5">
            <div
              aria-hidden="true"
              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-state-accent-hover text-text-accent"
            >
              <span className="i-ri-server-line size-5" />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="system-xl-semibold text-text-primary">环境与租户</h1>
                {overview && (
                  <StatusPill tone="info" dot={false}>
                    目录 R{overview.registry_revision}
                  </StatusPill>
                )}
                {overview?.control_version && (
                  <span className="rounded-md bg-background-section-burn px-2 py-0.5 font-mono text-[11px] text-text-tertiary">
                    v{overview.control_version}
                  </span>
                )}
              </div>
              <p className="mt-0.5 system-sm-regular text-text-secondary">
                维护业务环境连接与公司租户映射，集中观测运行节点并驱动一键开通
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3">
            {overview && (
              <div className="hidden items-center gap-4 border-r border-divider-subtle pr-4 md:flex">
                <div className="text-right">
                  <div className="text-[11px] text-text-tertiary">活跃环境</div>
                  <div className="system-sm-semibold text-text-primary">
                    {activeEnvsCount} / {overview.environments.length}
                  </div>
                </div>
                <div className="text-right">
                  <div className="text-[11px] text-text-tertiary">启用租户</div>
                  <div className="system-sm-semibold text-text-primary">
                    {enabledTenantsCount} / {totalTenantsCount}
                  </div>
                </div>
              </div>
            )}
            <div className="flex flex-wrap items-center gap-2">
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
                  variant="primary"
                  disabled={busy}
                  onClick={() => {
                    navigate(() => {
                      select(data, '')
                      setViewMode((current) =>
                        current === 'observe' || current === 'onboard' ? 'config' : current,
                      )
                    })
                  }}
                >
                  新增环境
                </Button>
              )}
            </div>
          </div>
        </header>

        {/* 全局提示条 */}
        {error && (
          <div className="flex items-center gap-2.5 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-4 py-3">
            <StatusDot status="error" size="medium" />
            <p role="alert" className="system-sm-medium text-text-destructive">
              {error}
            </p>
          </div>
        )}
        {notice && (
          <div className="flex items-center gap-2.5 rounded-xl border border-state-success-active bg-state-success-hover px-4 py-3">
            <StatusDot status="success" size="medium" />
            <p role="status" className="system-sm-medium text-text-success">
              {notice}
            </p>
          </div>
        )}
        {!data && !error && (
          <div className="flex items-center gap-3 rounded-2xl border border-components-panel-border bg-components-panel-bg px-6 py-8">
            <StatusDot status="normal" size="medium" />
            <p role="status" className="system-sm-medium text-text-secondary">
              正在读取环境目录…
            </p>
          </div>
        )}
        {data && overviewError && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-4 py-3">
            <p role="alert" className="system-sm-medium text-text-destructive">
              {overviewError}
            </p>
            {!overview && (
              <Button
                type="button"
                variant="secondary"
                size="small"
                disabled={busy || overviewBusy}
                onClick={() => void refreshOverview()}
              >
                重试读取运行状态
              </Button>
            )}
          </div>
        )}

        {/* 顶部全局环境总览卡片 */}
        {data && overview && overview.environments.length > 0 && (
          <section
            aria-label="环境总览"
            className="overflow-hidden rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs"
          >
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-5 py-3.5">
              <div className="flex flex-wrap items-center gap-2.5">
                <h2 className="system-md-semibold text-text-primary">环境总览</h2>
                <span className="system-xs-regular text-text-tertiary">
                  唯一生效目录 R{overview.registry_revision}
                  {overview.control_version ? ` · 控制端版本 ${overview.control_version}` : ''}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant="ghost"
                  size="small"
                  aria-expanded={!overviewCollapsed}
                  onClick={() => setOverviewCollapsed((v) => !v)}
                >
                  {overviewCollapsed ? '展开总览' : '收起总览'}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  size="small"
                  disabled={busy || overviewBusy}
                  onClick={() => void refreshOverview()}
                >
                  刷新运行状态
                </Button>
              </div>
            </div>
            {!overviewCollapsed && (
              <div className="overflow-x-auto">
                <table className="w-full text-left system-xs-regular">
                  <thead>
                    <tr className="border-b border-divider-subtle bg-background-section-burn/70 text-text-tertiary">
                      <th className="px-5 py-2.5 font-medium">环境</th>
                      <th className="px-3 py-2.5 font-medium">准入状态</th>
                      <th className="px-3 py-2.5 font-medium">租户（启用/总数）</th>
                      <th className="px-3 py-2.5 font-medium">节点与版本</th>
                      <th className="px-5 py-2.5 font-medium">草稿版本</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-divider-subtle">
                    {overview.environments.map((env) => {
                      const isCurrent = selected === env.id
                      const activateEnv = () => {
                        if (env.id === selected || busy) return
                        navigate(() => {
                          if (rows.some((r) => r.id === env.id)) select(data, env.id)
                          else void load(env.id)
                        })
                      }
                      return (
                        <tr
                          key={env.id}
                          tabIndex={0}
                          onClick={activateEnv}
                          onKeyDown={(event) => {
                            if (event.key === 'Enter' || event.key === ' ') {
                              event.preventDefault()
                              activateEnv()
                            }
                          }}
                          className={`cursor-pointer transition-colors focus-visible:outline-2 focus-visible:outline-state-accent-solid ${
                            isCurrent
                              ? 'bg-state-accent-hover/70'
                              : 'hover:bg-components-panel-on-panel-item-bg-hover'
                          }`}
                        >
                          <td className="px-5 py-3 font-medium text-text-primary">
                            <span className="inline-flex items-center gap-2">
                              <span>
                                {env.name}（{env.id}）{env.is_control ? ' · 控制环境' : ''}
                              </span>
                            </span>
                          </td>
                          <td className="px-3 py-3">
                            <StatusPill tone={envTone(env.status)}>
                              {environmentStatusLabels[env.status] || env.status}
                            </StatusPill>
                          </td>
                          <td className="px-3 py-3 font-medium text-text-primary">
                            {env.enabled_tenant_count}/{env.tenant_count}
                          </td>
                          <td className="px-3 py-3 text-text-secondary">
                            {summarizeNodeCell(env)}
                          </td>
                          <td className="px-5 py-3">
                            <span className="inline-flex items-center gap-1.5 text-text-secondary">
                              <span>
                                {env.draft_revision != null ? `V${env.draft_revision}` : '无草稿'}
                              </span>
                              {env.registry_changed && (
                                <StatusPill tone="warning" dot={false}>
                                  （目录已变更）
                                </StatusPill>
                              )}
                            </span>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        )}

        {/* 主体双栏布局：左侧环境列表 + 右侧工作台 */}
        {data && draft && (
          <div className="grid items-start gap-5 lg:grid-cols-[16rem_minmax(0,1fr)]">
            {/* 左侧业务环境列表 */}
            <aside className="rounded-2xl border border-components-panel-border bg-components-panel-bg p-3.5 shadow-xs lg:sticky lg:top-4">
              <div className="mb-3 flex items-center justify-between px-1">
                <span className="system-xs-semibold text-text-tertiary">
                  业务环境（{rows.length}）
                </span>
                {selected && (
                  <span className="rounded bg-background-section-burn px-1.5 py-0.5 font-mono text-[11px] text-text-tertiary">
                    #{selected}
                  </span>
                )}
              </div>
              <nav aria-label="业务环境列表" className="space-y-2">
                {rows.map((row) => {
                  const envSummary = overview?.environments.find((item) => item.id === row.id)
                  const active = selected === row.id
                  const status = envSummary?.status ?? (row.enabled ? 'active' : 'disabled')
                  return (
                    <div
                      key={row.id}
                      className={`rounded-xl border p-2 transition-all ${
                        active
                          ? 'border-state-accent-solid bg-state-accent-hover shadow-xs'
                          : 'border-divider-subtle bg-components-card-bg hover:border-divider-regular'
                      }`}
                    >
                      <Button
                        variant={active ? 'secondary-accent' : 'ghost'}
                        size="medium"
                        className="w-full justify-start px-2.5 text-left font-medium"
                        disabled={busy}
                        aria-current={active ? 'true' : undefined}
                        onClick={() => {
                          navigate(() => select(data, row.id))
                        }}
                      >
                        {row.name}（{row.id}）
                      </Button>
                      {envSummary && (
                        <div className="mt-1.5 flex items-center justify-between px-2 system-xs-regular text-text-tertiary">
                          <span className="inline-flex items-center gap-1.5">
                            <StatusDot status={badgeToneStyles[envTone(status)].dot} size="small" />
                            <span>
                              {environmentStatusLabels[envSummary.status] || envSummary.status} ·{' '}
                              {envSummary.enabled_tenant_count}/{envSummary.tenant_count} 租户
                            </span>
                          </span>
                        </div>
                      )}
                    </div>
                  )
                })}
                {!rows.length && (
                  <p className="px-2 py-4 text-center system-sm-regular text-text-secondary">
                    尚未登记环境
                  </p>
                )}
              </nav>
            </aside>

            {/* 右侧环境工作台 */}
            <div className="min-w-0 space-y-5">
              {/* 环境摘要 Hero 卡 + 4步引导条 + 视图切换 */}
              <div className="space-y-4 rounded-2xl border border-components-panel-border bg-components-panel-bg p-5 shadow-xs">
                <div className="flex flex-wrap items-start justify-between gap-4">
                  <div className="space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="system-xl-semibold text-text-primary">
                        {selected ? `${draft.name || selected}（${selected}）` : '新建业务环境草稿'}
                      </h2>
                      {selectedOverview && (
                        <StatusPill tone={envTone(selectedOverview.status)}>
                          {environmentStatusLabels[selectedOverview.status] ||
                            selectedOverview.status}
                        </StatusPill>
                      )}
                      {selectedOverview?.is_control && (
                        <StatusPill tone="info" dot={false}>
                          控制环境
                        </StatusPill>
                      )}
                      {dirty && <StatusPill tone="warning">未保存修改</StatusPill>}
                    </div>
                    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 system-xs-regular text-text-secondary">
                      <p>
                        当前生效目录：
                        {live
                          ? `R${data.registry_revision} · ${live.enabled ? '允许准入' : '已停用'} · ${live.tenants.length} 个租户`
                          : '此环境尚未生效'}
                      </p>
                      <p>
                        配置草稿：{saved ? `V${saved.revision}` : '未保存'} ·{' '}
                        {plan && !dirty
                          ? plan.state === 'expired'
                            ? '计划已过期'
                            : '已有预检计划'
                          : '未预检'}
                        {dirty ? ' · 有未保存修改' : ''}
                      </p>
                    </div>
                  </div>
                </div>

                {saved?.registry_changed && (
                  <div className="flex items-center gap-2 rounded-xl border border-divider-regular bg-components-badge-bg-orange-soft px-3.5 py-2.5">
                    <StatusDot status="warning" size="small" />
                    <p className="system-xs-medium text-text-warning">
                      生效目录在草稿保存后已变化，请核对连接和租户后重新保存。
                    </p>
                  </div>
                )}

                <WorkflowStepper
                  hasSaved={Boolean(saved)}
                  dirty={dirty}
                  registryChanged={Boolean(saved?.registry_changed)}
                  hasValidPlan={hasValidPlan}
                  hasReadyOnboard={hasReadyOnboard}
                  hasActivated={hasActivated}
                />

                {/* 视图切换胶囊条 */}
                <div
                  role="group"
                  aria-label="工作区视图切换"
                  className="flex flex-wrap items-center justify-between gap-2 border-t border-divider-subtle pt-3"
                >
                  <div className="inline-flex flex-wrap gap-1 rounded-xl bg-background-section-burn p-1">
                    {(
                      [
                        ['all', '全部视图'],
                        [
                          'onboard',
                          `一键开通与任务${activeTasksCount ? ` (${activeTasksCount})` : ''}`,
                        ],
                        ['observe', '节点与租户总览'],
                        ['config', '连接与租户配置'],
                      ] as const
                    ).map(([mode, label]) => (
                      <button
                        key={mode}
                        type="button"
                        aria-pressed={viewMode === mode}
                        onClick={() => setViewMode(mode)}
                        className={`cursor-pointer rounded-lg px-3 py-1.5 system-xs-medium transition-all ${
                          viewMode === mode
                            ? 'bg-components-panel-bg text-text-primary shadow-xs'
                            : 'text-text-tertiary hover:text-text-primary'
                        }`}
                      >
                        {label}
                      </button>
                    ))}
                  </div>
                  {preparing && (
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-components-badge-bg-blue-light-soft px-2.5 py-1 system-xs-medium text-text-accent">
                      <span className="size-2 animate-pulse rounded-full bg-state-accent-solid" />
                      后台执行中 · 每 5 秒自动同步
                    </span>
                  )}
                </div>
              </div>

              {/* 一键开通专享视图下未预检时的快捷引导条（仅在配置表单隐藏时出现，避免重复按钮） */}
              {viewMode === 'onboard' && (!plan || dirty) && (
                <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-divider-accent bg-state-accent-hover/50 px-5 py-4">
                  <div className="space-y-0.5">
                    <div className="system-sm-semibold text-text-primary">
                      {dirty
                        ? '当前有未保存的配置修改'
                        : !saved
                          ? '请先保存环境配置草稿'
                          : saved.registry_changed
                            ? '生效目录已变更，请先核对并重新保存草稿'
                            : '点击预检冻结计划后即可发起一键开通'}
                    </div>
                    <div className="system-xs-regular text-text-secondary">
                      预检会核对部署目标、制品摘要与配置投影，不修改目标环境运行状态
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {dirty || !saved || saved.registry_changed ? (
                      <Button
                        type="button"
                        variant="secondary"
                        onClick={() => setViewMode('config')}
                      >
                        前往核对与保存配置
                      </Button>
                    ) : (
                      <Button
                        type="button"
                        variant="primary"
                        disabled={busy}
                        onClick={() => void preflight()}
                      >
                        配置与制品预检
                      </Button>
                    )}
                  </div>
                </div>
              )}

              {/* 开通计划与一键开通向导（预检后直接浮在最前，操作零滚动！） */}
              {plan && !dirty && showPlanSection && (
                <section
                  aria-label="开通计划"
                  className="space-y-4 rounded-2xl border-2 border-state-accent-solid/35 bg-components-panel-bg p-5 shadow-xs"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3 border-b border-divider-subtle pb-3.5">
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="system-md-semibold text-text-primary">开通计划</h2>
                        <StatusPill tone={plan.state === 'expired' ? 'error' : 'info'}>
                          {plan.state === 'expired' ? '计划已过期' : '计划已冻结'}
                        </StatusPill>
                      </div>
                      <p className="system-xs-regular break-all text-text-tertiary">
                        {plan.id} · 草稿V{plan.draft_revision} / 目录R{plan.registry_revision}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      {plan.state === 'expired' && (
                        <Button
                          type="button"
                          variant="primary"
                          size="small"
                          disabled={busy || !saved || dirty || saved.registry_changed}
                          onClick={() => void preflight(false)}
                        >
                          重新预检
                        </Button>
                      )}
                      <Button
                        type="button"
                        variant="secondary"
                        size="small"
                        disabled={busy}
                        onClick={() => void preflight(true)}
                      >
                        重新核对计划
                      </Button>
                    </div>
                  </div>

                  <p
                    role="status"
                    className={`rounded-xl border px-3.5 py-2.5 system-xs-medium ${
                      plan.state === 'expired'
                        ? 'border-state-destructive-border bg-state-destructive-hover text-text-destructive'
                        : 'border-divider-accent bg-state-accent-hover/60 text-text-secondary'
                    }`}
                  >
                    {plan.state === 'expired'
                      ? '输入或有效期已变化，请重新预检。'
                      : '计划已冻结；执行前将核对各项条件，以对应任务结果为准。'}
                  </p>

                  {plan.checks.length > 0 && (
                    <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                      {plan.checks.map((check) => {
                        const tone: BadgeTone =
                          check.state === 'passed'
                            ? 'success'
                            : check.state === 'blocked'
                              ? 'error'
                              : 'neutral'
                        const label =
                          checkLabels[check.id || ''] ||
                          capabilityLabels[check.capability || ''] ||
                          check.id
                        const stateText =
                          check.state === 'passed'
                            ? '通过'
                            : check.state === 'blocked'
                              ? '阻塞'
                              : '未验证'
                        return (
                          <li
                            key={check.id}
                            className={`flex items-center gap-2 rounded-lg border px-3 py-1.5 system-xs-regular ${badgeToneStyles[tone].wrap}`}
                          >
                            <StatusDot status={badgeToneStyles[tone].dot} size="small" />
                            <span>
                              {label}：{stateText}
                            </span>
                          </li>
                        )
                      })}
                    </ul>
                  )}

                  {(plan.config_sha256 || plan.projection_sha256) && (
                    <div className="flex flex-wrap gap-2">
                      {plan.config_sha256 && (
                        <p className="rounded-lg bg-background-section-burn px-2.5 py-1 font-mono text-[11px] break-all text-text-tertiary">
                          配置摘要：{plan.config_sha256}
                        </p>
                      )}
                      {plan.projection_sha256 && (
                        <p className="rounded-lg bg-background-section-burn px-2.5 py-1 font-mono text-[11px] break-all text-text-tertiary">
                          停用候选投影摘要：{plan.projection_sha256}
                        </p>
                      )}
                    </div>
                  )}

                  {/* 主推：一次准备与一键开通卡片 */}
                  <section
                    aria-label="一次准备候选"
                    className="space-y-4 rounded-xl border border-divider-regular bg-components-card-bg p-4"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <h3 className="system-sm-semibold text-text-primary">一次准备候选</h3>
                        <p className="mt-0.5 system-xs-regular text-text-tertiary">
                          自动串联现场预检、工作区、知识连接与双端应用；「准备候选」仅准备资源，
                          「空机初装并准备」会在空机上安装停用模块；均不启用正式入口。
                        </p>
                      </div>
                      <Button
                        type="button"
                        variant="secondary-accent"
                        size="small"
                        disabled={busy || plan.state === 'expired'}
                        onClick={() => void siteTasks({ consumer_sources: true })}
                      >
                        选择准备模板
                      </Button>
                    </div>

                    {consumerSources.length > 0 && (
                      <div className="space-y-4">
                        <label className="block space-y-1.5 system-xs-medium text-text-secondary">
                          <span>候选客服来源版本</span>
                          <select
                            aria-label="候选客服来源版本"
                            className="block h-9 w-full rounded-lg border border-divider-regular bg-components-panel-bg px-3 system-sm-regular text-text-primary focus:border-state-accent-solid focus:outline-hidden"
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

                        {/* 核心推荐：一键开通主区 */}
                        <div className="space-y-3.5 rounded-xl border border-state-accent-solid/40 bg-state-accent-hover/35 p-4">
                          <div>
                            <h4 className="system-sm-semibold text-text-primary">
                              一键开通（准备 → 安装 → 登记 → 可选技术验证）
                            </h4>
                            <p className="mt-0.5 system-xs-regular text-text-secondary">
                              由受管执行链自动串起准备、停用安装、运行登记与可选业务验证，并在输入未变时自动续签同摘要计划窗；正式启用仍需人工复核验收结果后显式确认。
                            </p>
                          </div>

                          <div className="grid gap-3 md:grid-cols-2">
                            <label className="block space-y-1 system-xs-medium text-text-secondary">
                              <span>一键开通验收案例（选填，填写后自动串联三业务技术验证）</span>
                              <Input
                                aria-label="一键开通验收案例"
                                value={onboardCaseRef}
                                onChange={(event) => setOnboardCaseRef(event.target.value)}
                                placeholder="选填已登记受控案例编号（如 case-20261125-xq）"
                              />
                              {onboardCaseInvalid && (
                                <span className="block text-[11px] text-text-destructive">
                                  案例编号仅支持字母、数字、下划线或短横线（1–64 字符）
                                </span>
                              )}
                            </label>

                            <label className="flex cursor-pointer items-start gap-2.5 rounded-xl border border-divider-subtle bg-components-panel-bg px-3.5 py-2.5 system-xs-regular text-text-secondary">
                              <input
                                type="checkbox"
                                aria-label="一键开通包含空机初装"
                                className="mt-0.5 size-4 rounded border-divider-regular"
                                checked={onboardInitial}
                                onChange={(event) => setOnboardInitial(event.target.checked)}
                              />
                              <span>
                                <span className="block font-medium text-text-primary">
                                  包含空机初装（仅首次空白目标主机勾选）
                                </span>
                                <span className="text-[11px] text-text-tertiary">
                                  已安装目标环境请保持不勾选，将直接复用并升级既有模块
                                </span>
                              </span>
                            </label>
                          </div>

                          {!consumerSource && (
                            <p className="flex items-center gap-1.5 rounded-lg bg-components-badge-bg-orange-soft px-3 py-1.5 system-xs-medium text-text-warning">
                              <StatusDot status="warning" size="small" />
                              请先在上方选择「候选客服来源版本」，再点击下方租户的「一键开通」
                            </p>
                          )}

                          <div className="grid gap-2.5 sm:grid-cols-2">
                            {draft.tenants.map((tenant) => {
                              const tenantOverview = selectedOverview?.tenants.find(
                                (t) => t.id === tenant.id,
                              )
                              return (
                                <div
                                  key={`onboard-${tenant.id}`}
                                  className="flex flex-wrap items-center justify-between gap-2.5 rounded-xl border border-divider-subtle bg-components-panel-bg px-3.5 py-3"
                                >
                                  <div className="min-w-0">
                                    <div className="flex items-center gap-2">
                                      <span className="truncate system-sm-semibold text-text-primary">
                                        {tenant.name || tenant.id}
                                      </span>
                                      {tenantOverview && (
                                        <StatusPill tone={envTone(tenantOverview.admission)}>
                                          {tenantAdmissionLabels[tenantOverview.admission] ||
                                            tenantOverview.admission}
                                        </StatusPill>
                                      )}
                                    </div>
                                    <div className="mt-0.5 font-mono text-[11px] text-text-tertiary">
                                      {tenant.id}
                                    </div>
                                  </div>
                                  <Button
                                    type="button"
                                    variant="primary"
                                    size="medium"
                                    disabled={
                                      busy ||
                                      plan.state === 'expired' ||
                                      !consumerSource ||
                                      onboardCaseInvalid
                                    }
                                    onClick={() => {
                                      const msg = onboardInitial
                                        ? '将自动执行空机初装、资源准备、停用安装与运行登记（填写案例时同步执行只读业务技术验证）。不会自动启用正式入口，确认启动一键开通？'
                                        : '将自动执行资源准备、停用安装与运行登记（填写案例时同步执行只读业务技术验证）。不会自动启用正式入口，确认启动一键开通？'
                                      if (!confirmAction(msg)) return
                                      const payload: Record<string, unknown> = {
                                        onboard_plan: plan.id,
                                        tenant_id: tenant.id,
                                        source_app_id: consumerSource,
                                        source_version: consumerSources.find(
                                          (source) => source.app_id === consumerSource,
                                        )?.version_no,
                                      }
                                      if (onboardInitial) payload.initial_installation = true
                                      if (onboardCaseRef) {
                                        payload.case_ref = onboardCaseRef
                                        payload.capabilities = ['consumer', 'order', 'offline']
                                      }
                                      void siteTasks(payload)
                                    }}
                                  >
                                    一键开通：{tenant.name}（{tenant.id}）
                                  </Button>
                                </div>
                              )
                            })}
                          </div>
                        </div>

                        {/* 低干扰高级单步准备区 */}
                        <div className="space-y-2 rounded-xl border border-dashed border-divider-regular bg-background-section-burn/50 p-3">
                          <div className="system-xs-medium text-text-tertiary">
                            分步准备（高级运维排障用，常规开通请直接使用上方「一键开通」）
                          </div>
                          <div className="flex flex-wrap gap-2">
                            {draft.tenants.map((tenant) => (
                              <div key={tenant.id} className="flex flex-wrap gap-2">
                                <Button
                                  type="button"
                                  variant="secondary"
                                  size="small"
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
                                <Button
                                  type="button"
                                  variant="secondary"
                                  size="small"
                                  disabled={busy || plan.state === 'expired' || !consumerSource}
                                  onClick={() => {
                                    if (
                                      confirmAction(
                                        '仅当目标主机为空时使用：将在固定路径完成首次安装（停用目录），占用、半安装或旧单位会被拒绝对账，不会启用正式入口。确认包含初装？',
                                      )
                                    )
                                      void siteTasks({
                                        candidate_plan: plan.id,
                                        tenant_id: tenant.id,
                                        source_app_id: consumerSource,
                                        source_version: consumerSources.find(
                                          (source) => source.app_id === consumerSource,
                                        )?.version_no,
                                        initial_installation: true,
                                      })
                                  }}
                                >
                                  空机初装并准备：{tenant.name}（{tenant.id}）
                                </Button>
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </section>

                  {/* 独立受管核验快捷条 */}
                  <div className="flex flex-wrap items-center justify-between gap-2 border-t border-divider-subtle pt-3">
                    <span className="system-xs-regular text-text-tertiary">
                      独立只读核验（不改目标配置）
                    </span>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        type="button"
                        variant="secondary"
                        size="small"
                        disabled={busy || plan.state === 'expired'}
                        onClick={() => void siteTasks({ plan_id: plan.id })}
                      >
                        请求现场预检
                      </Button>
                      <Button
                        type="button"
                        variant="secondary"
                        size="small"
                        disabled={busy || plan.state === 'expired'}
                        onClick={() =>
                          void siteTasks({ verify_task: 'managed-host-identity', plan_id: plan.id })
                        }
                      >
                        主机身份验收
                      </Button>
                    </div>
                  </div>
                </section>
              )}

              {/* 观测驾驶舱：租户与能力总览 + 运行节点与工具 */}
              {viewMode === 'observe' && (!selected || !selectedOverview) && (
                <section
                  aria-label="运行观测状态"
                  className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-dashed border-divider-regular bg-components-panel-bg px-5 py-6"
                >
                  <p className="system-sm-regular text-text-secondary">
                    {!selected
                      ? '请先保存新环境草稿后再查看节点与租户运行状态。'
                      : '当前环境暂无运行总览数据，可点击重试读取。'}
                  </p>
                  {!selected ? (
                    <Button
                      type="button"
                      variant="secondary"
                      size="small"
                      onClick={() => setViewMode('config')}
                    >
                      前往填写环境配置
                    </Button>
                  ) : (
                    <Button
                      type="button"
                      variant="secondary"
                      size="small"
                      disabled={busy || overviewBusy}
                      onClick={() => void refreshOverview()}
                    >
                      刷新运行状态
                    </Button>
                  )}
                </section>
              )}
              {selected && selectedOverview && showObserveSection && (
                <div className="space-y-5">
                  <section
                    aria-label="租户与能力总览"
                    className="overflow-hidden rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs"
                  >
                    <div className="border-b border-divider-subtle px-5 py-3.5">
                      <h2 className="system-md-semibold text-text-primary">租户与能力总览</h2>
                      <p className="mt-0.5 system-xs-regular text-text-tertiary">
                        展示已登记租户的工作区、双端应用、知识空间与三项能力技术验证结果；技术通过不等于业务准确性人工确认。
                      </p>
                    </div>
                    {selectedOverview.tenants.length === 0 ? (
                      <p className="px-5 py-6 text-center system-sm-regular text-text-secondary">
                        该环境尚未登记租户
                      </p>
                    ) : (
                      <div className="overflow-x-auto">
                        <table className="w-full text-left system-xs-regular">
                          <thead>
                            <tr className="border-b border-divider-subtle bg-background-section-burn/70 text-text-tertiary">
                              <th className="px-5 py-2.5 font-medium">租户</th>
                              <th className="px-3 py-2.5 font-medium">准入</th>
                              <th className="px-3 py-2.5 font-medium">工作区</th>
                              <th className="px-3 py-2.5 font-medium">管家 / 客服应用</th>
                              <th className="px-3 py-2.5 font-medium">知识空间</th>
                              <th className="px-5 py-2.5 font-medium">三项能力技术验证</th>
                            </tr>
                          </thead>
                          <tbody className="divide-y divide-divider-subtle">
                            {selectedOverview.tenants.map((tenant) => (
                              <tr key={tenant.id} className="align-top">
                                <td className="px-5 py-3.5">
                                  <div className="system-sm-semibold text-text-primary">
                                    {tenant.name}
                                  </div>
                                  <div className="mt-0.5 font-mono text-[11px] text-text-tertiary">
                                    {tenant.id}
                                  </div>
                                </td>
                                <td className="px-3 py-3.5">
                                  <StatusPill tone={envTone(tenant.admission)}>
                                    {tenantAdmissionLabels[tenant.admission] || tenant.admission}
                                  </StatusPill>
                                </td>
                                <td className="px-3 py-3.5">
                                  <div className="font-medium text-text-primary">
                                    {workspaceStateLabels[tenant.workspace.state] ||
                                      tenant.workspace.state}
                                  </div>
                                  {tenant.workspace.id && (
                                    <div className="mt-1">
                                      <CopyableChip value={tenant.workspace.id} />
                                    </div>
                                  )}
                                </td>
                                <td className="space-y-1.5 px-3 py-3.5">
                                  <div className="flex items-center gap-1.5">
                                    <StatusDot
                                      status={
                                        badgeToneStyles[appTone(tenant.apps.operator.state)].dot
                                      }
                                      size="small"
                                    />
                                    <span className="text-text-secondary">
                                      管家：
                                      {appStateLabels[tenant.apps.operator.state] ||
                                        tenant.apps.operator.state}
                                      {tenant.apps.operator.version_no != null
                                        ? ` · v${tenant.apps.operator.version_no}`
                                        : ''}
                                      {tenant.apps.operator.app_id
                                        ? `（${tenant.apps.operator.app_id.slice(0, 8)}）`
                                        : ''}
                                    </span>
                                  </div>
                                  <div className="flex items-center gap-1.5">
                                    <StatusDot
                                      status={
                                        badgeToneStyles[appTone(tenant.apps.consumer.state)].dot
                                      }
                                      size="small"
                                    />
                                    <span className="text-text-secondary">
                                      客服：
                                      {appStateLabels[tenant.apps.consumer.state] ||
                                        tenant.apps.consumer.state}
                                      {tenant.apps.consumer.version_no != null
                                        ? ` · v${tenant.apps.consumer.version_no}`
                                        : ''}
                                      {tenant.apps.consumer.app_id
                                        ? `（${tenant.apps.consumer.app_id.slice(0, 8)}）`
                                        : ''}
                                    </span>
                                  </div>
                                </td>
                                <td className="px-3 py-3.5">
                                  {tenant.knowledge.active_grants > 0 ? (
                                    <StatusPill tone="success" dot={false}>
                                      活跃授权 {tenant.knowledge.active_grants}
                                    </StatusPill>
                                  ) : tenant.knowledge.knowledge_base_id ? (
                                    <StatusPill tone="info" dot={false}>
                                      候选库 {tenant.knowledge.knowledge_base_id.slice(0, 8)}
                                    </StatusPill>
                                  ) : (
                                    <span className="text-text-tertiary">未准备</span>
                                  )}
                                </td>
                                <td className="space-y-1.5 px-5 py-3.5">
                                  <div className="flex flex-wrap gap-1.5">
                                    {(
                                      [
                                        ['智能客服', tenant.capabilities.consumer],
                                        ['异常订单', tenant.capabilities.order],
                                        ['离线分析', tenant.capabilities.offline],
                                      ] as const
                                    ).map(([label, cap]) => (
                                      <StatusPill key={label} tone={capabilityTone(cap.state)}>
                                        {label}：{capabilityStateLabels[cap.state] || cap.state}
                                        {cap.error_code ? `（${cap.error_code}）` : ''}
                                      </StatusPill>
                                    ))}
                                  </div>
                                  <div className="text-[11px] text-text-tertiary">
                                    业务准确性：待人工确认
                                  </div>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </section>

                  <section
                    aria-label="运行节点与工具"
                    className="rounded-2xl border border-components-panel-border bg-components-panel-bg p-5 shadow-xs"
                  >
                    <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <h2 className="system-md-semibold text-text-primary">运行节点与工具</h2>
                        <p className="mt-0.5 system-xs-regular text-text-tertiary">
                          {selectedOverview.is_control
                            ? '控制环境展示执行节点实时心跳与工具目录兼容状态。'
                            : '远端环境展示受管任务点时回执（非实时心跳），以受控安装与观察证据为准。'}
                        </p>
                      </div>
                      <Button
                        type="button"
                        variant="secondary"
                        size="small"
                        disabled={busy || overviewBusy}
                        onClick={() => void refreshOverview()}
                      >
                        刷新节点状态
                      </Button>
                    </div>
                    {selectedOverview.nodes.length === 0 ? (
                      <p className="rounded-xl border border-dashed border-divider-regular py-5 text-center system-sm-regular text-text-secondary">
                        {selectedOverview.is_control
                          ? '暂无在线节点心跳记录'
                          : '暂无受管任务节点回执，请先执行主机身份验收或现场预检'}
                      </p>
                    ) : (
                      <div className="grid gap-3 md:grid-cols-2">
                        {selectedOverview.nodes.map((node) => (
                          <div
                            key={`${node.source}-${node.node_id}`}
                            className="space-y-2.5 rounded-xl border border-divider-subtle bg-components-card-bg p-4 system-xs-regular"
                          >
                            {node.source === 'heartbeat' ? (
                              <>
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <p className="system-sm-semibold text-text-primary">
                                    节点 {node.node_id} ·{' '}
                                    {nodeStatusLabels[node.status] || node.status}
                                    （实时心跳）
                                  </p>
                                  <StatusPill tone={nodeTone(node.status)}>
                                    {nodeStatusLabels[node.status] || node.status}
                                  </StatusPill>
                                </div>
                                <p className="text-text-secondary">
                                  网关版本：{node.version || '未知'} · 最近心跳：
                                  {formatTimestamp(node.observed_at)} · 工具协议：
                                  {node.tool_contract || '未上报'}
                                </p>
                                <p className="text-text-secondary">
                                  工具目录：
                                  {node.tools_compatible
                                    ? `与网关一致（可用工具 ${node.available_tools.length} 项）`
                                    : '与网关版本不兼容（暂不列出可用工具）'}
                                </p>
                                {node.available_tools.length > 0 && (
                                  <p className="rounded-lg bg-background-section-burn px-3 py-2 font-mono text-[11px] leading-relaxed break-all text-text-tertiary">
                                    可用工具：{node.available_tools.join('、')}
                                  </p>
                                )}
                              </>
                            ) : (
                              <>
                                <div className="flex flex-wrap items-center justify-between gap-2">
                                  <p className="system-sm-semibold text-text-primary">
                                    远端节点 {node.node_id} ·{' '}
                                    {nodeStatusLabels[node.status] || node.status}
                                    （受管任务点时回执，非实时心跳）
                                  </p>
                                  <StatusPill tone={nodeTone(node.status)}>
                                    {nodeStatusLabels[node.status] || node.status}
                                  </StatusPill>
                                </div>
                                <p className="text-text-secondary">
                                  观测版本：{node.version || '未上报'} · 最近观测：
                                  {formatTimestamp(node.observed_at)} · 主机名：
                                  {node.remote_hostname || '未采集'} · 服务身份：
                                  {node.service_user_present === true
                                    ? '已确认'
                                    : node.service_user_present === false
                                      ? '缺失'
                                      : '未采集'}
                                </p>
                                {node.installation && (
                                  <p className="text-text-tertiary">
                                    最近安装回执：
                                    {initialInstallationLabels[node.installation] ||
                                      node.installation}
                                  </p>
                                )}
                              </>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </section>
                </div>
              )}

              {/* 连接配置与租户草稿编辑器 */}
              {showConfigSection && (
                <form
                  onSubmit={(event) => {
                    event.preventDefault()
                    void save()
                  }}
                >
                  <fieldset
                    disabled={busy || (!selected && !data.can_create)}
                    className="space-y-5 rounded-2xl border border-components-panel-border bg-components-panel-bg p-5 shadow-xs"
                  >
                    <legend className="sr-only">连接配置</legend>
                    <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle pb-3">
                      <div>
                        <h2 className="system-md-semibold text-text-primary">连接配置</h2>
                        <p className="mt-0.5 system-xs-regular text-text-tertiary">
                          地址使用
                          HTTPS；凭据填写已登记引用，不填写口令或令牌。时区示例：Asia/Shanghai、Asia/Singapore。
                        </p>
                      </div>
                    </div>

                    <div className="space-y-4">
                      {fieldGroups.map((group) => (
                        <div
                          key={group.title}
                          className="space-y-3 rounded-xl border border-divider-subtle bg-components-card-bg p-4"
                        >
                          <div>
                            <div className="system-xs-semibold text-text-primary">
                              {group.title}
                            </div>
                            <div className="text-[11px] text-text-tertiary">
                              {group.description}
                            </div>
                          </div>
                          <div className="grid gap-3.5 sm:grid-cols-2 lg:grid-cols-3">
                            {group.items.map(([key, label, maxLength, placeholder]) => (
                              <label
                                key={key}
                                className="flex min-w-0 flex-col gap-1.5 system-xs-medium text-text-secondary"
                              >
                                <span>{label}</span>
                                <Input
                                  aria-label={label}
                                  value={draft[key]}
                                  placeholder={placeholder}
                                  required={key !== 'company_gateway_url'}
                                  maxLength={maxLength}
                                  disabled={key === 'id' && Boolean(selected)}
                                  onChange={(event) =>
                                    setDraft({ ...draft, [key]: event.target.value })
                                  }
                                />
                              </label>
                            ))}
                          </div>
                        </div>
                      ))}
                    </div>

                    <label className="flex cursor-pointer items-center gap-2.5 rounded-xl border border-divider-subtle bg-components-card-bg px-4 py-3 system-sm-regular text-text-primary">
                      <Checkbox
                        aria-label="草稿环境准入"
                        checked={draft.enabled}
                        onCheckedChange={(value) => setDraft({ ...draft, enabled: value === true })}
                      />
                      <span>预期允许环境准入（保存后仍不生效）</span>
                    </label>

                    <section
                      aria-label="租户草稿"
                      className="space-y-3 border-t border-divider-subtle pt-4"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <h3 className="system-md-semibold text-text-primary">
                            租户（{draft.tenants.length}）
                          </h3>
                          <p className="mt-0.5 system-xs-regular text-text-tertiary">
                            租户编号按公司系统填写；登记不表示已核实租户或已开通能力。
                          </p>
                        </div>
                        <Button
                          type="button"
                          variant="secondary"
                          size="small"
                          onClick={() =>
                            setDraft({
                              ...draft,
                              tenants: [...draft.tenants, { id: '', name: '', enabled: false }],
                            })
                          }
                        >
                          新增租户
                        </Button>
                      </div>

                      {/* 顺序固定且只允许追加，不能以正在编辑的编号为key重挂输入。 */}
                      <div className="space-y-2.5">
                        {draft.tenants.map((tenant, index) => (
                          <div
                            // oxlint-disable-next-line react/no-array-index-key -- Rows only append; editable ids must not remount inputs.
                            key={index}
                            className="grid items-end gap-3 rounded-xl border border-divider-subtle bg-components-card-bg p-3.5 sm:grid-cols-[2rem_minmax(0,1fr)_minmax(0,1fr)_auto]"
                          >
                            <span className="flex size-7 items-center justify-center rounded-lg bg-background-section-burn font-mono text-xs text-text-tertiary">
                              {index + 1}
                            </span>
                            <label
                              htmlFor={`tenant-id-${index}`}
                              className="flex min-w-0 flex-col gap-1 system-xs-medium text-text-secondary"
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
                              className="flex min-w-0 flex-col gap-1 system-xs-medium text-text-secondary"
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
                            <label className="flex h-8 cursor-pointer items-center gap-2 rounded-lg px-2 system-xs-medium text-text-secondary">
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
                      </div>
                    </section>

                    <footer className="flex flex-wrap items-center justify-between gap-3 border-t border-divider-subtle pt-4">
                      <p className="max-w-xl system-xs-regular text-text-tertiary">
                        保存只更新草稿；执行对应计划并通过各项核验后，才会改变资源和准入。
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          type="button"
                          variant="secondary-accent"
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
                </form>
              )}

              {/* 开通记录与任务进度中心 */}
              {selected && showTasksSection && (
                <section
                  aria-label="现场预检任务"
                  className="space-y-4 rounded-2xl border border-components-panel-border bg-components-panel-bg p-5 shadow-xs"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3 border-b border-divider-subtle pb-3.5">
                    <div>
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="system-md-semibold text-text-primary">环境准备任务</h2>
                        <span className="rounded-full bg-background-section-burn px-2 py-0.5 system-xs-medium text-text-tertiary">
                          共 {tasks.length} 条
                        </span>
                      </div>
                      <p className="mt-0.5 system-xs-regular text-text-tertiary">
                        后台自动预检并准备所选租户的候选工作区。结果未知时先核对，不重复创建；准备完成不表示已开通业务。
                      </p>
                    </div>
                    <Button
                      type="button"
                      variant="secondary"
                      size="small"
                      onClick={() => void siteTasks()}
                    >
                      刷新任务
                    </Button>
                  </div>

                  {/* 任务筛选胶囊 */}
                  {tasks.length > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {(
                        [
                          ['all', `全部 (${tasks.length})`],
                          ['active', `进行中 (${activeTasksCount})`],
                          ['lifecycle', `一键开通与发布 (${lifecycleTasksCount})`],
                          ['single', `单步与核验 (${singleTasksCount})`],
                        ] as const
                      ).map(([key, label]) => (
                        <button
                          key={key}
                          type="button"
                          aria-pressed={taskFilter === key}
                          onClick={() => setTaskFilter(key)}
                          className={`cursor-pointer rounded-full border px-3 py-1 system-xs-medium transition-colors ${
                            taskFilter === key
                              ? 'border-state-accent-solid bg-state-accent-hover text-text-accent'
                              : 'border-divider-subtle bg-components-card-bg text-text-secondary hover:bg-components-panel-on-panel-item-bg-hover'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                  )}

                  {taskError && (
                    <div className="rounded-xl border border-state-destructive-border bg-state-destructive-hover px-3.5 py-2.5">
                      <p role="alert" className="system-xs-medium text-text-destructive">
                        {taskError}
                      </p>
                    </div>
                  )}

                  {visibleTasks.length === 0 && (
                    <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-divider-regular py-6 text-center">
                      <p className="system-sm-regular text-text-secondary">
                        {tasks.length === 0
                          ? '当前环境暂无开通与准备任务记录'
                          : '当前筛选下无任务记录'}
                      </p>
                      {tasks.length > 0 && taskFilter !== 'all' && (
                        <Button
                          type="button"
                          variant="secondary"
                          size="small"
                          onClick={() => setTaskFilter('all')}
                        >
                          查看全部任务（{tasks.length}）
                        </Button>
                      )}
                    </div>
                  )}

                  <div className="space-y-3">
                    {visibleTasks.map((item) => {
                      const isLifecycle =
                        item.action === 'tenant-onboard' || item.action === 'environment-publish'
                      const simpleStatusText =
                        item.action === 'workspace-prepare' && item.state === 'observed'
                          ? '候选工作区已准备'
                          : item.action === 'knowledge-prepare' && item.state === 'observed'
                            ? '候选知识资源已准备'
                            : item.action === 'knowledge-connect' && item.state === 'observed'
                              ? '候选知识连接已准备'
                              : item.action === 'operator-prepare' && item.state === 'observed'
                                ? '候选管家应用已准备（入口停用）'
                                : item.action === 'consumer-prepare' && item.state === 'observed'
                                  ? '候选消费者应用已准备（入口停用）'
                                  : taskLabels[item.state] || item.state

                      return (
                        <article
                          key={item.id}
                          className={`space-y-3 rounded-xl border p-4 transition-colors ${
                            item.action === 'tenant-onboard' && item.result.ready_to_publish
                              ? 'border-state-success-active bg-state-success-hover/25'
                              : item.action === 'environment-publish' && item.result.activated
                                ? 'border-state-success-active bg-components-card-bg'
                                : isLifecycle
                                  ? 'border-divider-accent bg-components-card-bg'
                                  : 'border-divider-subtle bg-components-card-bg'
                          }`}
                        >
                          {/* 任务卡顶栏：任务ID + 状态胶囊 + 取消动作 */}
                          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle pb-2.5">
                            <div className="flex flex-wrap items-center gap-2">
                              <CopyableChip value={item.id} />
                              <StatusPill tone={taskTone(item.state)}>
                                {simpleStatusText}
                                {item.cancel_requested && item.state === 'running'
                                  ? ' · 等待安全取消'
                                  : ''}
                              </StatusPill>
                              {item.error_code && (
                                <StatusPill tone="error" dot={false}>
                                  {item.error_code}
                                </StatusPill>
                              )}
                            </div>

                            {['queued', 'running', 'reconcile'].includes(item.state) && (
                              <Button
                                type="button"
                                variant="secondary"
                                size="small"
                                onClick={() => void siteTasks({ id: item.id, cancel: true })}
                              >
                                {item.action === 'tenant-onboard'
                                  ? '取消一键开通'
                                  : item.action === 'environment-publish'
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
                          </div>

                          {/* 一键开通 4 阶段可视化卡片 */}
                          {item.action === 'tenant-onboard' && (
                            <section
                              aria-label={`一键开通进度：${item.tenant_id}`}
                              className="space-y-3"
                            >
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <p className="system-sm-semibold text-text-primary">
                                  一键开通 · {item.tenant_id}：
                                  {item.state === 'observed'
                                    ? item.result.ready_to_publish
                                      ? '技术验收已就绪，待人工确认发布'
                                      : '运行登记已完成，尚未启用正式入口'
                                    : taskLabels[item.state] || item.state}
                                </p>
                                <p className="text-[11px] text-text-tertiary">
                                  正式入口未启用 · 业务准确性待人工确认
                                </p>
                              </div>

                              {(item.result.stages || []).length > 0 && (
                                <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                                  {(item.result.stages || []).map((stage) => (
                                    <li
                                      key={stage.action}
                                      className="space-y-1 rounded-lg border border-divider-subtle bg-components-panel-bg p-2.5 system-xs-regular"
                                    >
                                      <div className="flex items-center gap-1.5 font-medium text-text-primary">
                                        <StatusDot
                                          status={badgeToneStyles[taskTone(stage.state)].dot}
                                          size="small"
                                        />
                                        <span>
                                          {onboardingStageLabels[stage.action] || stage.action}：
                                          {taskLabels[stage.state] || stage.state}
                                          {stage.version ? ` · v${stage.version}` : ''}
                                        </span>
                                      </div>
                                      <span className="block font-mono text-[11px] break-all text-text-tertiary">
                                        {stage.id}
                                      </span>
                                      {stage.error_code && (
                                        <span
                                          role="alert"
                                          className="block text-[11px] text-text-destructive"
                                        >
                                          {stage.error_code}
                                        </span>
                                      )}
                                    </li>
                                  ))}
                                </ul>
                              )}

                              {item.state === 'observed' &&
                                item.result.ready_to_publish &&
                                item.result.validation_task_id &&
                                (() => {
                                  const publishPlanId =
                                    item.plan_id ||
                                    (plan && plan.state !== 'expired' ? plan.id : '')
                                  const publishExpired =
                                    plan !== null &&
                                    plan.id === publishPlanId &&
                                    plan.state === 'expired'
                                  return (
                                    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-state-success-active bg-state-success-hover px-4 py-3">
                                      <span className="system-xs-medium text-text-success">
                                        四阶段技术验证已全部通过，可由人工确认后正式发布开通
                                      </span>
                                      <Button
                                        type="button"
                                        variant="primary"
                                        disabled={
                                          busy ||
                                          dirty ||
                                          saved?.registry_changed ||
                                          !publishPlanId ||
                                          publishExpired
                                        }
                                        onClick={() => {
                                          if (
                                            publishPlanId &&
                                            confirmAction(
                                              '按只读诊断范围发布并开通，并授权本应用所选知识库对客使用？系统会重新核验三项业务、权限、网络和容量；缺项不会启用，业务准确性仍待人工确认。',
                                            )
                                          )
                                            void siteTasks({
                                              validation_task: item.result.validation_task_id,
                                              publication_plan: publishPlanId,
                                              confirm_readonly: true,
                                              confirm_customer_knowledge: true,
                                              acceptance_ref: 'readonly-diagnosis',
                                            })
                                        }}
                                      >
                                        确认发布并开通：{item.tenant_id}
                                      </Button>
                                    </div>
                                  )
                                })()}
                            </section>
                          )}

                          {/* 正式发布与恢复卡片 */}
                          {item.action === 'environment-publish' && (
                            <section className="flex flex-wrap items-center justify-between gap-3">
                              <div className="space-y-1">
                                <p className="system-sm-semibold text-text-primary">
                                  正式开通 · {item.tenant_id}：
                                  {taskLabels[item.state] || item.state}
                                  ；阶段{' '}
                                  {publicationPhases[String(item.result?.phase || '')] ||
                                    String(item.result?.phase || '')}
                                  ，准入 {item.result?.activated ? '已启用' : '未启用'}
                                  ，业务准确性待人工确认
                                </p>
                              </div>
                              {['observed', 'reconcile', 'blocked'].includes(item.state) &&
                                item.result?.phase !== 'restored' && (
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    tone="destructive"
                                    disabled={busy}
                                    onClick={() => {
                                      if (
                                        confirmAction(
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

                          {/* 单步任务标题区 */}
                          {item.action === 'workspace-prepare' && (
                            <p className="system-sm-semibold text-text-primary">
                              候选工作区 · {item.tenant_id}
                            </p>
                          )}
                          {item.action === 'knowledge-prepare' && (
                            <p className="system-sm-semibold text-text-primary">
                              候选知识资源 · {item.tenant_id}
                            </p>
                          )}
                          {item.action === 'knowledge-connect' && (
                            <p className="system-sm-semibold text-text-primary">
                              候选知识连接 · {item.tenant_id}
                            </p>
                          )}
                          {item.action === 'operator-prepare' && (
                            <p className="system-sm-semibold text-text-primary">
                              候选管家应用 · {item.tenant_id}
                            </p>
                          )}
                          {item.action === 'consumer-prepare' && (
                            <p className="system-sm-semibold text-text-primary">
                              候选消费者应用 · {item.tenant_id}
                            </p>
                          )}

                          {item.action === 'host-verify' && (
                            <section
                              aria-label={`主机身份验收：${item.environment}`}
                              className="space-y-1.5"
                            >
                              <p className="system-sm-semibold text-text-primary">
                                主机身份验收 · {item.environment}：
                                {taskLabels[item.state] || item.state}
                              </p>
                              {item.result.evidence && (
                                <div className="flex flex-wrap gap-3 system-xs-regular text-text-secondary">
                                  <p className="break-all">
                                    现场主机：{item.result.evidence.remote_hostname || '未采集'}
                                  </p>
                                  <p>
                                    服务身份：
                                    {item.result.evidence.service_user_present ? '存在' : '缺失'}
                                  </p>
                                </div>
                              )}
                              <p className="system-xs-regular text-text-tertiary">
                                仅证明任务落在已登记主机，不代替正式网络、容量与业务验收
                              </p>
                            </section>
                          )}

                          {item.action === 'site-install' && (
                            <section
                              aria-label={`空机初装进度：${item.tenant_id}`}
                              className="space-y-1"
                            >
                              <p className="system-sm-semibold text-text-primary">
                                空机初装 · {item.tenant_id}：
                                {item.state === 'observed'
                                  ? item.result.installation === 'already_installed'
                                    ? '已装（原安装保留）'
                                    : '已装'
                                  : item.state === 'queued'
                                    ? '待安装'
                                    : taskLabels[item.state] || item.state}
                              </p>
                              {item.result.version && (
                                <p className="system-xs-regular text-text-secondary">
                                  实际版本：{item.result.version}
                                </p>
                              )}
                              <p className="system-xs-regular text-text-tertiary">
                                仅停用目录与公司连接 · 业务未验收 · 正式入口未启用
                              </p>
                            </section>
                          )}

                          {item.action === 'candidate-install' && (
                            <section
                              aria-label={`候选安装进度：${item.tenant_id}`}
                              className="flex flex-wrap items-center justify-between gap-3"
                            >
                              <div className="space-y-1">
                                <p className="system-sm-semibold text-text-primary">
                                  停用候选安装 · {item.tenant_id}：
                                  {taskLabels[item.state] || item.state}
                                </p>
                                {item.result.version && (
                                  <p className="system-xs-regular text-text-secondary">
                                    实际版本：{item.result.version}
                                  </p>
                                )}
                                <p className="system-xs-regular text-text-tertiary">
                                  安装回执不含运行登记 · 业务未验收 · 正式入口未启用
                                </p>
                              </div>
                              {['installed', 'unchanged'].includes(
                                item.result.installation || '',
                              ) &&
                                !item.cancel_requested && (
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    size="small"
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
                                        confirmAction(
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
                            </section>
                          )}

                          {item.action === 'candidate-register' && (
                            <div className="space-y-3">
                              <section
                                aria-label={`候选登记进度：${item.tenant_id}`}
                                className="space-y-1"
                              >
                                <p className="system-sm-semibold text-text-primary">
                                  候选运行登记 · {item.tenant_id}：
                                  {taskLabels[item.state] || item.state}
                                </p>
                                <p className="system-xs-regular text-text-tertiary">
                                  {['registered', 'unchanged'].includes(item.result.result || '')
                                    ? '运行资源已登记'
                                    : '运行登记尚未确认'}{' '}
                                  · 业务未验收 · 正式入口未启用
                                </p>
                              </section>
                              {['registered', 'unchanged'].includes(item.result.result || '') && (
                                <section
                                  aria-label={`业务验证选择：${item.tenant_id}`}
                                  className="space-y-3 rounded-xl border border-divider-subtle bg-components-panel-bg p-3.5"
                                >
                                  <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
                                    <div className="space-y-1">
                                      <label
                                        htmlFor={`case-${item.id}`}
                                        className="block system-xs-medium text-text-secondary"
                                      >
                                        受控验收案例
                                      </label>
                                      <Input
                                        id={`case-${item.id}`}
                                        value={caseRef}
                                        onChange={(e) => setCaseRef(e.target.value)}
                                        placeholder="填写已登记案例编号"
                                      />
                                    </div>
                                    <Button
                                      type="button"
                                      variant="secondary-accent"
                                      disabled={
                                        busy ||
                                        dirty ||
                                        saved?.registry_changed ||
                                        !plan ||
                                        plan.state === 'expired' ||
                                        !/^[\w-]{1,64}$/.test(caseRef) ||
                                        !capabilities.length
                                      }
                                      onClick={() => {
                                        if (
                                          plan &&
                                          confirmAction(
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
                                  </div>
                                  <fieldset className="flex flex-wrap items-center gap-4">
                                    <legend className="mb-1.5 system-xs-medium text-text-tertiary">
                                      本次验证范围
                                    </legend>
                                    {Object.entries({
                                      consumer: '智能客服（知识与本人订单）',
                                      order: '异常订单分析',
                                      offline: '离线分析',
                                    }).map(([key, label]) => (
                                      <label
                                        key={key}
                                        className="flex cursor-pointer items-center gap-1.5 system-xs-regular text-text-secondary"
                                      >
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
                                        <span>{label}</span>
                                      </label>
                                    ))}
                                  </fieldset>
                                </section>
                              )}
                            </div>
                          )}

                          {item.action === 'candidate-validate' && (
                            <div className="flex flex-wrap items-center justify-between gap-3">
                              <p className="system-sm-semibold text-text-primary">
                                业务技术验证 · {item.tenant_id}：
                                {taskLabels[item.state] || item.state}
                                ；业务准确性待确认，正式入口未启用
                              </p>
                              {item.state === 'observed' && (
                                <Button
                                  type="button"
                                  variant="primary"
                                  size="small"
                                  disabled={busy || dirty || !plan || plan.state === 'expired'}
                                  onClick={() => {
                                    if (
                                      plan &&
                                      confirmAction(
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
                            </div>
                          )}

                          {item.action === 'candidate-prepare' && (
                            <section
                              aria-label={`候选准备进度：${item.tenant_id}`}
                              className="space-y-2.5"
                            >
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <p className="system-sm-semibold text-text-primary">
                                  候选准备 · {item.tenant_id}：
                                  {item.state === 'observed'
                                    ? '资源已准备，尚未开通'
                                    : taskLabels[item.state] || item.state}
                                </p>
                                {item.state === 'observed' && (
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    size="small"
                                    disabled={busy || dirty || saved?.registry_changed}
                                    onClick={() => {
                                      if (
                                        confirmAction(
                                          '安装该停用候选的配置与运行接线？不会启用正式入口；在飞取消会等待安全结果。',
                                        )
                                      )
                                        void siteTasks({
                                          candidate_task: item.id,
                                          installation: true,
                                        })
                                    }}
                                  >
                                    安装停用候选：{item.tenant_id}
                                  </Button>
                                )}
                              </div>
                              {(item.result.steps || []).length > 0 && (
                                <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
                                  {(item.result.steps || []).map((step) => (
                                    <li
                                      key={step.action}
                                      className="rounded-lg border border-divider-subtle bg-components-panel-bg p-2 system-xs-regular"
                                    >
                                      <div className="flex items-center gap-1.5 font-medium text-text-primary">
                                        <StatusDot
                                          status={badgeToneStyles[taskTone(step.state)].dot}
                                          size="small"
                                        />
                                        <span>
                                          {preparationSteps[step.action] || step.action}：
                                          {taskLabels[step.state] || step.state}
                                        </span>
                                      </div>
                                      <span className="block font-mono text-[11px] break-all text-text-tertiary">
                                        {step.id}
                                      </span>
                                      {step.error_code && (
                                        <span role="alert" className="text-text-destructive">
                                          {step.error_code}
                                        </span>
                                      )}
                                    </li>
                                  ))}
                                </ul>
                              )}
                              <p className="system-xs-regular text-text-tertiary">
                                {item.result.initial_installation != null
                                  ? `基础初装：${
                                      initialInstallationLabels[item.result.initial_installation] ||
                                      item.result.initial_installation
                                    } · 运行安装未执行 · 业务未验收 · 正式入口未启用`
                                  : '安装未执行 · 业务未验收 · 正式入口未启用'}
                              </p>
                            </section>
                          )}

                          {/* 产出的资源标识胶囊 */}
                          {(item.result.workspace_id ||
                            item.result.knowledge_base_id ||
                            item.result.connection_id ||
                            item.result.app_id) && (
                            <div className="flex flex-wrap gap-2">
                              {item.result.workspace_id && (
                                <p className="rounded-md bg-background-section-burn px-2 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                  工作区编号：{item.result.workspace_id}
                                </p>
                              )}
                              {item.result.knowledge_base_id && (
                                <p className="rounded-md bg-background-section-burn px-2 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                  知识库编号：{item.result.knowledge_base_id}
                                </p>
                              )}
                              {item.result.connection_id && (
                                <p className="rounded-md bg-background-section-burn px-2 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                  连接编号：{item.result.connection_id}
                                </p>
                              )}
                              {item.result.app_id && (
                                <p className="rounded-md bg-background-section-burn px-2 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                  应用编号：{item.result.app_id}
                                </p>
                              )}
                            </div>
                          )}

                          {/* 逐项核验结果胶囊网格 */}
                          {item.result.checks && item.result.checks.length > 0 && (
                            <div className="flex flex-wrap gap-1.5">
                              {item.result.checks.map((check) => {
                                const tone: BadgeTone =
                                  check.state === 'passed'
                                    ? 'success'
                                    : check.state === 'blocked'
                                      ? 'error'
                                      : 'neutral'
                                const label =
                                  checkLabels[check.id || ''] ||
                                  capabilityLabels[check.capability || ''] ||
                                  check.id
                                const stateText =
                                  check.state === 'passed'
                                    ? '通过'
                                    : check.state === 'blocked'
                                      ? '阻塞'
                                      : '未验证'
                                return (
                                  <p
                                    key={check.id || check.capability}
                                    className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 system-xs-regular ${badgeToneStyles[tone].wrap}`}
                                  >
                                    <StatusDot status={badgeToneStyles[tone].dot} size="small" />
                                    <span>
                                      {label}：{stateText}
                                      {check.error_code ? ` ${check.error_code}` : ''}
                                    </span>
                                  </p>
                                )
                              })}
                            </div>
                          )}

                          {/* 单步任务下游接续操作按钮 */}
                          {item.action === 'knowledge-connect' &&
                            item.state === 'observed' &&
                            !dirty &&
                            !saved?.registry_changed && (
                              <div className="space-y-2.5 rounded-xl border border-divider-subtle bg-components-panel-bg p-3">
                                <div className="flex flex-wrap gap-2">
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    size="small"
                                    disabled={busy}
                                    onClick={() =>
                                      void siteTasks({
                                        connection_task: item.id,
                                        tenant_id: item.tenant_id,
                                      })
                                    }
                                  >
                                    准备候选管家应用：{item.tenant_id}
                                  </Button>
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    size="small"
                                    onClick={() => void siteTasks({ consumer_sources: true })}
                                  >
                                    读取正式消费者模板
                                  </Button>
                                </div>
                                {consumerSources.length > 0 && (
                                  <div className="flex flex-wrap items-end gap-2">
                                    <label className="min-w-52 flex-1 space-y-1 system-xs-medium text-text-secondary">
                                      <span>消费者来源模板：{item.tenant_id}</span>
                                      <select
                                        className="block h-8 w-full rounded-lg border border-divider-regular bg-components-card-bg px-2.5 system-xs-regular text-text-primary"
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
                                      size="small"
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
                                  </div>
                                )}
                              </div>
                            )}

                          {item.action === 'knowledge-prepare' &&
                            item.state === 'observed' &&
                            !dirty &&
                            !saved?.registry_changed && (
                              <div>
                                <Button
                                  type="button"
                                  variant="secondary"
                                  size="small"
                                  disabled={busy}
                                  onClick={() =>
                                    void siteTasks({
                                      knowledge_task: item.id,
                                      tenant_id: item.tenant_id,
                                    })
                                  }
                                >
                                  准备候选知识连接：{item.tenant_id}
                                </Button>
                              </div>
                            )}

                          {item.action === 'workspace-prepare' &&
                            item.state === 'observed' &&
                            !dirty &&
                            !saved?.registry_changed && (
                              <div>
                                <Button
                                  type="button"
                                  variant="secondary"
                                  size="small"
                                  disabled={busy}
                                  onClick={() =>
                                    void siteTasks({
                                      workspace_task: item.id,
                                      tenant_id: item.tenant_id,
                                    })
                                  }
                                >
                                  准备候选知识资源：{item.tenant_id}
                                </Button>
                              </div>
                            )}

                          {item.state === 'observed' &&
                            (!item.action || item.action === 'site-preflight') &&
                            saved &&
                            !dirty &&
                            !saved.registry_changed && (
                              <div className="flex flex-wrap gap-2">
                                {saved.draft.tenants.map((tenant) => (
                                  <Button
                                    key={tenant.id}
                                    type="button"
                                    variant="secondary"
                                    size="small"
                                    disabled={busy}
                                    onClick={() =>
                                      void siteTasks({ site_task: item.id, tenant_id: tenant.id })
                                    }
                                  >
                                    准备候选工作区：{tenant.name}（{tenant.id}）
                                  </Button>
                                ))}
                              </div>
                            )}
                        </article>
                      )
                    })}
                  </div>
                </section>
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
