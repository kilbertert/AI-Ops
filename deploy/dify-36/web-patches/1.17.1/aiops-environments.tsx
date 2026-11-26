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

type WorkspaceTab = 'onboard' | 'config' | 'ops'
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
  queued: '等待执行',
  running: '执行中',
  reconcile: '待核对',
  observed: '已完成',
  blocked: '检查受阻',
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

const taskActionLabels: Record<string, string> = {
  ...preparationSteps,
  'tenant-onboard': '一键开通',
  'environment-publish': '正式开通',
  'candidate-prepare': '候选准备',
  'candidate-install': '停用候选安装',
  'candidate-register': '候选运行登记',
  'candidate-validate': '业务技术验证',
  'host-verify': '主机身份验收',
}

const onboardingStageLabels: Record<string, string> = {
  'candidate-prepare': '资源准备',
  'candidate-install': '停用安装',
  'candidate-register': '运行登记',
  'candidate-validate': '技术验证',
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
    description: '环境唯一编号、名称与默认时区（例：Asia/Shanghai）',
    items: [
      ['id', '环境编号', 32, '如 95、41'],
      ['name', '环境名称', 128, '如 平高测试环境'],
      ['timezone', '时区', 64, 'Asia/Shanghai'],
    ],
  },
  {
    title: 'HTTPS 服务端点',
    description: '全部外部地址须使用 HTTPS',
    items: [
      ['company_gateway_url', '公司网关地址', 2048, 'https://api.example.com'],
      ['gateway_url', 'AI-Ops 入口地址', 2048, 'https://gateway.example.com'],
      ['cpms_base_url', 'CPMS API 地址', 2048, 'https://cpms.example.com'],
    ],
  },
  {
    title: '受管部署引用',
    description: '填写已登记的引用名称，禁止填写明文口令或令牌',
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
  disabled: '未启用',
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
  disabled: '已停用',
  candidate: '候选停用',
  unprepared: '未准备',
}
const capabilityStateLabels: Record<string, string> = {
  passed: '通过',
  blocked: '阻塞',
  pending: '待验证',
  expired: '过期',
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
    wrap: 'bg-components-badge-bg-green-soft text-text-success border-state-success-active/60',
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

function isLifecycleTask(item: SiteTask): boolean {
  return ['tenant-onboard', 'environment-publish'].includes(item.action || '')
}

function isPublishReadyOnboard(item: SiteTask): boolean {
  return (
    item.action === 'tenant-onboard' &&
    item.state === 'observed' &&
    Boolean(item.result.ready_to_publish)
  )
}

// 与服务端发布闸门同口径（onboarding/publication.py 的 submit）：观察通过、未取消、
// 三项能力全部 passed。验证任务 checks 只按 capability 生成（onboarding/validation.py），
// 故三项 capability 均 passed 即等价于验证范围完整且全部检查通过。
function isFullValidationReady(item: SiteTask): boolean {
  if (item.action !== 'candidate-validate' || item.state !== 'observed' || item.cancel_requested)
    return false
  const passed = new Set(
    (item.result.checks || [])
      .filter((check) => check.state === 'passed' && check.capability)
      .map((check) => check.capability),
  )
  return ['consumer', 'order', 'offline'].every((cap) => passed.has(cap))
}

function checkDisplay(check: { id?: string; capability?: string; state: string }) {
  const tone: BadgeTone =
    check.state === 'passed' ? 'success' : check.state === 'blocked' ? 'error' : 'neutral'
  const label = checkLabels[check.id || ''] || capabilityLabels[check.capability || ''] || check.id
  const stateText =
    check.state === 'passed' ? '通过' : check.state === 'blocked' ? '阻塞' : '未验证'
  return { tone, label, stateText }
}

function summarizeNodeCell(env: OverviewEnvironment): string {
  const primary =
    env.nodes.find((node) => node.source === 'heartbeat' && node.status === 'online') ??
    env.nodes[0]
  if (!primary) return env.is_control ? '无在线心跳' : '待观察'
  if (primary.source === 'heartbeat') {
    const state = nodeStatusLabels[primary.status] || primary.status
    const compat = primary.tools_compatible
      ? ` · ${primary.available_tools.length} 工具`
      : ' · 工具待升级'
    return `${primary.node_id}（${state}${compat}）`
  }
  const state = nodeStatusLabels[primary.status] || primary.status
  return primary.version ? `v${primary.version} · ${state}` : state
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
  const mountedRef = useRef(true)

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    }
  }, [])

  const scheduleReset = () => {
    if (!mountedRef.current) return
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      if (mountedRef.current) setStatus('idle')
    }, 1500)
  }

  const copyFallback = (text: string): boolean => {
    const previousFocus = document.activeElement
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
      if (previousFocus instanceof HTMLElement) previousFocus.focus()
      return ok
    } catch {
      if (previousFocus instanceof HTMLElement) previousFocus.focus()
      return false
    }
  }

  const copy = () => {
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard
        .writeText(value)
        .then(() => {
          if (!mountedRef.current) return
          setStatus('copied')
          scheduleReset()
        })
        .catch(() => {
          if (!mountedRef.current) return
          setStatus(copyFallback(value) ? 'copied' : 'failed')
          scheduleReset()
        })
      return
    }
    setStatus(copyFallback(value) ? 'copied' : 'failed')
    scheduleReset()
  }

  const text = status === 'copied' ? '已复制' : status === 'failed' ? '失败' : '复制'
  return (
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-md bg-background-section-burn px-2 py-0.5 font-mono text-[11px] text-text-secondary">
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

function CompactProgressSteps({
  draftReady,
  hasValidPlan,
  hasReadyOnboard,
  hasActivated,
}: {
  draftReady: boolean
  hasValidPlan: boolean
  hasReadyOnboard: boolean
  hasActivated: boolean
}) {
  const planDone = hasValidPlan || hasReadyOnboard || hasActivated
  const steps = [
    { num: '1', label: '配置草稿', done: draftReady, active: !draftReady },
    { num: '2', label: '预检计划', done: draftReady && planDone, active: draftReady && !planDone },
    {
      num: '3',
      label: '一键开通',
      done: draftReady && (hasReadyOnboard || hasActivated),
      active: draftReady && hasValidPlan && !hasReadyOnboard && !hasActivated,
    },
    {
      num: '4',
      label: '确认发布',
      done: draftReady && hasActivated,
      active: draftReady && hasReadyOnboard && !hasActivated,
    },
  ]
  return (
    <ol aria-label="开通流程指引" className="flex flex-wrap items-center gap-x-5 gap-y-2">
      {steps.map((step, index) => (
        <li key={step.num} className="flex items-center gap-2">
          <span
            className={`flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
              step.done
                ? 'border border-state-success-active bg-components-badge-bg-green-soft text-text-success'
                : step.active
                  ? 'bg-state-accent-solid text-text-primary-on-surface'
                  : 'bg-components-badge-bg-gray-soft text-text-tertiary'
            }`}
          >
            {step.done ? '✓' : step.num}
          </span>
          <span
            className={`system-xs-medium ${
              step.active
                ? 'text-text-accent'
                : step.done
                  ? 'text-text-primary'
                  : 'text-text-tertiary'
            }`}
          >
            {step.label}
          </span>
          {index < steps.length - 1 && (
            <span aria-hidden="true" className="ml-2 hidden text-divider-regular sm:inline">
              →
            </span>
          )}
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
  const [activeTab, setActiveTab] = useState<WorkspaceTab>('onboard')
  const [taskFilter, setTaskFilter] = useState<TaskFilter>('all')
  const [expandedTenants, setExpandedTenants] = useState<Record<string, boolean>>({})
  const [lastSubmittedTaskId, setLastSubmittedTaskId] = useState('')
  const [expiredPlanIds, setExpiredPlanIds] = useState<string[]>([])

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
    setExpandedTenants({})
    setLastSubmittedTaskId('')
    setExpiredPlanIds([])
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
      const catalogue: Catalogue = await manage('environment-drafts', {})
      if (view !== overviewViewRef.current) return
      setData(catalogue)
      setOverview(catalogue.overview ?? null)
      setOverviewError('')
      const ids = new Set([
        ...catalogue.effective.map((item) => item.id),
        ...catalogue.drafts.map((item) => item.environment),
      ])
      const next = ids.has(preferred) ? preferred : (ids.values().next().value ?? '')
      select(catalogue, next)
      if (!next && catalogue.can_create) setActiveTab('config')
    } catch (e) {
      if (view !== overviewViewRef.current) return
      taskViewRef.current += 1
      setSelected('')
      setPlan(null)
      setTasks([])
      setTaskError('')
      setConsumerSources([])
      setConsumerSource('')
      setExpandedTenants({})
      setLastSubmittedTaskId('')
      setExpiredPlanIds([])
      setData(null)
      setOverview(null)
      setOverviewError('')
      setDraft(null)
      setOriginal('')
      setNotice('')
      setError(e instanceof Error ? e.message : '读取环境目录失败')
    } finally {
      if (view === overviewViewRef.current) setBusy(false)
    }
  }

  async function refreshOverview() {
    setOverviewBusy(true)
    setOverviewError('')
    const view = ++overviewViewRef.current
    try {
      const result: EnvironmentOverview = await manage('environment-overview', {})
      if (view !== overviewViewRef.current) return
      setOverview(result)
    } catch (e) {
      if (view !== overviewViewRef.current) return
      setOverview(null)
      setOverviewError(e instanceof Error ? e.message : '读取运行总览失败')
    } finally {
      if (view === overviewViewRef.current) setOverviewBusy(false)
    }
  }

  useEffect(() => {
    void load('')
  }, [])

  useEffect(() => {
    if (!dirty) return
    const beforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
    }
    const onLinkClick = (event: MouseEvent) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0)
        return
      const target = event.target instanceof Element ? event.target.closest('a[href]') : null
      if (!(target instanceof HTMLAnchorElement) || target.target === '_blank') return
      const next = new URL(target.href, window.location.href)
      if (next.origin !== window.location.origin) return
      if (
        next.pathname === window.location.pathname &&
        next.search === window.location.search &&
        next.hash === window.location.hash
      )
        return
      event.preventDefault()
      event.stopPropagation()
      setPending(() => () => router.push(`${next.pathname}${next.search}${next.hash}`))
    }
    window.addEventListener('beforeunload', beforeUnload)
    document.addEventListener('click', onLinkClick, true)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      document.removeEventListener('click', onLinkClick, true)
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
      if (result.state === 'expired') {
        setExpiredPlanIds((prev) => (prev.includes(result.id) ? prev : [...prev, result.id]))
      }
      if (!readback && activeTab === 'config') setActiveTab('onboard')
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
      else if ('sources' in result) {
        setConsumerSources(result.sources)
        setConsumerSource((current) =>
          result.sources.some((s: { app_id: string }) => s.app_id === current) ? current : '',
        )
      } else {
        setLastSubmittedTaskId(result.id)
        const lifecycleResult = isLifecycleTask(result)
        const isActionableResult =
          ['queued', 'running', 'reconcile'].includes(result.state) ||
          isPublishReadyOnboard(result) ||
          isFullValidationReady(result)
        setTaskFilter((current) => {
          if (current === 'active' && !isActionableResult) return 'all'
          if (current === 'lifecycle' && !lifecycleResult) return 'all'
          if (current === 'single' && lifecycleResult) return 'all'
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
  const totalTenantsCount = overview?.environments.reduce((sum, e) => sum + e.tenant_count, 0) ?? 0
  const enabledTenantsCount =
    overview?.environments.reduce((sum, e) => sum + e.enabled_tenant_count, 0) ?? 0

  const tenantNameOf = (tenantId?: string) => {
    if (!tenantId) return ''
    const found =
      selectedOverview?.tenants.find((t) => t.id === tenantId)?.name ||
      draft?.tenants.find((t) => t.id === tenantId)?.name
    return found ? `${found}（${tenantId}）` : tenantId
  }

  const hasValidPlan = Boolean(plan && !dirty && plan.state !== 'expired')
  const enabledTenantIds = new Set(
    selectedOverview
      ? selectedOverview.enabled
        ? selectedOverview.tenants
            .filter((tenant) => tenant.effective_enabled)
            .map((tenant) => tenant.id)
        : []
      : live?.enabled
        ? live.tenants.filter((tenant) => tenant.enabled).map((tenant) => tenant.id)
        : [],
  )

  // 按时间倒序（最新在前）逐租户判别待发布状态：
  // - 若该租户最新核心记录已是 activated 的 environment-publish，则视为已发布，不再计入待发布；
  // - 否则仅取最新一条 ready_to_publish 的 tenant-onboard 或三能力全通的 candidate-validate，避免历史重复按钮。
  const pendingPublishOnboardIds = new Set<string>()
  const pendingPublishValidateIds = new Set<string>()
  const settledTenants = new Set<string>()
  for (const item of tasks) {
    const key = item.tenant_id || '__default__'
    if (settledTenants.has(key)) continue
    if (
      item.action === 'environment-publish' &&
      item.state === 'observed' &&
      Boolean(item.result.activated)
    ) {
      settledTenants.add(key)
      continue
    }
    if (isPublishReadyOnboard(item) && Boolean(item.result.validation_task_id)) {
      pendingPublishOnboardIds.add(item.id)
      settledTenants.add(key)
      continue
    }
    if (isFullValidationReady(item)) {
      pendingPublishValidateIds.add(item.id)
      settledTenants.add(key)
    }
  }

  const readyToPublishOnboard = tasks.find((item) => pendingPublishOnboardIds.has(item.id))
  const readyToPublishValidate = tasks.find((item) => pendingPublishValidateIds.has(item.id))
  const hasPendingPublish = pendingPublishOnboardIds.size > 0 || pendingPublishValidateIds.size > 0
  const hasReadyOnboard =
    hasPendingPublish ||
    tasks.some((item) => isPublishReadyOnboard(item) || isFullValidationReady(item))
  const isActionableTask = (item: SiteTask) =>
    ['queued', 'running', 'reconcile'].includes(item.state) ||
    pendingPublishOnboardIds.has(item.id) ||
    pendingPublishValidateIds.has(item.id) ||
    (isPublishReadyOnboard(item) && !item.result.validation_task_id)

  const hasActivated =
    !hasPendingPublish &&
    (tasks.some(
      (item) =>
        item.action === 'environment-publish' &&
        item.state === 'observed' &&
        Boolean(item.result.activated),
    ) ||
      enabledTenantIds.size > 0)
  const draftReady = Boolean(saved) && !dirty && !saved?.registry_changed
  const onboardCaseInvalid = Boolean(onboardCaseRef) && !/^[\w-]{1,64}$/.test(onboardCaseRef)

  const activeTasksCount = tasks.filter(isActionableTask).length
  const lifecycleTasksCount = tasks.filter((item) => isLifecycleTask(item)).length
  const singleTasksCount = tasks.length - lifecycleTasksCount

  // 首屏「开通与租户」保留全部进行中、全部待发布、刚刚手动提交的任务 + 最近 2 条核心开通/发布记录
  const focusedLifecycleTasks = (() => {
    const priority = tasks.filter(
      (item) =>
        ['queued', 'running', 'reconcile'].includes(item.state) ||
        pendingPublishOnboardIds.has(item.id) ||
        item.id === lastSubmittedTaskId,
    )
    const priorityIds = new Set(priority.map((item) => item.id))
    const recent = tasks
      .filter((item) => isLifecycleTask(item) && !priorityIds.has(item.id))
      .slice(0, 2)
    return [...priority, ...recent]
  })()

  const visibleOpsTasks = tasks.filter((item) => {
    if (taskFilter === 'active') return isActionableTask(item)
    if (taskFilter === 'lifecycle') return isLifecycleTask(item)
    if (taskFilter === 'single') return !isLifecycleTask(item)
    return true
  })

  const renderPublishConfirmButton = (item: SiteTask) => {
    if (
      item.action !== 'tenant-onboard' ||
      item.state !== 'observed' ||
      !item.result.ready_to_publish ||
      !item.result.validation_task_id
    )
      return null
    // 优先使用任务自身最新的续签计划 ID（防止旧页面态 plan 覆盖后台续签计划）；
    // 仅当任务原计划 ID 已被确认过期时，改用重新预检得到的有效新计划 ID。
    const itemPlanExpired = Boolean(item.plan_id) && expiredPlanIds.includes(item.plan_id)
    const publishPlanId =
      (item.plan_id && !itemPlanExpired ? item.plan_id : '') ||
      (plan && plan.state !== 'expired' ? plan.id : '')
    const publishExpired =
      (plan !== null && plan.id === publishPlanId && plan.state === 'expired') ||
      (Boolean(publishPlanId) && expiredPlanIds.includes(publishPlanId))
    return (
      <Button
        type="button"
        variant="primary"
        disabled={busy || dirty || saved?.registry_changed || !publishPlanId || publishExpired}
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
    )
  }

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

      <div className="mx-auto max-w-[1320px] space-y-5 px-6 py-5">
        {/* 极简页面顶栏：标题 + 一行核心统计 + 两个动作 */}
        <header className="flex flex-wrap items-center justify-between gap-4 border-b border-divider-subtle pb-4">
          <div className="flex flex-wrap items-center gap-3">
            <div
              aria-hidden="true"
              className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-state-accent-hover text-text-accent"
            >
              <span className="i-ri-server-line size-5" />
            </div>
            <div>
              <div className="flex flex-wrap items-center gap-2.5">
                <h1 className="system-xl-semibold text-text-primary">环境与租户</h1>
                {overview && (
                  <span className="rounded-full bg-background-section-burn px-2.5 py-0.5 system-xs-medium text-text-secondary">
                    {overview.environments.length} 个环境 · {enabledTenantsCount}/
                    {totalTenantsCount} 租户已启用 · 目录 R{overview.registry_revision}
                  </span>
                )}
              </div>
              <p className="mt-0.5 system-xs-regular text-text-tertiary">
                选择左侧环境，按引导预检并一键开通业务租户
              </p>
            </div>
          </div>

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
                    setActiveTab('config')
                  })
                }}
              >
                新增环境
              </Button>
            )}
          </div>
        </header>

        {/* 全局反馈条 */}
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

        {/* 主体双栏：极简左侧环境栏 + 干净单焦点右侧工作台 */}
        {data && draft && (
          <div className="grid items-start gap-6 lg:grid-cols-[14.5rem_minmax(0,1fr)]">
            {/* 左侧业务环境列表（无套娃卡片） */}
            <aside className="space-y-2 lg:sticky lg:top-4">
              <div className="flex items-center justify-between px-2">
                <span className="system-xs-semibold text-text-tertiary">
                  业务环境（{rows.length}）
                </span>
              </div>
              <nav aria-label="业务环境列表" className="space-y-1">
                {rows.map((row) => {
                  const envSummary = overview?.environments.find((item) => item.id === row.id)
                  const active = selected === row.id
                  const status = envSummary?.status ?? (row.enabled ? 'active' : 'disabled')
                  return (
                    <div
                      key={row.id}
                      className={`rounded-xl border transition-colors ${
                        active
                          ? 'border-state-accent-solid/50 bg-state-accent-hover/80'
                          : 'border-transparent hover:bg-components-panel-bg'
                      }`}
                    >
                      <button
                        type="button"
                        disabled={busy}
                        aria-label={`${row.name}（${row.id}）`}
                        aria-current={active ? 'true' : undefined}
                        onClick={() => {
                          navigate(() => select(data, row.id))
                        }}
                        className="w-full cursor-pointer px-3 py-2.5 text-left disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className={`truncate system-sm-semibold ${
                              active ? 'text-text-accent' : 'text-text-primary'
                            }`}
                          >
                            {row.name}（{row.id}）
                          </span>
                        </div>
                        <div className="mt-1 flex items-center gap-1.5 system-xs-regular text-text-tertiary">
                          <StatusDot status={badgeToneStyles[envTone(status)].dot} size="small" />
                          <span>
                            {environmentStatusLabels[status] || status}
                            {envSummary
                              ? ` · ${envSummary.enabled_tenant_count}/${envSummary.tenant_count} 租户`
                              : ''}
                          </span>
                        </div>
                      </button>
                    </div>
                  )
                })}
                {!rows.length && (
                  <p className="px-3 py-6 text-center system-xs-regular text-text-tertiary">
                    尚未登记环境
                  </p>
                )}
              </nav>
            </aside>

            {/* 右侧主工作台 */}
            <div className="min-w-0 space-y-5">
              {/* 工作区顶栏 + 3 个极简主 Tab */}
              <div className="rounded-2xl border border-components-panel-border bg-components-panel-bg px-6 pt-5 shadow-xs">
                <div className="flex flex-wrap items-center justify-between gap-3 pb-4">
                  <div className="space-y-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h2 className="system-xl-semibold text-text-primary">
                        {selected
                          ? `${draft.name || selected}（${selected}）`
                          : data.can_create
                            ? '新建业务环境'
                            : '暂无已分配环境'}
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
                    <p className="system-xs-regular text-text-tertiary">
                      当前生效目录：
                      {live
                        ? `R${data.registry_revision} · ${live.enabled ? '允许准入' : '已停用'} · ${live.tenants.length} 个租户`
                        : '此环境尚未生效'}
                      {' · '}
                      配置草稿：{saved ? `V${saved.revision}` : '未保存'} ·{' '}
                      {plan && !dirty
                        ? plan.state === 'expired'
                          ? '计划已过期'
                          : '已有预检计划'
                        : '未预检'}
                    </p>
                  </div>

                  {preparing && (
                    <span className="inline-flex items-center gap-1.5 rounded-full bg-components-badge-bg-blue-light-soft px-3 py-1 system-xs-medium text-text-accent">
                      <span className="size-2 animate-pulse rounded-full bg-state-accent-solid" />
                      后台执行中 · 自动同步
                    </span>
                  )}
                </div>

                {saved?.registry_changed && (
                  <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-components-badge-bg-orange-soft px-3.5 py-2.5">
                    <span className="flex items-center gap-2 system-xs-medium text-text-warning">
                      <StatusDot status="warning" size="small" />
                      生效目录已变化，请前往「连接配置」核对并重新保存草稿。
                    </span>
                    {activeTab !== 'config' && (
                      <Button
                        type="button"
                        variant="ghost"
                        size="small"
                        onClick={() => setActiveTab('config')}
                      >
                        去核对
                      </Button>
                    )}
                  </div>
                )}

                {/* 3 个极简主 Tab */}
                <div
                  role="tablist"
                  aria-label="工作区视图切换"
                  className="-mb-px flex flex-wrap gap-6 border-t border-divider-subtle pt-1"
                >
                  {(
                    [
                      [
                        'onboard',
                        '开通与租户',
                        activeTasksCount > 0 ? String(activeTasksCount) : '',
                        false,
                      ],
                      ['config', '连接配置', '', dirty || Boolean(saved?.registry_changed)],
                      ['ops', '运行与排查', tasks.length > 0 ? String(tasks.length) : '', false],
                    ] as const
                  ).map(([tab, label, badge, warnDot], idx, list) => {
                    const isCurrent = activeTab === tab
                    return (
                      <button
                        key={tab}
                        id={`aiops-tab-${tab}`}
                        type="button"
                        role="tab"
                        aria-selected={isCurrent}
                        aria-controls={`aiops-panel-${tab}`}
                        tabIndex={isCurrent ? 0 : -1}
                        onClick={() => setActiveTab(tab)}
                        onKeyDown={(event) => {
                          if (event.key === 'ArrowRight') {
                            event.preventDefault()
                            const next = list[(idx + 1) % list.length]?.[0]
                            if (next) setActiveTab(next)
                          } else if (event.key === 'ArrowLeft') {
                            event.preventDefault()
                            const prev = list[(idx - 1 + list.length) % list.length]?.[0]
                            if (prev) setActiveTab(prev)
                          }
                        }}
                        className={`relative inline-flex cursor-pointer items-center gap-2 border-b-2 py-3 system-sm-medium transition-colors ${
                          isCurrent
                            ? 'border-state-accent-solid text-text-accent'
                            : 'border-transparent text-text-tertiary hover:text-text-primary'
                        }`}
                      >
                        <span>{label}</span>
                        {badge && (
                          <span
                            className={`rounded-full px-1.5 py-0.5 text-[11px] font-medium ${
                              isCurrent
                                ? 'bg-state-accent-hover text-text-accent'
                                : 'bg-background-section-burn text-text-tertiary'
                            }`}
                          >
                            {badge}
                          </span>
                        )}
                        {warnDot && <StatusDot status="warning" size="small" />}
                      </button>
                    )
                  })}
                </div>
              </div>

              {/* ================================================================= */}
              {/* TAB 1: 开通与租户（默认首屏 —— 极简、单焦点、一眼知道怎么下手）      */}
              {/* ================================================================= */}
              {activeTab === 'onboard' && (
                <div
                  id="aiops-panel-onboard"
                  role="tabpanel"
                  aria-labelledby="aiops-tab-onboard"
                  className="space-y-5"
                >
                  {/* 1. 单一明确「下一步行动卡」 */}
                  {!plan || dirty ? (
                    <section
                      aria-label="下一步操作"
                      className={`rounded-2xl border p-6 shadow-xs ${
                        (readyToPublishOnboard || readyToPublishValidate) && draftReady
                          ? 'border-state-success-active bg-state-success-hover/35'
                          : 'border-components-panel-border bg-components-panel-bg'
                      }`}
                    >
                      <div className="mb-4 border-b border-divider-subtle pb-3.5">
                        <CompactProgressSteps
                          draftReady={draftReady}
                          hasValidPlan={hasValidPlan}
                          hasReadyOnboard={hasReadyOnboard}
                          hasActivated={hasActivated}
                        />
                      </div>

                      {!selected && !data.can_create ? (
                        <div className="space-y-1">
                          <h3 className="system-md-semibold text-text-primary">
                            当前账号暂无已分配的业务环境
                          </h3>
                          <p className="system-xs-regular text-text-secondary">
                            请联系平台目录管理员为当前账号分配环境授权后刷新页面。
                          </p>
                        </div>
                      ) : readyToPublishOnboard && draftReady ? (
                        <div className="flex flex-wrap items-center justify-between gap-4">
                          <div className="space-y-1">
                            <div className="flex items-center gap-2">
                              <StatusPill tone="success">技术验收就绪</StatusPill>
                              <h3 className="system-md-semibold text-text-primary">
                                待人工确认发布：{tenantNameOf(readyToPublishOnboard.tenant_id)}
                              </h3>
                            </div>
                            <p className="system-xs-regular text-text-secondary">
                              四阶段技术验证已通过，复核无误后点击正式开通（业务准确性仍待人工确认）。
                            </p>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            {renderPublishConfirmButton(readyToPublishOnboard)}
                            <Button
                              type="button"
                              variant="secondary"
                              disabled={busy}
                              onClick={() => void preflight()}
                            >
                              配置与制品预检
                            </Button>
                          </div>
                        </div>
                      ) : readyToPublishValidate && draftReady ? (
                        <div className="flex flex-wrap items-center justify-between gap-4">
                          <div className="space-y-1">
                            <div className="flex items-center gap-2">
                              <StatusPill tone="success">三项能力验证就绪</StatusPill>
                              <h3 className="system-md-semibold text-text-primary">
                                待人工确认发布：{tenantNameOf(readyToPublishValidate.tenant_id)}
                              </h3>
                            </div>
                            <p className="system-xs-regular text-text-secondary">
                              单步业务技术验证已全部通过，预检计划后可在「运行与排查」发起发布。
                            </p>
                          </div>
                          <div className="flex flex-wrap items-center gap-2">
                            <Button
                              type="button"
                              variant="primary"
                              disabled={busy}
                              onClick={() => void preflight()}
                            >
                              配置与制品预检
                            </Button>
                            <Button
                              type="button"
                              variant="secondary"
                              onClick={() => setActiveTab('ops')}
                            >
                              前往运行与排查
                            </Button>
                          </div>
                        </div>
                      ) : !draftReady ? (
                        <div className="flex flex-wrap items-center justify-between gap-4">
                          <div className="space-y-1">
                            <h3 className="system-md-semibold text-text-primary">
                              {dirty
                                ? '先保存未提交的配置修改'
                                : !saved
                                  ? '先保存环境配置草稿'
                                  : '生效目录已变更，请先核对配置'}
                            </h3>
                            <p className="system-xs-regular text-text-secondary">
                              保存配置草稿后，即可预检并一键开通业务租户。
                            </p>
                          </div>
                          <Button
                            type="button"
                            variant="primary"
                            onClick={() => setActiveTab('config')}
                          >
                            前往连接配置
                          </Button>
                        </div>
                      ) : (
                        <div className="flex flex-wrap items-center justify-between gap-4">
                          <div className="space-y-1">
                            <h3 className="system-md-semibold text-text-primary">
                              {hasActivated
                                ? '环境已开通，可继续扩展租户'
                                : '第一步：预检并准备开通'}
                            </h3>
                            <p className="system-xs-regular text-text-secondary">
                              只读核对部署目标与制品摘要，不修改目标环境运行状态。
                            </p>
                          </div>
                          <Button
                            type="button"
                            variant="primary"
                            disabled={busy}
                            onClick={() => void preflight()}
                          >
                            配置与制品预检
                          </Button>
                        </div>
                      )}
                    </section>
                  ) : (
                    /* 预检完成：直接呈现干净的「开通计划与一键开通」操作卡 */
                    <section
                      aria-label="开通计划"
                      className="space-y-5 rounded-2xl border-2 border-state-accent-solid/35 bg-components-panel-bg p-6 shadow-xs"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-divider-subtle pb-4">
                        <div className="flex flex-wrap items-center gap-2.5">
                          <h3 className="system-md-semibold text-text-primary">开通计划</h3>
                          <StatusPill tone={plan.state === 'expired' ? 'error' : 'info'}>
                            {plan.state === 'expired' ? '计划已过期' : '计划已冻结'}
                          </StatusPill>
                          <span className="system-xs-regular text-text-tertiary">
                            草稿 V{plan.draft_revision} / 目录 R{plan.registry_revision}
                          </span>
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
                        className={`rounded-xl px-3.5 py-2.5 system-xs-medium ${
                          plan.state === 'expired'
                            ? 'bg-state-destructive-hover text-text-destructive'
                            : 'bg-background-section-burn text-text-secondary'
                        }`}
                      >
                        {plan.state === 'expired'
                          ? '输入或有效期已变化，请重新预检。'
                          : '计划已冻结；可选择模板直接一键开通，正式启用仍需人工确认。'}
                      </p>

                      {plan.checks.length > 0 && (
                        <ul className="flex flex-wrap gap-2">
                          {plan.checks.map((check) => {
                            const { tone, label, stateText } = checkDisplay(check)
                            return (
                              <li
                                key={check.id}
                                className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 system-xs-regular ${badgeToneStyles[tone].wrap}`}
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

                      {/* 主操作：一键开通 */}
                      <section aria-label="一次准备候选" className="space-y-4 pt-1">
                        {consumerSources.length === 0 ? (
                          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-state-accent-hover/40 px-4 py-3.5">
                            <div className="space-y-0.5">
                              <div className="system-sm-semibold text-text-primary">
                                下一步：选择客服来源模板，发起一键开通
                              </div>
                              <div className="system-xs-regular text-text-secondary">
                                自动串联准备、停用安装、运行登记与可选技术验证，不自动启用入口。
                              </div>
                            </div>
                            <Button
                              type="button"
                              variant="primary"
                              disabled={busy || plan.state === 'expired'}
                              onClick={() => void siteTasks({ consumer_sources: true })}
                            >
                              选择准备模板
                            </Button>
                          </div>
                        ) : (
                          <div className="space-y-4 rounded-xl bg-background-section-burn/60 p-4">
                            <div className="flex flex-wrap items-center justify-between gap-2">
                              <h4 className="system-sm-semibold text-text-primary">一键开通配置</h4>
                              <Button
                                type="button"
                                variant="ghost"
                                size="small"
                                disabled={busy || plan.state === 'expired'}
                                onClick={() => void siteTasks({ consumer_sources: true })}
                              >
                                刷新模板
                              </Button>
                            </div>

                            <div className="grid gap-3 md:grid-cols-2">
                              <label className="block space-y-1 system-xs-medium text-text-secondary">
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

                              <label className="block space-y-1 system-xs-medium text-text-secondary">
                                <span>一键开通验收案例（选填，填写后自动三业务验证）</span>
                                <Input
                                  aria-label="一键开通验收案例"
                                  value={onboardCaseRef}
                                  onChange={(event) => setOnboardCaseRef(event.target.value)}
                                  placeholder="选填案例编号（如 case-20261125-xq）"
                                />
                                {onboardCaseInvalid && (
                                  <span className="block text-[11px] text-text-destructive">
                                    仅支持字母、数字、下划线或短横线（1–64 字符）
                                  </span>
                                )}
                              </label>
                            </div>

                            <label className="inline-flex cursor-pointer items-center gap-2 system-xs-regular text-text-secondary">
                              <input
                                type="checkbox"
                                aria-label="一键开通包含空机初装"
                                className="size-4 rounded border-divider-regular"
                                checked={onboardInitial}
                                onChange={(event) => setOnboardInitial(event.target.checked)}
                              />
                              <span>包含空机初装（仅首次空白目标主机勾选）</span>
                            </label>

                            {!consumerSource && (
                              <p className="flex items-center gap-1.5 rounded-lg bg-components-badge-bg-orange-soft px-3 py-1.5 system-xs-medium text-text-warning">
                                <StatusDot status="warning" size="small" />
                                请先选择上方「候选客服来源版本」，再点击租户「一键开通」
                              </p>
                            )}

                            <div className="divide-y divide-divider-subtle rounded-xl border border-divider-subtle bg-components-panel-bg">
                              {draft.tenants.map((tenant) => {
                                const tenantOverview = selectedOverview?.tenants.find(
                                  (t) => t.id === tenant.id,
                                )
                                return (
                                  <div
                                    key={`onboard-${tenant.id}`}
                                    className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
                                  >
                                    <div className="flex flex-wrap items-center gap-2">
                                      <span className="system-sm-semibold text-text-primary">
                                        {tenant.name || tenant.id}
                                      </span>
                                      {tenantOverview && (
                                        <StatusPill tone={envTone(tenantOverview.admission)}>
                                          {tenantAdmissionLabels[tenantOverview.admission] ||
                                            tenantOverview.admission}
                                        </StatusPill>
                                      )}
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
                        )}

                        {/* 折叠收纳：计划摘要与高级分步运维工具 */}
                        <details className="rounded-xl bg-background-section-burn/50 px-4 py-2.5">
                          <summary className="cursor-pointer system-xs-medium text-text-tertiary select-none hover:text-text-primary">
                            高级操作与计划校验摘要（分步准备 / 主机核验 / SHA）
                          </summary>
                          <div className="mt-3 space-y-3 border-t border-divider-subtle pt-3">
                            <div className="flex flex-wrap gap-2">
                              <CopyableChip key={plan.id} label="计划" value={plan.id} />
                              {plan.config_sha256 && (
                                <CopyableChip
                                  key={plan.config_sha256}
                                  label="配置摘要"
                                  value={plan.config_sha256}
                                />
                              )}
                              {plan.projection_sha256 && (
                                <CopyableChip
                                  key={plan.projection_sha256}
                                  label="停用候选投影摘要"
                                  value={plan.projection_sha256}
                                />
                              )}
                            </div>

                            {consumerSources.length > 0 && (
                              <div className="space-y-1.5">
                                <div className="system-xs-medium text-text-tertiary">
                                  分步准备（排障用）
                                </div>
                                <div className="flex flex-wrap gap-2">
                                  {draft.tenants.map((tenant) => (
                                    <div key={tenant.id} className="flex flex-wrap gap-2">
                                      <Button
                                        type="button"
                                        variant="secondary"
                                        size="small"
                                        disabled={
                                          busy || plan.state === 'expired' || !consumerSource
                                        }
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
                                        disabled={
                                          busy || plan.state === 'expired' || !consumerSource
                                        }
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
                            )}

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
                                  void siteTasks({
                                    verify_task: 'managed-host-identity',
                                    plan_id: plan.id,
                                  })
                                }
                              >
                                主机身份验收
                              </Button>
                            </div>
                          </div>
                        </details>
                      </section>
                    </section>
                  )}

                  {/* 2. 极简「租户状态」列表（默认隐藏 UUID，一键展开详情） */}
                  {selected && (
                    <section
                      aria-label="租户与能力总览"
                      className="rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-6 py-4">
                        <div className="flex items-center gap-2">
                          <h3 className="system-md-semibold text-text-primary">租户状态</h3>
                          <span className="system-xs-regular text-text-tertiary">
                            {selectedOverview
                              ? `${selectedOverview.enabled_tenant_count}/${selectedOverview.tenant_count} 已启用`
                              : `${draft.tenants.length} 个草稿租户`}
                          </span>
                        </div>
                        <Button
                          type="button"
                          variant="ghost"
                          size="small"
                          disabled={busy || overviewBusy}
                          onClick={() => void refreshOverview()}
                        >
                          刷新状态
                        </Button>
                      </div>

                      {selectedOverview && selectedOverview.tenants.length > 0 ? (
                        <ul className="divide-y divide-divider-subtle">
                          {selectedOverview.tenants.map((tenant) => {
                            const expanded = Boolean(expandedTenants[tenant.id])
                            return (
                              <li key={tenant.id} className="px-6 py-3.5">
                                <div className="flex flex-wrap items-center justify-between gap-3">
                                  <div className="flex flex-wrap items-center gap-2.5">
                                    <span className="system-sm-semibold text-text-primary">
                                      {tenant.name}
                                    </span>
                                    <StatusPill tone={envTone(tenant.admission)}>
                                      {tenantAdmissionLabels[tenant.admission] || tenant.admission}
                                    </StatusPill>
                                  </div>

                                  <div className="flex flex-wrap items-center gap-4">
                                    <div className="flex flex-wrap items-center gap-3 system-xs-regular text-text-secondary">
                                      {(
                                        [
                                          ['智能客服', tenant.capabilities.consumer],
                                          ['异常订单', tenant.capabilities.order],
                                          ['离线分析', tenant.capabilities.offline],
                                        ] as const
                                      ).map(([label, cap]) => (
                                        <span
                                          key={label}
                                          className="inline-flex items-center gap-1.5"
                                        >
                                          <StatusDot
                                            status={badgeToneStyles[capabilityTone(cap.state)].dot}
                                            size="small"
                                          />
                                          <span>
                                            {label}：{capabilityStateLabels[cap.state] || cap.state}
                                            {cap.error_code ? `（${cap.error_code}）` : ''}
                                          </span>
                                        </span>
                                      ))}
                                    </div>

                                    <button
                                      type="button"
                                      aria-expanded={expanded}
                                      onClick={() =>
                                        setExpandedTenants((prev) => ({
                                          ...prev,
                                          [tenant.id]: !prev[tenant.id],
                                        }))
                                      }
                                      className="cursor-pointer rounded-lg px-2 py-1 system-xs-medium text-text-tertiary transition-colors hover:bg-background-section-burn hover:text-text-primary"
                                    >
                                      {expanded ? '收起详情' : '查看详情'}
                                    </button>
                                  </div>
                                </div>

                                {expanded && (
                                  <div className="mt-3 grid gap-2.5 rounded-xl bg-background-section-burn/60 p-3.5 system-xs-regular sm:grid-cols-2 lg:grid-cols-4">
                                    <div className="space-y-1">
                                      <div className="text-text-tertiary">租户编号</div>
                                      <CopyableChip value={tenant.id} />
                                    </div>
                                    <div className="space-y-1">
                                      <div className="text-text-tertiary">
                                        工作区（
                                        {workspaceStateLabels[tenant.workspace.state] ||
                                          tenant.workspace.state}
                                        ）
                                      </div>
                                      {tenant.workspace.id ? (
                                        <CopyableChip value={tenant.workspace.id} />
                                      ) : (
                                        <span className="text-text-tertiary">未创建</span>
                                      )}
                                    </div>
                                    <div className="space-y-1">
                                      <div className="text-text-tertiary">双端应用</div>
                                      <div className="flex flex-wrap gap-1.5">
                                        <StatusPill tone={appTone(tenant.apps.operator.state)}>
                                          管家：
                                          {appStateLabels[tenant.apps.operator.state] ||
                                            tenant.apps.operator.state}
                                        </StatusPill>
                                        <StatusPill tone={appTone(tenant.apps.consumer.state)}>
                                          客服：
                                          {appStateLabels[tenant.apps.consumer.state] ||
                                            tenant.apps.consumer.state}
                                        </StatusPill>
                                      </div>
                                    </div>
                                    <div className="space-y-1">
                                      <div className="text-text-tertiary">知识空间与业务验收</div>
                                      <div className="text-text-secondary">
                                        {tenant.knowledge.knowledge_base_id
                                          ? `已连接（${tenant.knowledge.active_grants} 项授权）`
                                          : '未准备'}
                                        {' · 业务准确性：待人工确认'}
                                      </div>
                                    </div>
                                  </div>
                                )}
                              </li>
                            )
                          })}
                        </ul>
                      ) : (
                        <div className="flex flex-wrap items-center justify-between gap-2 px-6 py-5 system-sm-regular text-text-secondary">
                          <span>
                            {draft.tenants.length === 0
                              ? '当前环境尚未配置租户'
                              : `已配置 ${draft.tenants.length} 个草稿租户，尚未读取到运行态总览`}
                          </span>
                          <Button
                            type="button"
                            variant="ghost"
                            size="small"
                            onClick={() => setActiveTab('config')}
                          >
                            管理租户配置
                          </Button>
                        </div>
                      )}
                    </section>
                  )}

                  {/* 3. 精简「近期开通与发布」动态（默认仅展示进行中 + 最近 2 条核心记录） */}
                  {selected && (
                    <section
                      aria-label="近期开通与发布"
                      className="rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-6 py-4">
                        <div className="flex items-center gap-2">
                          <h3 className="system-md-semibold text-text-primary">近期开通与发布</h3>
                          {tasks.length > 0 && (
                            <span className="system-xs-regular text-text-tertiary">
                              仅展示近期核心动态
                            </span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <Button
                            type="button"
                            variant="ghost"
                            size="small"
                            onClick={() => void siteTasks()}
                          >
                            刷新
                          </Button>
                          {tasks.length > 0 && (
                            <Button
                              type="button"
                              variant="ghost"
                              size="small"
                              onClick={() => setActiveTab('ops')}
                            >
                              全部历史与排障（{tasks.length}）→
                            </Button>
                          )}
                        </div>
                      </div>

                      {taskError && (
                        <div className="mx-6 mt-4 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-3.5 py-2.5">
                          <p role="alert" className="system-xs-medium text-text-destructive">
                            {taskError}
                          </p>
                        </div>
                      )}

                      {focusedLifecycleTasks.length === 0 ? (
                        <p className="px-6 py-6 text-center system-xs-regular text-text-tertiary">
                          {tasks.length === 0
                            ? '暂无开通记录，完成上方预检后即可一键开通。'
                            : '当前无进行中或近期一键开通记录，历史单步记录请前往「运行与排查」查看。'}
                        </p>
                      ) : (
                        <div className="divide-y divide-divider-subtle">
                          {focusedLifecycleTasks.map((item) => {
                            const highlightedInHero =
                              (!plan || dirty) &&
                              draftReady &&
                              readyToPublishOnboard?.id === item.id
                            return (
                              <article key={item.id} className="space-y-3 px-6 py-4">
                                <section
                                  aria-label={
                                    item.action === 'tenant-onboard'
                                      ? `一键开通进度：${item.tenant_id}`
                                      : undefined
                                  }
                                  className="space-y-3"
                                >
                                  <div className="flex flex-wrap items-center justify-between gap-3">
                                    <div className="flex flex-wrap items-center gap-2.5">
                                      <span className="system-sm-semibold text-text-primary">
                                        {item.action === 'tenant-onboard'
                                          ? `一键开通 · ${tenantNameOf(item.tenant_id)}${
                                              item.state === 'observed' &&
                                              item.result.ready_to_publish
                                                ? '（技术验收已就绪，待人工确认发布）'
                                                : ''
                                            }`
                                          : item.action === 'environment-publish'
                                            ? `正式开通 · ${tenantNameOf(item.tenant_id)}（${
                                                publicationPhases[
                                                  String(item.result?.phase || '')
                                                ] || String(item.result?.phase || '')
                                              } · 准入 ${item.result?.activated ? '已启用' : '未启用'}）`
                                            : `${taskActionLabels[item.action || ''] || '环境任务'}${item.tenant_id ? ` · ${tenantNameOf(item.tenant_id)}` : ''}`}
                                      </span>
                                      <StatusPill tone={taskTone(item.state)}>
                                        {item.action === 'environment-publish' && item.result?.phase
                                          ? publicationPhases[String(item.result.phase)] ||
                                            String(item.result.phase)
                                          : taskLabels[item.state] || item.state}
                                        {item.cancel_requested && item.state === 'running'
                                          ? ' · 取消中'
                                          : ''}
                                      </StatusPill>
                                      {item.error_code && (
                                        <StatusPill tone="error" dot={false}>
                                          {item.error_code}
                                        </StatusPill>
                                      )}
                                    </div>

                                    <div className="flex flex-wrap items-center gap-2">
                                      {!highlightedInHero &&
                                        pendingPublishOnboardIds.has(item.id) &&
                                        renderPublishConfirmButton(item)}

                                      {item.action === 'environment-publish' &&
                                        ['observed', 'reconcile', 'blocked'].includes(item.state) &&
                                        item.result?.phase !== 'restored' && (
                                          <Button
                                            type="button"
                                            variant="secondary"
                                            tone="destructive"
                                            size="small"
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

                                      {['queued', 'running', 'reconcile'].includes(item.state) && (
                                        <Button
                                          type="button"
                                          variant="secondary"
                                          size="small"
                                          onClick={() =>
                                            void siteTasks({ id: item.id, cancel: true })
                                          }
                                        >
                                          {item.action === 'tenant-onboard'
                                            ? '取消一键开通'
                                            : item.action === 'environment-publish'
                                              ? '停止开通并恢复'
                                              : '取消任务'}
                                        </Button>
                                      )}
                                    </div>
                                  </div>

                                  {/* 干净的四阶段中文进度条（隐藏子任务 UUID） */}
                                  {item.action === 'tenant-onboard' &&
                                    (item.result.stages || []).length > 0 && (
                                      <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                                        {(item.result.stages || []).map((stage) => (
                                          <li
                                            key={stage.action}
                                            className="flex items-center justify-between gap-2 rounded-lg bg-background-section-burn/70 px-3 py-2 system-xs-regular"
                                          >
                                            <span className="flex items-center gap-1.5 font-medium text-text-primary">
                                              <StatusDot
                                                status={badgeToneStyles[taskTone(stage.state)].dot}
                                                size="small"
                                              />
                                              <span>
                                                {onboardingStageLabels[stage.action] ||
                                                  stage.action}
                                              </span>
                                            </span>
                                            <span className="text-text-tertiary">
                                              {taskLabels[stage.state] || stage.state}
                                            </span>
                                          </li>
                                        ))}
                                      </ul>
                                    )}
                                </section>

                                <details className="text-text-tertiary">
                                  <summary className="cursor-pointer text-[11px] select-none hover:text-text-primary">
                                    查看任务编号与技术详情
                                  </summary>
                                  <div className="mt-2 flex flex-wrap gap-2">
                                    <CopyableChip label="任务" value={item.id} />
                                    {item.plan_id && (
                                      <CopyableChip label="计划" value={item.plan_id} />
                                    )}
                                    {(item.result.stages || []).map((stage) => (
                                      <CopyableChip
                                        key={stage.id}
                                        label={onboardingStageLabels[stage.action] || stage.action}
                                        value={stage.id}
                                      />
                                    ))}
                                  </div>
                                </details>
                              </article>
                            )
                          })}
                        </div>
                      )}
                    </section>
                  )}
                </div>
              )}

              {/* ================================================================= */}
              {/* TAB 2: 连接配置（专注表单编辑，仅此处包裹 <form onSubmit>）          */}
              {/* ================================================================= */}
              {activeTab === 'config' && (
                <form
                  id="aiops-panel-config"
                  role="tabpanel"
                  aria-labelledby="aiops-tab-config"
                  onSubmit={(event) => {
                    event.preventDefault()
                    void save()
                  }}
                >
                  <fieldset
                    disabled={busy || (!selected && !data.can_create)}
                    className="divide-y divide-divider-subtle rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs"
                  >
                    <legend className="sr-only">连接配置</legend>

                    <div className="px-6 py-4">
                      <h3 className="system-md-semibold text-text-primary">连接配置</h3>
                      <p className="mt-0.5 system-xs-regular text-text-tertiary">
                        维护环境端点、受管引用与租户清单；保存仅更新草稿，不影响现役运行状态。
                      </p>
                    </div>

                    {fieldGroups.map((group) => (
                      <div key={group.title} className="space-y-3.5 px-6 py-5">
                        <div>
                          <div className="system-sm-semibold text-text-primary">{group.title}</div>
                          <div className="system-xs-regular text-text-tertiary">
                            {group.description}
                          </div>
                        </div>
                        <div className="grid gap-3.5 md:grid-cols-2 xl:grid-cols-3">
                          {group.items.map(([key, label, maxLength, placeholder]) => (
                            <label key={key} className="block space-y-1.5">
                              <span className="block system-xs-medium text-text-secondary">
                                {label}
                              </span>
                              <Input
                                aria-label={label}
                                required={key !== 'company_gateway_url'}
                                maxLength={maxLength}
                                placeholder={placeholder}
                                disabled={key === 'id' && Boolean(selected)}
                                value={draft[key]}
                                onChange={(event) =>
                                  setDraft({ ...draft, [key]: event.target.value })
                                }
                              />
                            </label>
                          ))}
                        </div>
                      </div>
                    ))}

                    <div className="px-6 py-4">
                      <label className="inline-flex cursor-pointer items-center gap-2.5 system-sm-medium text-text-primary">
                        <Checkbox
                          aria-label="环境预期准入"
                          checked={draft.enabled}
                          onCheckedChange={(value) =>
                            setDraft({ ...draft, enabled: value === true })
                          }
                        />
                        <span>预期允许该环境准入（仅记录期望状态，需完成发布流程后生效）</span>
                      </label>
                    </div>

                    {/* 租户草稿清单 */}
                    <section aria-label="租户草稿" className="space-y-3.5 px-6 py-5">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <h3 className="system-sm-semibold text-text-primary">
                            租户清单（{draft.tenants.length}）
                          </h3>
                          <p className="system-xs-regular text-text-tertiary">
                            新增租户默认不勾选准入
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

                      {draft.tenants.length === 0 ? (
                        <p className="rounded-xl bg-background-section-burn/60 py-5 text-center system-xs-regular text-text-tertiary">
                          暂无租户，点击「新增租户」添加
                        </p>
                      ) : (
                        <div className="space-y-2.5">
                          {draft.tenants.map((tenant, index) => {
                            const persisted = Boolean(
                              (saved?.draft.tenants ?? live?.tenants)?.[index],
                            )
                            return (
                              <div
                                // oxlint-disable-next-line react/no-array-index-key -- Tenant IDs are user-editable before saving.
                                key={index}
                                className="grid items-end gap-3 rounded-xl bg-background-section-burn/50 p-3.5 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto_auto]"
                              >
                                <label className="block space-y-1">
                                  <span className="block system-xs-medium text-text-secondary">
                                    租户 {index + 1} 编号
                                  </span>
                                  <Input
                                    aria-label={`租户 ${index + 1} 编号`}
                                    required
                                    maxLength={64}
                                    placeholder="稳定租户 ID"
                                    disabled={persisted}
                                    value={tenant.id}
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
                                <label className="block space-y-1">
                                  <span className="block system-xs-medium text-text-secondary">
                                    租户 {index + 1} 名称
                                  </span>
                                  <Input
                                    aria-label={`租户 ${index + 1} 名称`}
                                    required
                                    maxLength={128}
                                    placeholder="公司或运营商名称"
                                    value={tenant.name}
                                    onChange={(event) =>
                                      setDraft({
                                        ...draft,
                                        tenants: draft.tenants.map((item, i) =>
                                          i === index
                                            ? { ...item, name: event.target.value }
                                            : item,
                                        ),
                                      })
                                    }
                                  />
                                </label>
                                <label className="flex h-9 cursor-pointer items-center gap-2 system-xs-medium text-text-secondary">
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
                                {!persisted && (
                                  <Button
                                    type="button"
                                    variant="ghost"
                                    size="small"
                                    onClick={() =>
                                      setDraft({
                                        ...draft,
                                        tenants: draft.tenants.filter((_, i) => i !== index),
                                      })
                                    }
                                  >
                                    移除
                                  </Button>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </section>

                    <footer className="flex flex-wrap items-center justify-between gap-3 px-6 py-4">
                      <span className="system-xs-regular text-text-tertiary">
                        保存后点「配置与制品预检」即可进入一键开通
                      </span>
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

              {/* ================================================================= */}
              {/* TAB 3: 运行与排查（节点工具、全环境对比、全量任务历史与单步排障）      */}
              {/* ================================================================= */}
              {activeTab === 'ops' && (
                <div
                  id="aiops-panel-ops"
                  role="tabpanel"
                  aria-labelledby="aiops-tab-ops"
                  className="space-y-5"
                >
                  {!selected && (
                    <section
                      aria-label="运行排查状态"
                      className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-dashed border-divider-regular bg-components-panel-bg px-6 py-6"
                    >
                      <p className="system-sm-regular text-text-secondary">
                        {data.can_create
                          ? '当前处于新建环境草稿模式，保存环境后即可查看节点与任务记录。'
                          : '当前暂无已分配环境，请联系管理员授权后刷新目录。'}
                      </p>
                      {data.can_create && (
                        <Button
                          type="button"
                          variant="secondary"
                          size="small"
                          onClick={() => setActiveTab('config')}
                        >
                          前往连接配置
                        </Button>
                      )}
                    </section>
                  )}
                  {/* 1. 运行节点与工具 */}
                  {selected && (
                    <section
                      aria-label="运行节点与工具"
                      className="rounded-2xl border border-components-panel-border bg-components-panel-bg p-6 shadow-xs"
                    >
                      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                        <div>
                          <h3 className="system-md-semibold text-text-primary">运行节点与工具</h3>
                          <p className="mt-0.5 system-xs-regular text-text-tertiary">
                            {selectedOverview?.is_control
                              ? '实时执行节点心跳与工具兼容性'
                              : '远端环境受管任务点时回执（非实时心跳）'}
                          </p>
                        </div>
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

                      {!selectedOverview || selectedOverview.nodes.length === 0 ? (
                        <p className="rounded-xl bg-background-section-burn/60 py-5 text-center system-xs-regular text-text-tertiary">
                          {selectedOverview?.is_control
                            ? '暂无在线节点心跳记录'
                            : '暂无受管任务节点回执'}
                        </p>
                      ) : (
                        <div className="grid gap-3 md:grid-cols-2">
                          {selectedOverview.nodes.map((node) => (
                            <div
                              key={`${node.source}-${node.node_id}`}
                              className="space-y-2 rounded-xl bg-background-section-burn/60 p-4 system-xs-regular"
                            >
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <span className="font-mono system-sm-semibold text-text-primary">
                                  {node.node_id}
                                </span>
                                <StatusPill tone={nodeTone(node.status)}>
                                  {nodeStatusLabels[node.status] || node.status}
                                </StatusPill>
                              </div>
                              <div className="flex flex-wrap gap-x-4 gap-y-1 text-text-secondary">
                                <span>版本：{node.version || '—'}</span>
                                <span>观测：{formatTimestamp(node.observed_at)}</span>
                              </div>
                              {node.source === 'heartbeat' ? (
                                <div className="space-y-1.5">
                                  <div className="flex items-center gap-2">
                                    <StatusPill
                                      tone={node.tools_compatible ? 'success' : 'warning'}
                                      dot={false}
                                    >
                                      {node.tools_compatible
                                        ? `${node.available_tools.length} 个工具兼容`
                                        : '工具目录待升级'}
                                    </StatusPill>
                                  </div>
                                  {node.available_tools.length > 0 && (
                                    <details>
                                      <summary className="cursor-pointer text-[11px] text-text-tertiary select-none hover:text-text-primary">
                                        展开工具清单（{node.available_tools.length}）
                                      </summary>
                                      <div className="mt-1.5 flex flex-wrap gap-1">
                                        {node.available_tools.map((tool) => (
                                          <span
                                            key={tool}
                                            className="rounded bg-components-panel-bg px-1.5 py-0.5 font-mono text-[11px] text-text-secondary"
                                          >
                                            {tool}
                                          </span>
                                        ))}
                                      </div>
                                    </details>
                                  )}
                                </div>
                              ) : (
                                <div className="text-text-tertiary">
                                  主机：{node.remote_hostname || '—'}
                                  {node.installation
                                    ? ` · 安装：${initialInstallationLabels[node.installation] || node.installation}`
                                    : ''}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      )}
                    </section>
                  )}

                  {/* 2. 全环境运行对比表（收纳于此，不再占首屏） */}
                  {overview && overview.environments.length > 0 && (
                    <section
                      aria-label="环境总览"
                      className="overflow-hidden rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-6 py-4">
                        <div className="flex items-center gap-2">
                          <h3 className="system-md-semibold text-text-primary">环境总览</h3>
                          <span className="system-xs-regular text-text-tertiary">
                            目录 R{overview.registry_revision}
                            {overview.control_version ? ` · v${overview.control_version}` : ''}
                          </span>
                        </div>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left system-xs-regular">
                          <thead>
                            <tr className="border-b border-divider-subtle bg-background-section-burn/50 text-text-tertiary">
                              <th className="px-6 py-2.5 font-medium">环境</th>
                              <th className="px-3 py-2.5 font-medium">准入状态</th>
                              <th className="px-3 py-2.5 font-medium">租户</th>
                              <th className="px-3 py-2.5 font-medium">节点与版本</th>
                              <th className="px-6 py-2.5 font-medium">草稿</th>
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
                                      ? 'bg-state-accent-hover/60'
                                      : 'hover:bg-components-panel-on-panel-item-bg-hover'
                                  }`}
                                >
                                  <td className="px-6 py-3 font-medium text-text-primary">
                                    {env.name}（{env.id}）{env.is_control ? ' · 控制环境' : ''}
                                  </td>
                                  <td className="px-3 py-3">
                                    <StatusPill tone={envTone(env.status)}>
                                      {environmentStatusLabels[env.status] || env.status}
                                    </StatusPill>
                                  </td>
                                  <td className="px-3 py-3 text-text-primary">
                                    {env.enabled_tenant_count}/{env.tenant_count}
                                  </td>
                                  <td className="px-3 py-3 text-text-secondary">
                                    {summarizeNodeCell(env)}
                                  </td>
                                  <td className="px-6 py-3 text-text-secondary">
                                    {env.draft_revision != null ? `V${env.draft_revision}` : '无'}
                                    {env.registry_changed ? '（目录已变更）' : ''}
                                  </td>
                                </tr>
                              )
                            })}
                          </tbody>
                        </table>
                      </div>
                    </section>
                  )}

                  {/* 3. 全量任务历史与单步排障操作 */}
                  {selected && (
                    <section
                      aria-label="现场预检任务"
                      className="space-y-4 rounded-2xl border border-components-panel-border bg-components-panel-bg p-6 shadow-xs"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-divider-subtle pb-4">
                        <div className="flex items-center gap-2">
                          <h3 className="system-md-semibold text-text-primary">环境准备任务</h3>
                          <span className="rounded-full bg-background-section-burn px-2 py-0.5 system-xs-medium text-text-tertiary">
                            共 {tasks.length} 条
                          </span>
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

                      {draftReady && (!plan || plan.state === 'expired') && tasks.length > 0 && (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-background-section-burn/70 px-4 py-2.5">
                          <span className="system-xs-regular text-text-secondary">
                            执行单步登记、业务验证或正式发布前需持有有效预检计划。
                          </span>
                          <Button
                            type="button"
                            variant="secondary"
                            size="small"
                            disabled={busy}
                            onClick={() => void preflight()}
                          >
                            配置与制品预检
                          </Button>
                        </div>
                      )}

                      {tasks.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {(
                            [
                              ['all', `全部 (${tasks.length})`],
                              ['active', `进行中/待发布 (${activeTasksCount})`],
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

                      {visibleOpsTasks.length === 0 && (
                        <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-background-section-burn/50 py-6 text-center">
                          <p className="system-sm-regular text-text-secondary">
                            {tasks.length === 0 ? '当前环境暂无任务记录' : '当前筛选下无任务记录'}
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

                      <div className="divide-y divide-divider-subtle">
                        {visibleOpsTasks.map((item) => {
                          const simpleStatusText =
                            item.action === 'workspace-prepare' && item.state === 'observed'
                              ? '候选工作区已准备'
                              : item.action === 'knowledge-prepare' && item.state === 'observed'
                                ? '候选知识资源已准备'
                                : item.action === 'knowledge-connect' && item.state === 'observed'
                                  ? '候选知识连接已准备'
                                  : item.action === 'operator-prepare' && item.state === 'observed'
                                    ? '候选管家应用已准备（入口停用）'
                                    : item.action === 'consumer-prepare' &&
                                        item.state === 'observed'
                                      ? '候选消费者应用已准备（入口停用）'
                                      : taskLabels[item.state] || item.state

                          return (
                            <article key={item.id} className="space-y-3 py-4 first:pt-1 last:pb-0">
                              <div className="flex flex-wrap items-center justify-between gap-2">
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

                              {/* 一键开通 */}
                              {item.action === 'tenant-onboard' && (
                                <section
                                  aria-label={`一键开通进度：${item.tenant_id}`}
                                  className="space-y-2.5"
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
                                    {renderPublishConfirmButton(item)}
                                  </div>
                                  {(item.result.stages || []).length > 0 && (
                                    <ul className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
                                      {(item.result.stages || []).map((stage) => (
                                        <li
                                          key={stage.action}
                                          className="space-y-1 rounded-lg bg-background-section-burn/70 p-2.5 system-xs-regular"
                                        >
                                          <div className="flex items-center gap-1.5 font-medium text-text-primary">
                                            <StatusDot
                                              status={badgeToneStyles[taskTone(stage.state)].dot}
                                              size="small"
                                            />
                                            <span>
                                              {onboardingStageLabels[stage.action] || stage.action}
                                              ：{taskLabels[stage.state] || stage.state}
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
                                </section>
                              )}

                              {/* 正式发布 */}
                              {item.action === 'environment-publish' && (
                                <section className="flex flex-wrap items-center justify-between gap-3">
                                  <p className="system-sm-semibold text-text-primary">
                                    正式开通 · {item.tenant_id}：
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
                                        tone="destructive"
                                        size="small"
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
                                  className="space-y-1"
                                >
                                  <p className="system-sm-semibold text-text-primary">
                                    主机身份验收 · {item.environment}：
                                    {taskLabels[item.state] || item.state}
                                  </p>
                                  {item.result.evidence && (
                                    <div className="flex flex-wrap gap-3 system-xs-regular text-text-secondary">
                                      <span>
                                        现场主机：{item.result.evidence.remote_hostname || '未采集'}
                                      </span>
                                      <span>
                                        服务身份：
                                        {item.result.evidence.service_user_present
                                          ? '存在'
                                          : '缺失'}
                                      </span>
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
                                </section>
                              )}

                              {item.action === 'candidate-install' && (
                                <section
                                  aria-label={`候选安装进度：${item.tenant_id}`}
                                  className="flex flex-wrap items-center justify-between gap-3"
                                >
                                  <div className="space-y-0.5">
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
                                  <section aria-label={`候选登记进度：${item.tenant_id}`}>
                                    <p className="system-sm-semibold text-text-primary">
                                      候选运行登记 · {item.tenant_id}：
                                      {taskLabels[item.state] || item.state}
                                    </p>
                                    <p className="system-xs-regular text-text-tertiary">
                                      {['registered', 'unchanged'].includes(
                                        item.result.result || '',
                                      )
                                        ? '运行资源已登记'
                                        : '运行登记尚未确认'}{' '}
                                      · 业务未验收 · 正式入口未启用
                                    </p>
                                  </section>
                                  {['registered', 'unchanged'].includes(
                                    item.result.result || '',
                                  ) && (
                                    <section
                                      aria-label={`业务验证选择：${item.tenant_id}`}
                                      className="space-y-3 rounded-xl bg-background-section-burn/60 p-3.5"
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
                                        <legend className="mb-1 system-xs-medium text-text-tertiary">
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
                                  {pendingPublishValidateIds.has(item.id) && (
                                    <Button
                                      type="button"
                                      variant="primary"
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
                                          className="rounded-lg bg-background-section-burn/70 p-2 system-xs-regular"
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
                                          initialInstallationLabels[
                                            item.result.initial_installation
                                          ] || item.result.initial_installation
                                        } · 运行安装未执行 · 业务未验收 · 正式入口未启用`
                                      : '安装未执行 · 业务未验收 · 正式入口未启用'}
                                  </p>
                                </section>
                              )}

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

                              {item.result.checks && item.result.checks.length > 0 && (
                                <div className="flex flex-wrap gap-1.5">
                                  {item.result.checks.map((check) => {
                                    const { tone, label, stateText } = checkDisplay(check)
                                    return (
                                      <p
                                        key={check.id || check.capability}
                                        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 system-xs-regular ${badgeToneStyles[tone].wrap}`}
                                      >
                                        <StatusDot
                                          status={badgeToneStyles[tone].dot}
                                          size="small"
                                        />
                                        <span>
                                          {label}：{stateText}
                                          {check.error_code ? ` ${check.error_code}` : ''}
                                        </span>
                                      </p>
                                    )
                                  })}
                                </div>
                              )}

                              {item.action === 'knowledge-connect' &&
                                item.state === 'observed' &&
                                !dirty &&
                                !saved?.registry_changed && (
                                  <div className="space-y-2.5 rounded-xl bg-background-section-burn/60 p-3">
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
                                            className="block h-8 w-full rounded-lg border border-divider-regular bg-components-panel-bg px-2.5 system-xs-regular text-text-primary"
                                            aria-label={`消费者来源模板：${item.tenant_id}`}
                                            value={consumerSource}
                                            onChange={(event) =>
                                              setConsumerSource(event.target.value)
                                            }
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
                                          void siteTasks({
                                            site_task: item.id,
                                            tenant_id: tenant.id,
                                          })
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
              )}
            </div>
          </div>
        )}
      </div>
    </section>
  )
}
