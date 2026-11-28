import type { StatusDotStatus } from './ui/status-dot'
import {
  AlertDialog,
  AlertDialogActions,
  AlertDialogCancelButton,
  AlertDialogConfirmButton,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogTitle,
} from './ui/alert-dialog'
import { Button } from './ui/button'
import { Checkbox } from './ui/checkbox'
import { Input } from './ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from './ui/select'
import { StatusDot } from './ui/status-dot'
import { useEffect, useRef, useState } from 'react'
import { manage } from '../lib/manage'
import EnvironmentReleasesPanel from './aiops-environment-releases'
import { LiquidGlassSurface } from './liquid-glass'
import OnboardingWizard from './aiops-onboarding-wizard'

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

type WorkspaceTab = 'onboard' | 'releases' | 'config' | 'ops'
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
    wrap: 'bg-components-badge-bg-green-soft text-text-success border-state-success-active/50',
    dot: 'success',
  },
  warning: {
    wrap: 'bg-components-badge-bg-orange-soft text-text-warning border-state-warning-active/40',
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

function stageBoxClass(state: string): string {
  if (state === 'observed')
    return 'border-state-success-active/40 bg-components-badge-bg-green-soft/60'
  if (state === 'running' || state === 'queued')
    return 'border-divider-accent/70 bg-components-badge-bg-blue-light-soft/70'
  if (state === 'reconcile')
    return 'border-state-warning-active/40 bg-components-badge-bg-orange-soft/60'
  if (state === 'blocked' || state === 'expired')
    return 'border-state-destructive-border bg-components-badge-bg-red-soft/60'
  return 'border-divider-subtle bg-background-section-burn/70'
}

function taskActionIcon(action?: string): string {
  if (action === 'tenant-onboard') return 'i-ri-rocket-line'
  if (action === 'environment-publish') return 'i-ri-checkbox-circle-line'
  if (action === 'host-verify') return 'i-ri-shield-check-line'
  if (action === 'site-install' || action === 'candidate-install') return 'i-ri-server-line'
  if (action === 'candidate-validate') return 'i-ri-pulse-line'
  return 'i-ri-terminal-box-line'
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
      className={`inline-flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-0.5 system-xs-medium whitespace-nowrap shadow-2xs ${style.wrap} ${className}`}
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
    <span className="inline-flex max-w-full items-center gap-1.5 rounded-lg border border-divider-subtle bg-background-section-burn px-2.5 py-1 font-mono text-[11px] text-text-secondary">
      {label && <span className="font-sans text-text-tertiary">{label}</span>}
      <span className="break-all select-all">{value}</span>
      <button
        type="button"
        onClick={copy}
        aria-label={`复制 ${value}`}
        title={status === 'idle' ? `复制 ${value}` : text}
        className={`ml-0.5 inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-md px-1.5 py-0.5 font-sans text-[10px] font-medium transition-colors ${
          status === 'copied'
            ? 'bg-components-badge-bg-green-soft text-text-success'
            : status === 'failed'
              ? 'bg-components-badge-bg-red-soft text-text-destructive'
              : 'text-text-tertiary hover:bg-components-button-ghost-bg-hover hover:text-text-primary'
        }`}
      >
        <span
          aria-hidden="true"
          className={`size-3 ${status === 'copied' ? 'i-ri-check-line' : 'i-ri-file-copy-line'}`}
        />
        <span>{text}</span>
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
    <ol
      aria-label="开通流程指引"
      className="grid grid-cols-2 gap-3 sm:flex sm:items-center sm:gap-2"
    >
      {steps.map((step, index) => (
        <li
          key={step.num}
          className={`flex items-center gap-2 ${index < steps.length - 1 ? 'sm:flex-1' : 'sm:shrink-0'}`}
        >
          <div className="flex shrink-0 items-center gap-2">
            <span
              className={`flex size-6 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold transition-all ${
                step.done
                  ? 'border border-state-success-active/60 bg-components-badge-bg-green-soft text-text-success'
                  : step.active
                    ? 'bg-state-accent-solid text-text-primary-on-surface ring-4 ring-state-accent-hover'
                    : 'border border-divider-subtle bg-components-badge-bg-gray-soft text-text-tertiary'
              }`}
            >
              {step.done ? (
                <span aria-hidden="true" className="i-ri-check-line size-3.5" />
              ) : (
                step.num
              )}
            </span>
            <span
              className={`system-xs-medium whitespace-nowrap ${
                step.active
                  ? 'text-text-accent'
                  : step.done
                    ? 'text-text-primary'
                    : 'text-text-tertiary'
              }`}
            >
              {step.label}
            </span>
          </div>
          {index < steps.length - 1 && (
            <div
              aria-hidden="true"
              className={`mx-1 hidden h-0.5 flex-1 rounded-full sm:block ${
                step.done ? 'bg-state-success-solid/45' : 'bg-divider-subtle'
              }`}
            />
          )}
        </li>
      ))}
    </ol>
  )
}

export default function EnvironmentManagement() {
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
  const [showWizard, setShowWizard] = useState(false)
  const [envQuery, setEnvQuery] = useState('')
  const [workspaceBusyKey, setWorkspaceBusyKey] = useState('')

  const openTenantWorkspace = async (workspaceId: string, knowledge = false) => {
    if (!workspaceId) return
    const key = `${workspaceId}:${knowledge ? 'kb' : 'apps'}`
    setWorkspaceBusyKey(key)
    setError('')
    try {
      await manage('workspace', { workspace_id: workspaceId })
      window.location.assign(
        knowledge
          ? `/__aiops/knowledge-open?workspace=${encodeURIComponent(workspaceId)}`
          : '/apps',
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : '进入工作区失败')
    } finally {
      setWorkspaceBusyKey('')
    }
  }

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
      setPending(() => () => window.location.assign(`${next.pathname}${next.search}${next.hash}`))
    }
    window.addEventListener('beforeunload', beforeUnload)
    document.addEventListener('click', onLinkClick, true)
    return () => {
      window.removeEventListener('beforeunload', beforeUnload)
      document.removeEventListener('click', onLinkClick, true)
    }
  }, [dirty])

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
  const normalizedEnvQuery = envQuery.trim().toLowerCase()
  const filteredRows =
    rows.length > 4 && normalizedEnvQuery
      ? rows.filter(
          (row) =>
            row.id.toLowerCase().includes(normalizedEnvQuery) ||
            (row.name || '').toLowerCase().includes(normalizedEnvQuery),
        )
      : rows
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
  // - 否则仅取最新一条 ready_to_publish 的 tenant-onboard 或三能力全通的 candidate-validate，
  //   作为租户级待发布代表：hero 单一引导、「进行中/待发布」筛选与开通卡便捷按钮按租户去重、
  //   发布后自动退出筛选。验证任务卡自身的发布按钮不按此去重——服务端按所选验证的
  //   证据链（case_ref/验证任务号）发布，旧验证与新验证不等价，凡就绪验证均保留发布动作。
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

      <div className="mx-auto max-w-[1360px] space-y-5 px-4 py-5 sm:px-6">
        {/* 顶部工作台横幅与全局概览指标 */}
        <LiquidGlassSurface
          as="header"
          variant="hero"
          refract
          interactive
          className="rounded-2xl px-5 py-4 sm:px-6"
        >
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-3.5">
              <div
                aria-hidden="true"
                className="aiops-glass-pill flex size-11 shrink-0 items-center justify-center rounded-2xl text-text-accent"
              >
                <span className="i-ri-server-line size-5" />
              </div>
              <div>
                <div className="flex flex-wrap items-center gap-2.5">
                  <h1 className="system-xl-semibold text-text-primary">环境与租户</h1>
                  {overview && (
                    <span className="aiops-glass-pill inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 system-xs-medium text-text-secondary">
                      <span
                        aria-hidden="true"
                        className="i-ri-git-branch-line size-3 text-text-accent"
                      />
                      <span>目录 R{overview.registry_revision}</span>
                    </span>
                  )}
                </div>
                <p className="mt-0.5 system-xs-regular text-text-tertiary">
                  选择左侧业务环境，按引导完成配置预检、一键开通与版本演进管理
                </p>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Button
                variant="secondary"
                className="aiops-glass-pill-btn"
                disabled={busy}
                onClick={() => {
                  navigate(() => {
                    void load(selected)
                  })
                }}
              >
                <span
                  aria-hidden="true"
                  className={`mr-1.5 i-ri-refresh-line size-3.5 ${busy ? 'animate-spin' : ''}`}
                />
                刷新目录
              </Button>
              {data?.can_create && (
                <Button
                  variant="primary"
                  className="rounded-full!"
                  disabled={busy}
                  onClick={() => {
                    navigate(() => {
                      select(data, '')
                      setActiveTab('config')
                    })
                  }}
                >
                  <span aria-hidden="true" className="mr-1 i-ri-add-line size-4" />
                  新增环境
                </Button>
              )}
            </div>
          </div>

          {overview && (
            <div className="mt-4 grid gap-3 border-t border-divider-subtle pt-3.5 sm:grid-cols-3">
              <div className="aiops-glass-card flex items-center justify-between rounded-xl px-3.5 py-2.5">
                <div className="flex items-center gap-2.5">
                  <span
                    aria-hidden="true"
                    className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-blue-soft text-text-accent"
                  >
                    <span className="i-ri-server-line size-4" />
                  </span>
                  <div>
                    <div className="text-[11px] text-text-tertiary">已纳管环境</div>
                    <div className="system-sm-semibold text-text-primary tabular-nums">
                      {overview.environments.length} 个环境
                    </div>
                  </div>
                </div>
                <span className="rounded-full bg-components-badge-bg-green-soft px-2 py-0.5 text-[11px] font-medium text-text-success tabular-nums">
                  {overview.environments.filter((e) => e.status === 'active').length} 启用中
                </span>
              </div>

              <div className="aiops-glass-card flex items-center justify-between gap-3 rounded-xl px-3.5 py-2.5">
                <div className="flex items-center gap-2.5">
                  <span
                    aria-hidden="true"
                    className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-green-soft text-text-success"
                  >
                    <span className="i-ri-group-2-line size-4" />
                  </span>
                  <div>
                    <div className="text-[11px] text-text-tertiary">租户准入覆盖</div>
                    <div className="system-sm-semibold whitespace-nowrap text-text-primary tabular-nums">
                      {enabledTenantsCount} / {totalTenantsCount} 租户已启用
                    </div>
                  </div>
                </div>
                <div className="hidden w-14 shrink-0 md:block">
                  <div className="h-1.5 overflow-hidden rounded-full bg-components-progress-bar-bg">
                    <div
                      className="h-full rounded-full bg-state-success-solid transition-all"
                      style={{
                        width: `${totalTenantsCount > 0 ? Math.round((enabledTenantsCount / totalTenantsCount) * 100) : 0}%`,
                      }}
                    />
                  </div>
                </div>
              </div>

              <div className="aiops-glass-card flex items-center justify-between rounded-xl px-3.5 py-2.5">
                <div className="flex items-center gap-2.5">
                  <span
                    aria-hidden="true"
                    className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-indigo-soft text-text-accent"
                  >
                    <span className="i-ri-shield-check-line size-4" />
                  </span>
                  <div>
                    <div className="text-[11px] text-text-tertiary">控制面与目录修订</div>
                    <div className="system-sm-semibold text-text-primary tabular-nums">
                      R{overview.registry_revision}
                      {overview.control_version ? ` · v${overview.control_version}` : ''}
                    </div>
                  </div>
                </div>
                <span className="text-[11px] text-text-tertiary">只读诊断</span>
              </div>
            </div>
          )}
        </LiquidGlassSurface>

        {/* 全局反馈条 */}
        {error && (
          <div className="flex items-center gap-2.5 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-4 py-3 shadow-2xs">
            <span
              aria-hidden="true"
              className="i-ri-error-warning-fill size-4 shrink-0 text-text-destructive"
            />
            <p role="alert" className="system-sm-medium text-text-destructive">
              {error}
            </p>
          </div>
        )}
        {notice && (
          <div className="flex items-center justify-between gap-2.5 rounded-xl border border-state-success-active/60 bg-state-success-hover px-4 py-3 shadow-2xs">
            <div className="flex items-center gap-2.5">
              <span
                aria-hidden="true"
                className="i-ri-checkbox-circle-fill size-4 shrink-0 text-text-success"
              />
              <p role="status" className="system-sm-medium text-text-success">
                {notice}
              </p>
            </div>
            <button
              type="button"
              aria-label="关闭提示"
              onClick={() => setNotice('')}
              className="cursor-pointer rounded-md p-1 text-text-success/75 transition-colors hover:bg-components-panel-bg/60 hover:text-text-success"
            >
              <span aria-hidden="true" className="i-ri-close-line size-4" />
            </button>
          </div>
        )}
        {!data && !error && (
          <div className="aiops-glass-elevated space-y-4 rounded-2xl p-6">
            <div className="flex items-center gap-3">
              <span
                aria-hidden="true"
                className="i-ri-loader-2-line size-5 animate-spin text-text-accent"
              />
              <p role="status" className="system-sm-medium text-text-secondary">
                正在读取环境目录…
              </p>
            </div>
            <div className="grid gap-4 lg:grid-cols-[15rem_minmax(0,1fr)]">
              <div className="space-y-2">
                <div className="h-14 animate-pulse rounded-xl bg-background-section-burn" />
                <div className="h-14 animate-pulse rounded-xl bg-background-section-burn/70" />
              </div>
              <div className="h-32 animate-pulse rounded-xl bg-background-section-burn/60" />
            </div>
          </div>
        )}
        {data && overviewError && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-4 py-3">
            <div className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className="i-ri-error-warning-line size-4 shrink-0 text-text-destructive"
              />
              <p role="alert" className="system-sm-medium text-text-destructive">
                {overviewError}
              </p>
            </div>
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

        {/* 主体双栏：精致左侧环境栏 + 层次分明的右侧工作台 */}
        {data && draft && (
          <div className="grid items-start gap-6 lg:grid-cols-[16.5rem_minmax(0,1fr)]">
            {/* 左侧业务环境列表 */}
            <LiquidGlassSurface
              as="aside"
              variant="rail"
              className="rounded-2xl p-3 lg:sticky lg:top-4"
            >
              <div className="mb-2.5 flex items-center justify-between px-1.5">
                <span className="system-xs-semibold text-text-secondary">
                  业务环境（{rows.length}）
                </span>
                {rows.length > 0 && (
                  <span className="rounded-full bg-background-section-burn px-2 py-0.5 text-[11px] text-text-tertiary tabular-nums">
                    {rows.filter((r) => r.enabled).length} 已启用
                  </span>
                )}
              </div>

              {rows.length > 4 && (
                <div className="relative mb-2.5">
                  <span
                    aria-hidden="true"
                    className="pointer-events-none absolute top-1/2 left-2.5 i-ri-search-line size-3.5 -translate-y-1/2 text-text-tertiary"
                  />
                  <input
                    type="search"
                    aria-label="筛选业务环境"
                    placeholder="搜索环境名称或编号…"
                    value={envQuery}
                    onChange={(event) => setEnvQuery(event.target.value)}
                    className="h-8 w-full rounded-lg border border-divider-subtle bg-background-section-burn/70 pr-7 pl-8 system-xs-regular text-text-primary placeholder:text-text-quaternary focus:border-state-accent-solid focus:bg-components-panel-bg focus:outline-hidden"
                  />
                  {envQuery && (
                    <button
                      type="button"
                      aria-label="清空环境筛选"
                      onClick={() => setEnvQuery('')}
                      className="absolute top-1/2 right-2 -translate-y-1/2 cursor-pointer text-text-tertiary hover:text-text-primary"
                    >
                      <span aria-hidden="true" className="i-ri-close-line size-3.5" />
                    </button>
                  )}
                </div>
              )}

              <nav aria-label="业务环境列表" className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-1">
                {filteredRows.map((row) => {
                  const envSummary = overview?.environments.find((item) => item.id === row.id)
                  const active = selected === row.id
                  const status = envSummary?.status ?? (row.enabled ? 'active' : 'disabled')
                  const totalT = envSummary?.tenant_count ?? row.tenants.length
                  const enabledT = envSummary?.enabled_tenant_count ?? 0
                  const ratio = totalT > 0 ? Math.round((enabledT / totalT) * 100) : 0
                  return (
                    <div
                      key={row.id}
                      className={`relative overflow-hidden rounded-xl border transition-all ${
                        active
                          ? 'aiops-glass-pill border-state-accent-solid/60 bg-state-accent-hover/70 shadow-sm'
                          : 'border-transparent hover:border-divider-subtle hover:bg-background-section-burn/55'
                      }`}
                    >
                      {active && (
                        <span
                          aria-hidden="true"
                          className="absolute inset-y-2 left-0 w-1 rounded-r-full bg-state-accent-solid"
                        />
                      )}
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
                          {envSummary?.is_control && (
                            <span className="shrink-0 rounded bg-components-badge-bg-blue-light-soft px-1.5 py-0.5 text-[10px] font-medium text-text-accent">
                              控制面
                            </span>
                          )}
                        </div>
                        <div className="mt-1.5 flex items-center justify-between gap-2 text-[11px] text-text-tertiary">
                          <span className="inline-flex items-center gap-1.5">
                            <StatusDot status={badgeToneStyles[envTone(status)].dot} size="small" />
                            <span>{environmentStatusLabels[status] || status}</span>
                          </span>
                          <span className="tabular-nums">
                            {envSummary
                              ? `${enabledT}/${totalT} 租户`
                              : `${row.tenants.length} 租户`}
                          </span>
                        </div>
                        {totalT > 0 && envSummary && (
                          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-components-progress-bar-bg">
                            <div
                              className={`h-full rounded-full transition-all ${
                                active ? 'bg-state-accent-solid' : 'bg-state-success-solid/75'
                              }`}
                              style={{ width: `${ratio}%` }}
                            />
                          </div>
                        )}
                      </button>
                    </div>
                  )
                })}
                {!rows.length && (
                  <p className="col-span-full px-3 py-6 text-center system-xs-regular text-text-tertiary">
                    尚未登记环境
                  </p>
                )}
                {rows.length > 0 && filteredRows.length === 0 && (
                  <p className="col-span-full px-3 py-5 text-center system-xs-regular text-text-tertiary">
                    无匹配环境
                  </p>
                )}
              </nav>
            </LiquidGlassSurface>

            {/* 右侧主工作台 */}
            <div className="min-w-0 space-y-5">
              {/* 工作区顶栏 + 4 个主 Tab */}
              <LiquidGlassSurface variant="elevated" className="overflow-hidden rounded-2xl">
                <div className="bg-linear-to-b from-components-panel-gradient-1/25 to-transparent px-5 pt-5 pb-4 sm:px-6">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="space-y-2">
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
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="inline-flex items-center gap-1.5 rounded-md bg-background-section-burn/80 px-2.5 py-1 system-xs-regular text-text-secondary">
                          <span
                            aria-hidden="true"
                            className="i-ri-database-2-line size-3.5 text-text-tertiary"
                          />
                          <span>
                            当前生效目录：
                            {live
                              ? `R${data.registry_revision} · ${live.enabled ? '允许准入' : '已停用'} · ${live.tenants.length} 个租户`
                              : '此环境尚未生效'}
                          </span>
                        </span>
                        <span className="inline-flex items-center gap-1.5 rounded-md bg-background-section-burn/80 px-2.5 py-1 system-xs-regular text-text-secondary">
                          <span
                            aria-hidden="true"
                            className="i-ri-file-copy-line size-3.5 text-text-tertiary"
                          />
                          <span>
                            配置草稿：{saved ? `V${saved.revision}` : '未保存'} ·{' '}
                            {plan && !dirty
                              ? plan.state === 'expired'
                                ? '计划已过期'
                                : '已有预检计划'
                              : '未预检'}
                          </span>
                        </span>
                      </div>
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      {preparing && (
                        <span className="inline-flex items-center gap-1.5 rounded-full border border-divider-accent/50 bg-components-badge-bg-blue-light-soft px-3 py-1 system-xs-medium text-text-accent">
                          <span className="size-2 animate-pulse rounded-full bg-state-accent-solid" />
                          后台执行中 · 自动同步
                        </span>
                      )}
                      <Button
                        type="button"
                        variant={showWizard ? 'secondary' : 'primary'}
                        size="small"
                        onClick={() => setShowWizard((v) => !v)}
                      >
                        <span
                          aria-hidden="true"
                          className={`mr-1.5 size-3.5 ${showWizard ? 'i-ri-close-line' : 'i-ri-magic-line'}`}
                        />
                        {showWizard ? '关闭自助向导' : '5 步自助开通向导'}
                      </Button>
                    </div>
                  </div>

                  {saved?.registry_changed && (
                    <div className="mt-3.5 flex flex-wrap items-center justify-between gap-2 rounded-xl border border-state-warning-active/40 bg-components-badge-bg-orange-soft px-3.5 py-2.5">
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
                </div>

                {/* 4 个主 Tab */}
                <div
                  role="tablist"
                  aria-label="工作区视图切换"
                  className="flex flex-wrap gap-6 border-t border-divider-subtle px-5 pt-1 sm:px-6"
                >
                  {(
                    [
                      [
                        'onboard',
                        '开通与租户',
                        'i-ri-rocket-line',
                        activeTasksCount > 0 ? String(activeTasksCount) : '',
                        false,
                      ],
                      ['releases', '版本与发版', 'i-ri-git-branch-line', '', false],
                      [
                        'config',
                        '连接配置',
                        'i-ri-settings-3-line',
                        '',
                        dirty || Boolean(saved?.registry_changed),
                      ],
                      [
                        'ops',
                        '运行与排查',
                        'i-ri-pulse-line',
                        tasks.length > 0 ? String(tasks.length) : '',
                        false,
                      ],
                    ] as const
                  ).map(([tab, label, iconClass, badge, warnDot], idx, list) => {
                    const isCurrent = !showWizard && activeTab === tab
                    return (
                      <button
                        key={tab}
                        id={`aiops-tab-${tab}`}
                        type="button"
                        role="tab"
                        aria-selected={isCurrent}
                        aria-controls={`aiops-panel-${tab}`}
                        tabIndex={isCurrent || (showWizard && idx === 0) ? 0 : -1}
                        onClick={() => {
                          setShowWizard(false)
                          setActiveTab(tab)
                        }}
                        onKeyDown={(event) => {
                          if (event.key === 'ArrowRight') {
                            event.preventDefault()
                            const next = list[(idx + 1) % list.length]?.[0]
                            if (next) {
                              setShowWizard(false)
                              setActiveTab(next)
                            }
                          } else if (event.key === 'ArrowLeft') {
                            event.preventDefault()
                            const prev = list[(idx - 1 + list.length) % list.length]?.[0]
                            if (prev) {
                              setShowWizard(false)
                              setActiveTab(prev)
                            }
                          }
                        }}
                        className={`relative inline-flex cursor-pointer items-center gap-2 py-3 system-sm-medium transition-colors ${
                          isCurrent
                            ? 'text-text-accent'
                            : 'text-text-tertiary hover:text-text-primary'
                        }`}
                      >
                        <span aria-hidden="true" className={`${iconClass} size-4`} />
                        <span>{label}</span>
                        {badge && (
                          <span
                            className={`rounded-full px-1.5 py-0.5 text-[11px] font-medium tabular-nums ${
                              isCurrent
                                ? 'bg-state-accent-hover text-text-accent'
                                : 'bg-background-section-burn text-text-tertiary'
                            }`}
                          >
                            {badge}
                          </span>
                        )}
                        {warnDot && <StatusDot status="warning" size="small" />}
                        {isCurrent && (
                          <span
                            aria-hidden="true"
                            className="absolute inset-x-0 bottom-0 h-[2.5px] rounded-t-full bg-state-accent-solid shadow-[0_0_12px_var(--aiops-glass-edge-glow)]"
                          />
                        )}
                      </button>
                    )
                  })}
                </div>
              </LiquidGlassSurface>

              {showWizard && (
                <OnboardingWizard
                  initialEnvironment={draft?.id || selected || ''}
                  initialName={draft?.name || ''}
                  initialApiDomain={
                    draft?.company_gateway_url
                      ? draft.company_gateway_url.replace(/^https?:\/\//, '')
                      : ''
                  }
                  onClose={() => setShowWizard(false)}
                  onOpenAdvancedConfig={() => {
                    setShowWizard(false)
                    setActiveTab('config')
                  }}
                  onOpenLegacyOnboard={() => {
                    setShowWizard(false)
                    setActiveTab('onboard')
                  }}
                />
              )}

              {/* ================================================================= */}
              {/* TAB 1: 开通与租户（默认首屏 —— 极简、单焦点、一眼知道怎么下手）      */}
              {/* ================================================================= */}
              {!showWizard && activeTab === 'onboard' && (
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
                      className={`aiops-glass-elevated rounded-2xl p-6 ${
                        (readyToPublishOnboard || readyToPublishValidate) && draftReady
                          ? 'border-state-success-active/60 bg-linear-to-br from-components-badge-bg-green-soft/55 via-transparent to-transparent'
                          : ''
                      }`}
                    >
                      <div className="mb-5 border-b border-divider-subtle pb-4">
                        <CompactProgressSteps
                          draftReady={draftReady}
                          hasValidPlan={hasValidPlan}
                          hasReadyOnboard={hasReadyOnboard}
                          hasActivated={hasActivated}
                        />
                      </div>

                      {!selected && !data.can_create ? (
                        <div className="flex items-start gap-3">
                          <span
                            aria-hidden="true"
                            className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-components-badge-bg-gray-soft text-text-tertiary"
                          >
                            <span className="i-ri-information-line size-5" />
                          </span>
                          <div className="space-y-1">
                            <h3 className="system-md-semibold text-text-primary">
                              当前账号暂无已分配的业务环境
                            </h3>
                            <p className="system-xs-regular text-text-secondary">
                              请联系平台目录管理员为当前账号分配环境授权后刷新页面。
                            </p>
                          </div>
                        </div>
                      ) : readyToPublishOnboard && draftReady ? (
                        <div className="flex flex-wrap items-center justify-between gap-4">
                          <div className="flex items-start gap-3">
                            <span
                              aria-hidden="true"
                              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-green-soft text-text-success"
                            >
                              <span className="i-ri-checkbox-circle-fill size-5" />
                            </span>
                            <div className="space-y-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <StatusPill tone="success">技术验收就绪</StatusPill>
                                <h3 className="system-md-semibold text-text-primary">
                                  待人工确认发布：{tenantNameOf(readyToPublishOnboard.tenant_id)}
                                </h3>
                              </div>
                              <p className="system-xs-regular text-text-secondary">
                                四阶段技术验证已通过，复核无误后点击正式开通（业务准确性仍待人工确认）。
                              </p>
                            </div>
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
                          <div className="flex items-start gap-3">
                            <span
                              aria-hidden="true"
                              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-green-soft text-text-success"
                            >
                              <span className="i-ri-shield-check-line size-5" />
                            </span>
                            <div className="space-y-1">
                              <div className="flex flex-wrap items-center gap-2">
                                <StatusPill tone="success">三项能力验证就绪</StatusPill>
                                <h3 className="system-md-semibold text-text-primary">
                                  待人工确认发布：{tenantNameOf(readyToPublishValidate.tenant_id)}
                                </h3>
                              </div>
                              <p className="system-xs-regular text-text-secondary">
                                单步业务技术验证已全部通过，预检计划后可在「运行与排查」发起发布。
                              </p>
                            </div>
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
                          <div className="flex items-start gap-3">
                            <span
                              aria-hidden="true"
                              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-orange-soft text-text-warning"
                            >
                              <span className="i-ri-settings-3-line size-5" />
                            </span>
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
                          <div className="flex items-start gap-3">
                            <span
                              aria-hidden="true"
                              className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-blue-soft text-text-accent"
                            >
                              <span className="i-ri-rocket-2-line size-5" />
                            </span>
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
                          </div>
                          <Button
                            type="button"
                            variant="primary"
                            disabled={busy}
                            onClick={() => void preflight()}
                          >
                            <span
                              aria-hidden="true"
                              className="mr-1.5 i-ri-shield-check-line size-4"
                            />
                            配置与制品预检
                          </Button>
                        </div>
                      )}
                    </section>
                  ) : (
                    /* 预检完成：直接呈现干净的「开通计划与一键开通」操作卡 */
                    <section
                      aria-label="开通计划"
                      className="aiops-glass-elevated overflow-hidden rounded-2xl border-divider-accent/70"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-divider-subtle bg-components-badge-bg-blue-light-soft/45 px-6 py-4">
                        <div className="flex flex-wrap items-center gap-2.5">
                          <span
                            aria-hidden="true"
                            className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-blue-soft text-text-accent"
                          >
                            <span className="i-ri-shield-check-line size-4" />
                          </span>
                          <h3 className="system-md-semibold text-text-primary">开通计划</h3>
                          <StatusPill tone={plan.state === 'expired' ? 'error' : 'info'}>
                            {plan.state === 'expired' ? '计划已过期' : '计划已冻结'}
                          </StatusPill>
                          <span className="rounded-md bg-components-panel-bg/80 px-2 py-0.5 system-xs-regular text-text-tertiary tabular-nums">
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
                            <span aria-hidden="true" className="mr-1 i-ri-refresh-line size-3.5" />
                            重新核对计划
                          </Button>
                        </div>
                      </div>

                      <div className="space-y-5 p-6">
                        <p
                          role="status"
                          className={`rounded-xl border px-3.5 py-2.5 system-xs-medium ${
                            plan.state === 'expired'
                              ? 'border-state-destructive-border bg-state-destructive-hover text-text-destructive'
                              : 'border-divider-subtle bg-background-section-burn/70 text-text-secondary'
                          }`}
                        >
                          {plan.state === 'expired'
                            ? '输入或有效期已变化，请重新预检。'
                            : '计划已冻结；可选择模板直接一键开通，正式启用仍需人工确认。'}
                        </p>

                        {plan.checks.length > 0 &&
                          (() => {
                            const passedCount = plan.checks.filter(
                              (c) => c.state === 'passed',
                            ).length
                            const blockedCount = plan.checks.filter(
                              (c) => c.state === 'blocked',
                            ).length
                            const pendingCount = plan.checks.length - passedCount - blockedCount
                            return (
                              <div className="space-y-2.5">
                                <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-text-tertiary">
                                  <span className="font-medium text-text-secondary">
                                    本地与预检核对项（共 {plan.checks.length} 项）
                                  </span>
                                  <span className="tabular-nums">
                                    通过 {passedCount} · 阻塞 {blockedCount} · 待验证 {pendingCount}
                                  </span>
                                </div>
                                <ul className="flex flex-wrap gap-2">
                                  {plan.checks.map((check) => {
                                    const { tone, label, stateText } = checkDisplay(check)
                                    return (
                                      <li
                                        key={check.id}
                                        className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 system-xs-regular ${badgeToneStyles[tone].wrap}`}
                                      >
                                        <StatusDot
                                          status={badgeToneStyles[tone].dot}
                                          size="small"
                                        />
                                        <span>
                                          {label}：{stateText}
                                        </span>
                                      </li>
                                    )
                                  })}
                                </ul>
                              </div>
                            )
                          })()}

                        {/* 主操作：一键开通 */}
                        <section aria-label="一次准备候选" className="space-y-4 pt-1">
                          {consumerSources.length === 0 ? (
                            <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-divider-accent/40 bg-state-accent-hover/50 px-4 py-3.5">
                              <div className="flex items-start gap-3">
                                <span
                                  aria-hidden="true"
                                  className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-components-icon-bg-blue-soft text-text-accent"
                                >
                                  <span className="i-ri-apps-2-line size-4" />
                                </span>
                                <div className="space-y-0.5">
                                  <div className="system-sm-semibold text-text-primary">
                                    下一步：选择客服来源模板，发起一键开通
                                  </div>
                                  <div className="system-xs-regular text-text-secondary">
                                    自动串联准备、停用安装、运行登记与可选技术验证，不自动启用入口。
                                  </div>
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
                            <div className="space-y-4 rounded-xl border border-divider-subtle bg-background-section-burn/55 p-4">
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <div className="flex items-center gap-2">
                                  <span
                                    aria-hidden="true"
                                    className="i-ri-rocket-line size-4 text-text-accent"
                                  />
                                  <h4 className="system-sm-semibold text-text-primary">
                                    一键开通配置
                                  </h4>
                                </div>
                                <Button
                                  type="button"
                                  variant="ghost"
                                  size="small"
                                  disabled={busy || plan.state === 'expired'}
                                  onClick={() => void siteTasks({ consumer_sources: true })}
                                >
                                  <span
                                    aria-hidden="true"
                                    className="mr-1 i-ri-refresh-line size-3.5"
                                  />
                                  刷新模板
                                </Button>
                              </div>

                              <div className="grid gap-3.5 md:grid-cols-2">
                                <div className="space-y-1.5 system-xs-medium text-text-secondary">
                                  <span>候选客服来源版本</span>
                                  <Select
                                    value={consumerSource || null}
                                    onValueChange={(value) => setConsumerSource(value ?? '')}
                                  >
                                    <SelectTrigger
                                      aria-label="候选客服来源版本"
                                      size="large"
                                      className="border border-divider-regular bg-components-panel-bg text-xs"
                                    >
                                      <SelectValue placeholder="请选择已发布模板">
                                        {(() => {
                                          const found = consumerSources.find(
                                            (s) => s.app_id === consumerSource,
                                          )
                                          return found
                                            ? `${found.name} · v${found.version_no}`
                                            : '请选择已发布模板'
                                        })()}
                                      </SelectValue>
                                    </SelectTrigger>
                                    <SelectContent className="w-(--anchor-width)">
                                      {consumerSources.map((source) => (
                                        <SelectItem key={source.app_id} value={source.app_id}>
                                          <SelectItemText>
                                            {source.name} · v{source.version_no}
                                          </SelectItemText>
                                          <SelectItemIndicator />
                                        </SelectItem>
                                      ))}
                                    </SelectContent>
                                  </Select>
                                </div>

                                <label className="block space-y-1.5 system-xs-medium text-text-secondary">
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

                              <label className="inline-flex cursor-pointer items-center gap-2.5 rounded-lg bg-components-panel-bg/80 px-3 py-2 system-xs-regular text-text-secondary">
                                <Checkbox
                                  aria-label="一键开通包含空机初装"
                                  checked={onboardInitial}
                                  onCheckedChange={(value) => setOnboardInitial(value === true)}
                                />
                                <span>包含空机初装（仅首次空白目标主机勾选）</span>
                              </label>

                              {!consumerSource && (
                                <p className="flex items-center gap-2 rounded-lg border border-state-warning-active/40 bg-components-badge-bg-orange-soft px-3 py-2 system-xs-medium text-text-warning">
                                  <StatusDot status="warning" size="small" />
                                  请先选择上方「候选客服来源版本」，再点击租户「一键开通」
                                </p>
                              )}

                              <div className="divide-y divide-divider-subtle overflow-hidden rounded-xl border border-divider-subtle bg-components-panel-bg">
                                {draft.tenants.map((tenant) => {
                                  const tenantOverview = selectedOverview?.tenants.find(
                                    (t) => t.id === tenant.id,
                                  )
                                  const initialChar = (tenant.name || tenant.id || '?').slice(0, 1)
                                  return (
                                    <div
                                      key={`onboard-${tenant.id}`}
                                      className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 transition-colors hover:bg-background-section-burn/40"
                                    >
                                      <div className="flex flex-wrap items-center gap-2.5">
                                        <span
                                          aria-hidden="true"
                                          className="flex size-7 shrink-0 items-center justify-center rounded-full bg-components-icon-bg-blue-soft text-xs font-semibold text-text-accent"
                                        >
                                          {initialChar}
                                        </span>
                                        <div>
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
                                          <div className="font-mono text-[11px] text-text-tertiary">
                                            ID: {tenant.id}
                                          </div>
                                        </div>
                                      </div>
                                      <Button
                                        type="button"
                                        variant={consumerSource ? 'primary' : 'secondary'}
                                        size="medium"
                                        title={
                                          !consumerSource
                                            ? '请先选择上方「候选客服来源版本」'
                                            : undefined
                                        }
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
                          <details className="group rounded-xl border border-divider-subtle bg-background-section-burn/50 px-4 py-2.5">
                            <summary className="flex cursor-pointer list-none items-center justify-between gap-2 system-xs-medium text-text-tertiary select-none hover:text-text-primary [&::-webkit-details-marker]:hidden">
                              <span>高级操作与计划校验摘要（分步准备 / 主机核验 / SHA）</span>
                              <span
                                aria-hidden="true"
                                className="i-ri-arrow-down-s-line size-4 shrink-0 transition-transform group-open:rotate-180"
                              />
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
                      </div>
                    </section>
                  )}

                  {/* 2. 「租户状态」列表（默认隐藏 UUID，一键展开详情） */}
                  {selected && (
                    <section
                      aria-label="租户与能力总览"
                      className="aiops-glass-elevated overflow-hidden rounded-2xl"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-6 py-4">
                        <div className="flex items-center gap-2.5">
                          <span
                            aria-hidden="true"
                            className="flex size-7 items-center justify-center rounded-lg bg-components-icon-bg-green-soft text-text-success"
                          >
                            <span className="i-ri-group-2-line size-4" />
                          </span>
                          <h3 className="system-md-semibold text-text-primary">租户状态</h3>
                          <span className="rounded-full bg-background-section-burn px-2 py-0.5 text-[11px] font-medium text-text-tertiary tabular-nums">
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
                          <span
                            aria-hidden="true"
                            className={`mr-1 i-ri-refresh-line size-3.5 ${overviewBusy ? 'animate-spin' : ''}`}
                          />
                          刷新状态
                        </Button>
                      </div>

                      {selectedOverview && selectedOverview.tenants.length > 0 ? (
                        <ul className="divide-y divide-divider-subtle">
                          {selectedOverview.tenants.map((tenant) => {
                            const expanded = Boolean(expandedTenants[tenant.id])
                            const initialChar = (tenant.name || tenant.id || '?').slice(0, 1)
                            return (
                              <li
                                key={tenant.id}
                                className="px-6 py-4 transition-colors hover:bg-background-section-burn/35"
                              >
                                <div className="flex flex-wrap items-center justify-between gap-3">
                                  <div className="flex items-center gap-3">
                                    <span
                                      aria-hidden="true"
                                      className="flex size-8 shrink-0 items-center justify-center rounded-full bg-components-icon-bg-indigo-soft text-xs font-semibold text-text-accent"
                                    >
                                      {initialChar}
                                    </span>
                                    <div>
                                      <div className="flex flex-wrap items-center gap-2">
                                        <span className="system-sm-semibold text-text-primary">
                                          {tenant.name}
                                        </span>
                                        <StatusPill tone={envTone(tenant.admission)}>
                                          {tenantAdmissionLabels[tenant.admission] ||
                                            tenant.admission}
                                        </StatusPill>
                                      </div>
                                      <div className="mt-0.5 font-mono text-[11px] text-text-tertiary">
                                        租户 {tenant.id}
                                      </div>
                                    </div>
                                  </div>

                                  <div className="flex flex-wrap items-center gap-2.5">
                                    <div className="flex flex-wrap items-center gap-1.5">
                                      {(
                                        [
                                          ['智能客服', tenant.capabilities.consumer],
                                          ['异常订单', tenant.capabilities.order],
                                          ['离线分析', tenant.capabilities.offline],
                                        ] as const
                                      ).map(([label, cap]) => {
                                        const tone = capabilityTone(cap.state)
                                        const boxBg =
                                          tone === 'success'
                                            ? 'border-state-success-active/35 bg-components-badge-bg-green-soft/60 text-text-success'
                                            : tone === 'error'
                                              ? 'border-state-destructive-border bg-components-badge-bg-red-soft/70 text-text-destructive'
                                              : tone === 'warning'
                                                ? 'border-state-warning-active/35 bg-components-badge-bg-orange-soft/60 text-text-warning'
                                                : 'border-divider-subtle bg-background-section-burn/80 text-text-secondary'
                                        return (
                                          <span
                                            key={label}
                                            className={`inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-medium ${boxBg}`}
                                          >
                                            <StatusDot
                                              status={badgeToneStyles[tone].dot}
                                              size="small"
                                            />
                                            <span>
                                              {label}：
                                              {capabilityStateLabels[cap.state] || cap.state}
                                              {cap.error_code ? `（${cap.error_code}）` : ''}
                                            </span>
                                          </span>
                                        )
                                      })}
                                    </div>

                                    {tenant.workspace.id && (
                                      <div className="flex flex-wrap items-center gap-1.5">
                                        <button
                                          type="button"
                                          disabled={workspaceBusyKey !== ''}
                                          onClick={() =>
                                            void openTenantWorkspace(tenant.workspace.id, false)
                                          }
                                          className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-divider-accent/40 bg-components-badge-bg-blue-light-soft px-2.5 py-1 system-xs-medium text-text-accent transition-colors hover:bg-state-accent-hover disabled:cursor-not-allowed disabled:opacity-60"
                                        >
                                          <span
                                            aria-hidden="true"
                                            className="i-ri-apps-2-line size-3.5"
                                          />
                                          <span>
                                            {workspaceBusyKey === `${tenant.workspace.id}:apps`
                                              ? '切换中…'
                                              : '进入工作区'}
                                          </span>
                                        </button>
                                        <button
                                          type="button"
                                          disabled={workspaceBusyKey !== ''}
                                          onClick={() =>
                                            void openTenantWorkspace(tenant.workspace.id, true)
                                          }
                                          className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-divider-subtle bg-components-panel-bg px-2.5 py-1 system-xs-medium text-text-secondary transition-colors hover:border-divider-regular hover:bg-background-section-burn hover:text-text-primary disabled:cursor-not-allowed disabled:opacity-60"
                                        >
                                          <span
                                            aria-hidden="true"
                                            className="i-ri-book-open-line size-3.5"
                                          />
                                          <span>
                                            {workspaceBusyKey === `${tenant.workspace.id}:kb`
                                              ? '打开中…'
                                              : '进入知识库'}
                                          </span>
                                        </button>
                                      </div>
                                    )}

                                    <button
                                      type="button"
                                      aria-expanded={expanded}
                                      onClick={() =>
                                        setExpandedTenants((prev) => ({
                                          ...prev,
                                          [tenant.id]: !prev[tenant.id],
                                        }))
                                      }
                                      className="inline-flex cursor-pointer items-center gap-1 rounded-lg border border-divider-subtle bg-components-panel-bg px-2.5 py-1 system-xs-medium text-text-secondary transition-colors hover:border-divider-regular hover:bg-background-section-burn hover:text-text-primary"
                                    >
                                      <span>{expanded ? '收起详情' : '查看详情'}</span>
                                      <span
                                        aria-hidden="true"
                                        className={`i-ri-arrow-down-s-line size-3.5 transition-transform ${
                                          expanded ? 'rotate-180' : ''
                                        }`}
                                      />
                                    </button>
                                  </div>
                                </div>

                                {expanded && (
                                  <div className="mt-3.5 grid gap-3 rounded-xl border border-divider-subtle bg-background-section-burn/60 p-4 system-xs-regular sm:grid-cols-2 lg:grid-cols-4">
                                    <div className="space-y-1.5 rounded-lg bg-components-panel-bg/80 p-3">
                                      <div className="flex items-center gap-1.5 text-[11px] font-medium text-text-tertiary">
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-key-2-line size-3.5"
                                        />
                                        <span>租户编号</span>
                                      </div>
                                      <CopyableChip value={tenant.id} />
                                    </div>
                                    <div className="space-y-1.5 rounded-lg bg-components-panel-bg/80 p-3">
                                      <div className="flex items-center gap-1.5 text-[11px] font-medium text-text-tertiary">
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-global-line size-3.5"
                                        />
                                        <span>
                                          工作区（
                                          {workspaceStateLabels[tenant.workspace.state] ||
                                            tenant.workspace.state}
                                          ）
                                        </span>
                                      </div>
                                      {tenant.workspace.id ? (
                                        <CopyableChip value={tenant.workspace.id} />
                                      ) : (
                                        <span className="text-text-tertiary">未创建</span>
                                      )}
                                    </div>
                                    <div className="space-y-1.5 rounded-lg bg-components-panel-bg/80 p-3">
                                      <div className="flex items-center gap-1.5 text-[11px] font-medium text-text-tertiary">
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-apps-2-line size-3.5"
                                        />
                                        <span>双端应用</span>
                                      </div>
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
                                    <div className="space-y-1.5 rounded-lg bg-components-panel-bg/80 p-3">
                                      <div className="flex items-center gap-1.5 text-[11px] font-medium text-text-tertiary">
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-book-open-line size-3.5"
                                        />
                                        <span>知识空间与业务验收</span>
                                      </div>
                                      <div className="space-y-0.5 text-text-secondary">
                                        <div>
                                          {tenant.knowledge.knowledge_base_id
                                            ? `已连接（${tenant.knowledge.active_grants} 项授权）`
                                            : '未准备'}
                                        </div>
                                        <div className="text-[11px] text-text-tertiary">
                                          业务准确性：待人工确认
                                        </div>
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
                      className="aiops-glass-elevated overflow-hidden rounded-2xl"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-6 py-4">
                        <div className="flex items-center gap-2.5">
                          <span
                            aria-hidden="true"
                            className="flex size-7 items-center justify-center rounded-lg bg-components-icon-bg-indigo-soft text-text-accent"
                          >
                            <span className="i-ri-history-line size-4" />
                          </span>
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
                            <span aria-hidden="true" className="mr-1 i-ri-refresh-line size-3.5" />
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
                        <div className="flex flex-col items-center justify-center gap-1.5 px-6 py-8 text-center">
                          <span
                            aria-hidden="true"
                            className="i-ri-history-line size-6 text-text-quaternary"
                          />
                          <p className="system-xs-regular text-text-tertiary">
                            {tasks.length === 0
                              ? '暂无开通记录，完成上方预检后即可一键开通。'
                              : '当前无进行中或近期一键开通记录，历史单步记录请前往「运行与排查」查看。'}
                          </p>
                        </div>
                      ) : (
                        <div className="divide-y divide-divider-subtle">
                          {focusedLifecycleTasks.map((item) => {
                            const highlightedInHero =
                              (!plan || dirty) &&
                              draftReady &&
                              readyToPublishOnboard?.id === item.id
                            const phase = String(item.result?.phase || '')
                            const phaseLabel = publicationPhases[phase] || phase
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
                                      <span
                                        aria-hidden="true"
                                        className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-background-section-burn text-text-secondary"
                                      >
                                        <span className={`${taskActionIcon(item.action)} size-4`} />
                                      </span>
                                      <span className="system-sm-semibold text-text-primary">
                                        {item.action === 'tenant-onboard'
                                          ? `一键开通 · ${tenantNameOf(item.tenant_id)}${
                                              item.state === 'observed' &&
                                              item.result.ready_to_publish
                                                ? '（技术验收已就绪，待人工确认发布）'
                                                : ''
                                            }`
                                          : item.action === 'environment-publish'
                                            ? phase === 'activated'
                                              ? `正式开通 · ${tenantNameOf(item.tenant_id)}（准入 ${item.result?.activated ? '已启用' : '未启用'}）`
                                              : `正式开通 · ${tenantNameOf(item.tenant_id)}（${phaseLabel} · 准入 ${item.result?.activated ? '已启用' : '未启用'}）`
                                            : `${taskActionLabels[item.action || ''] || '环境任务'}${item.tenant_id ? ` · ${tenantNameOf(item.tenant_id)}` : ''}`}
                                      </span>
                                      <StatusPill tone={taskTone(item.state)}>
                                        {item.action === 'environment-publish' && phase
                                          ? phaseLabel
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
                                      <ul className="grid gap-2.5 sm:grid-cols-2 xl:grid-cols-4">
                                        {(item.result.stages || []).map((stage, stageIndex) => (
                                          <li
                                            key={stage.action}
                                            className={`flex items-center justify-between gap-2 rounded-xl border px-3 py-2.5 system-xs-regular ${stageBoxClass(stage.state)}`}
                                          >
                                            <span className="flex items-center gap-2 font-medium text-text-primary">
                                              <span className="font-mono text-[10px] text-text-tertiary">
                                                0{stageIndex + 1}
                                              </span>
                                              <StatusDot
                                                status={badgeToneStyles[taskTone(stage.state)].dot}
                                                size="small"
                                              />
                                              <span>
                                                {onboardingStageLabels[stage.action] ||
                                                  stage.action}
                                              </span>
                                            </span>
                                            <span className="text-[11px] font-medium text-text-secondary">
                                              {taskLabels[stage.state] || stage.state}
                                            </span>
                                          </li>
                                        ))}
                                      </ul>
                                    )}
                                </section>

                                <details className="group text-text-tertiary">
                                  <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[11px] select-none hover:text-text-primary [&::-webkit-details-marker]:hidden">
                                    <span>查看任务编号与技术详情</span>
                                    <span
                                      aria-hidden="true"
                                      className="i-ri-arrow-down-s-line size-3.5 transition-transform group-open:rotate-180"
                                    />
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
              {/* TAB: 版本与发版（Day-2 已上线客户机无损升级、入口同步与安全回滚）   */}
              {/* ================================================================= */}
              {!showWizard && activeTab === 'releases' && selected && (
                <EnvironmentReleasesPanel
                  environment={selected}
                  environmentName={draft?.name || selected}
                  isControl={Boolean(selectedOverview?.is_control || selected === '41')}
                  onOpenWizard={() => setShowWizard(true)}
                />
              )}

              {/* ================================================================= */}
              {/* TAB 2: 连接配置（专注表单编辑，仅此处包裹 <form onSubmit>）          */}
              {/* ================================================================= */}
              {!showWizard && activeTab === 'config' && (
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
                    className="aiops-glass-elevated divide-y divide-divider-subtle overflow-hidden rounded-2xl"
                  >
                    <legend className="sr-only">连接配置</legend>

                    <div className="flex flex-wrap items-center justify-between gap-3 bg-background-section-burn/45 px-6 py-4">
                      <div className="flex items-center gap-2.5">
                        <span
                          aria-hidden="true"
                          className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-blue-soft text-text-accent"
                        >
                          <span className="i-ri-settings-3-line size-4" />
                        </span>
                        <div>
                          <h3 className="system-md-semibold text-text-primary">连接配置</h3>
                          <p className="mt-0.5 system-xs-regular text-text-tertiary">
                            维护环境端点、受管引用与租户清单；保存仅更新草稿，不影响现役运行状态。
                          </p>
                        </div>
                      </div>
                      {dirty && <StatusPill tone="warning">存在未保存修改</StatusPill>}
                    </div>

                    {fieldGroups.map((group) => {
                      const groupIcon =
                        group.title === '基础标识'
                          ? 'i-ri-information-line'
                          : group.title === 'HTTPS 服务端点'
                            ? 'i-ri-global-line'
                            : 'i-ri-key-2-line'
                      return (
                        <div key={group.title} className="space-y-4 px-6 py-5">
                          <div className="flex items-center gap-2">
                            <span
                              aria-hidden="true"
                              className={`${groupIcon} size-4 text-text-accent`}
                            />
                            <div>
                              <div className="system-sm-semibold text-text-primary">
                                {group.title}
                              </div>
                              <div className="system-xs-regular text-text-tertiary">
                                {group.description}
                              </div>
                            </div>
                          </div>
                          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                            {group.items.map(([key, label, maxLength, placeholder]) => {
                              const locked = key === 'id' && Boolean(selected)
                              return (
                                <label key={key} className="block space-y-1.5">
                                  <span className="flex items-center justify-between gap-2 system-xs-medium text-text-secondary">
                                    <span>{label}</span>
                                    {locked && (
                                      <span className="inline-flex items-center gap-1 rounded bg-background-section-burn px-1.5 py-0.5 text-[10px] text-text-tertiary">
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-lock-line size-3"
                                        />
                                        已锁定
                                      </span>
                                    )}
                                  </span>
                                  <Input
                                    aria-label={label}
                                    required={key !== 'company_gateway_url'}
                                    maxLength={maxLength}
                                    placeholder={placeholder}
                                    disabled={locked}
                                    value={draft[key]}
                                    onChange={(event) =>
                                      setDraft({ ...draft, [key]: event.target.value })
                                    }
                                  />
                                </label>
                              )
                            })}
                          </div>
                        </div>
                      )
                    })}

                    <div className="px-6 py-4">
                      <label className="inline-flex cursor-pointer items-center gap-2.5 rounded-xl border border-divider-subtle bg-background-section-burn/50 px-3.5 py-2.5 system-sm-medium text-text-primary transition-colors hover:bg-background-section-burn">
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
                    <section aria-label="租户草稿" className="space-y-4 px-6 py-5">
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <span
                            aria-hidden="true"
                            className="i-ri-group-2-line size-4 text-text-accent"
                          />
                          <div>
                            <h3 className="system-sm-semibold text-text-primary">
                              租户清单（{draft.tenants.length}）
                            </h3>
                            <p className="system-xs-regular text-text-tertiary">
                              新增租户默认不勾选准入
                            </p>
                          </div>
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
                          <span aria-hidden="true" className="mr-1 i-ri-add-line size-3.5" />
                          新增租户
                        </Button>
                      </div>

                      {draft.tenants.length === 0 ? (
                        <p className="rounded-xl border border-dashed border-divider-regular bg-background-section-burn/45 py-6 text-center system-xs-regular text-text-tertiary">
                          暂无租户，点击「新增租户」添加
                        </p>
                      ) : (
                        <div className="space-y-3">
                          {draft.tenants.map((tenant, index) => {
                            const persisted = Boolean(
                              (saved?.draft.tenants ?? live?.tenants)?.[index],
                            )
                            return (
                              <div
                                // oxlint-disable-next-line react/no-array-index-key -- Tenant IDs are user-editable before saving.
                                key={index}
                                className="grid items-end gap-3.5 rounded-xl border border-divider-subtle bg-background-section-burn/45 p-4 transition-colors hover:border-divider-regular md:grid-cols-[auto_minmax(0,1fr)_minmax(0,1fr)_auto_auto]"
                              >
                                <span
                                  aria-hidden="true"
                                  className="hidden h-9 w-7 items-center justify-center rounded-lg bg-components-panel-bg font-mono text-xs font-semibold text-text-tertiary md:flex"
                                >
                                  {index + 1}
                                </span>
                                <label className="block space-y-1">
                                  <span className="flex items-center justify-between gap-2 system-xs-medium text-text-secondary">
                                    <span>租户 {index + 1} 编号</span>
                                    {persisted && (
                                      <span className="inline-flex items-center gap-0.5 text-[10px] text-text-tertiary">
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-lock-line size-3"
                                        />
                                        已登记
                                      </span>
                                    )}
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
                                <label className="flex h-9 cursor-pointer items-center gap-2 rounded-lg bg-components-panel-bg/80 px-3 system-xs-medium text-text-secondary">
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
                                    tone="destructive"
                                    onClick={() =>
                                      setDraft({
                                        ...draft,
                                        tenants: draft.tenants.filter((_, i) => i !== index),
                                      })
                                    }
                                  >
                                    <span
                                      aria-hidden="true"
                                      className="mr-1 i-ri-delete-bin-line size-3.5"
                                    />
                                    移除
                                  </Button>
                                )}
                              </div>
                            )
                          })}
                        </div>
                      )}
                    </section>

                    <footer className="flex flex-wrap items-center justify-between gap-3 bg-background-section-burn/45 px-6 py-4">
                      <span className="system-xs-regular text-text-tertiary">
                        {dirty
                          ? '当前有未保存修改，保存后可点击「配置与制品预检」'
                          : '保存后点「配置与制品预检」即可进入一键开通'}
                      </span>
                      <div className="flex flex-wrap gap-2">
                        {dirty && original && (
                          <Button
                            type="button"
                            variant="ghost"
                            onClick={() => {
                              try {
                                setDraft(JSON.parse(original) as Draft)
                                setError('')
                              } catch {
                                // 忽略非法 JSON
                              }
                            }}
                          >
                            还原修改
                          </Button>
                        )}
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
              {!showWizard && activeTab === 'ops' && (
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
                      className="aiops-glass-elevated rounded-2xl p-6"
                    >
                      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
                        <div className="flex items-center gap-2.5">
                          <span
                            aria-hidden="true"
                            className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-blue-soft text-text-accent"
                          >
                            <span className="i-ri-cpu-line size-4" />
                          </span>
                          <div>
                            <h3 className="system-md-semibold text-text-primary">运行节点与工具</h3>
                            <p className="mt-0.5 system-xs-regular text-text-tertiary">
                              {selectedOverview?.is_control
                                ? '实时执行节点心跳与工具兼容性'
                                : '远端环境受管任务点时回执（非实时心跳）'}
                            </p>
                          </div>
                        </div>
                        <Button
                          type="button"
                          variant="secondary"
                          size="small"
                          disabled={busy || overviewBusy}
                          onClick={() => void refreshOverview()}
                        >
                          <span
                            aria-hidden="true"
                            className={`mr-1 i-ri-refresh-line size-3.5 ${overviewBusy ? 'animate-spin' : ''}`}
                          />
                          刷新运行状态
                        </Button>
                      </div>

                      {!selectedOverview || selectedOverview.nodes.length === 0 ? (
                        <p className="rounded-xl bg-background-section-burn/60 py-6 text-center system-xs-regular text-text-tertiary">
                          {selectedOverview?.is_control
                            ? '暂无在线节点心跳记录'
                            : '暂无受管任务节点回执'}
                        </p>
                      ) : (
                        <div className="grid gap-3 md:grid-cols-2">
                          {selectedOverview.nodes.map((node) => (
                            <div
                              key={`${node.source}-${node.node_id}`}
                              className="space-y-2.5 rounded-xl border border-divider-subtle bg-background-section-burn/50 p-4 system-xs-regular"
                            >
                              <div className="flex flex-wrap items-center justify-between gap-2">
                                <div className="flex items-center gap-2">
                                  <span
                                    aria-hidden="true"
                                    className="i-ri-server-line size-4 text-text-secondary"
                                  />
                                  <span className="font-mono system-sm-semibold text-text-primary">
                                    {node.node_id}
                                  </span>
                                </div>
                                <StatusPill tone={nodeTone(node.status)}>
                                  {nodeStatusLabels[node.status] || node.status}
                                </StatusPill>
                              </div>
                              <div className="grid grid-cols-2 gap-2 rounded-lg bg-components-panel-bg/80 px-3 py-2 text-text-secondary">
                                <span>版本：{node.version || '—'}</span>
                                <span>观测：{formatTimestamp(node.observed_at)}</span>
                              </div>
                              {node.source === 'heartbeat' ? (
                                <div className="space-y-2">
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
                                    <details className="group">
                                      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[11px] text-text-tertiary select-none hover:text-text-primary [&::-webkit-details-marker]:hidden">
                                        <span>展开工具清单（{node.available_tools.length}）</span>
                                        <span
                                          aria-hidden="true"
                                          className="i-ri-arrow-down-s-line size-3.5 transition-transform group-open:rotate-180"
                                        />
                                      </summary>
                                      <div className="mt-2 flex flex-wrap gap-1.5">
                                        {node.available_tools.map((tool) => (
                                          <span
                                            key={tool}
                                            className="rounded-md border border-divider-subtle bg-components-panel-bg px-2 py-0.5 font-mono text-[11px] text-text-secondary"
                                          >
                                            {tool}
                                          </span>
                                        ))}
                                      </div>
                                    </details>
                                  )}
                                </div>
                              ) : (
                                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-text-tertiary">
                                  <span>主机：{node.remote_hostname || '—'}</span>
                                  {node.installation && (
                                    <span>
                                      安装：
                                      {initialInstallationLabels[node.installation] ||
                                        node.installation}
                                    </span>
                                  )}
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
                      className="aiops-glass-elevated overflow-hidden rounded-2xl"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle px-6 py-4">
                        <div className="flex items-center gap-2.5">
                          <span
                            aria-hidden="true"
                            className="flex size-7 items-center justify-center rounded-lg bg-components-icon-bg-indigo-soft text-text-accent"
                          >
                            <span className="i-ri-server-line size-4" />
                          </span>
                          <h3 className="system-md-semibold text-text-primary">环境总览</h3>
                          <span className="rounded-md bg-background-section-burn px-2 py-0.5 system-xs-regular text-text-tertiary tabular-nums">
                            目录 R{overview.registry_revision}
                            {overview.control_version ? ` · v${overview.control_version}` : ''}
                          </span>
                        </div>
                      </div>
                      <div className="overflow-x-auto">
                        <table className="w-full text-left system-xs-regular">
                          <thead>
                            <tr className="border-b border-divider-subtle bg-background-section-burn/55 text-text-tertiary">
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
                                      ? 'bg-state-accent-hover/65'
                                      : 'hover:bg-components-panel-on-panel-item-bg-hover'
                                  }`}
                                >
                                  <td className="px-6 py-3 font-medium text-text-primary">
                                    <div className="flex items-center gap-2">
                                      <span>
                                        {env.name}（{env.id}）{env.is_control ? ' · 控制环境' : ''}
                                      </span>
                                      {isCurrent && (
                                        <span className="rounded bg-components-badge-bg-blue-light-soft px-1.5 py-0.5 text-[10px] font-medium text-text-accent">
                                          当前
                                        </span>
                                      )}
                                    </div>
                                  </td>
                                  <td className="px-3 py-3">
                                    <StatusPill tone={envTone(env.status)}>
                                      {environmentStatusLabels[env.status] || env.status}
                                    </StatusPill>
                                  </td>
                                  <td className="px-3 py-3 text-text-primary tabular-nums">
                                    {env.enabled_tenant_count}/{env.tenant_count}
                                  </td>
                                  <td className="px-3 py-3 text-text-secondary">
                                    {summarizeNodeCell(env)}
                                  </td>
                                  <td className="px-6 py-3 text-text-secondary tabular-nums">
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
                      className="aiops-glass-elevated space-y-4 rounded-2xl p-6"
                    >
                      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-divider-subtle pb-4">
                        <div className="flex items-center gap-2.5">
                          <span
                            aria-hidden="true"
                            className="flex size-8 items-center justify-center rounded-lg bg-components-icon-bg-indigo-soft text-text-accent"
                          >
                            <span className="i-ri-terminal-box-line size-4" />
                          </span>
                          <h3 className="system-md-semibold text-text-primary">环境准备任务</h3>
                          <span className="rounded-full bg-background-section-burn px-2.5 py-0.5 system-xs-medium text-text-tertiary tabular-nums">
                            共 {tasks.length} 条
                          </span>
                        </div>
                        <Button
                          type="button"
                          variant="secondary"
                          size="small"
                          onClick={() => void siteTasks()}
                        >
                          <span aria-hidden="true" className="mr-1 i-ri-refresh-line size-3.5" />
                          刷新任务
                        </Button>
                      </div>

                      {draftReady && (!plan || plan.state === 'expired') && tasks.length > 0 && (
                        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-divider-subtle bg-background-section-burn/70 px-4 py-2.5">
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
                        <div className="flex flex-wrap gap-2">
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
                              className={`cursor-pointer rounded-full border px-3 py-1 system-xs-medium tabular-nums transition-colors ${
                                taskFilter === key
                                  ? 'border-state-accent-solid bg-state-accent-hover text-text-accent shadow-2xs'
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
                        <div className="flex flex-col items-center justify-center gap-2 rounded-xl bg-background-section-burn/50 py-8 text-center">
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

                      <div className="space-y-3">
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
                            <article
                              key={item.id}
                              className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4 transition-colors hover:border-divider-regular"
                            >
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
                                          className={`space-y-1 rounded-xl border p-2.5 system-xs-regular ${stageBoxClass(stage.state)}`}
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
                                            className="flex cursor-pointer items-center gap-2 system-xs-regular text-text-secondary"
                                          >
                                            <Checkbox
                                              aria-label={label}
                                              checked={capabilities.includes(key)}
                                              onCheckedChange={(checked) =>
                                                setCapabilities((values) =>
                                                  checked === true
                                                    ? values.includes(key)
                                                      ? values
                                                      : [...values, key]
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
                                  {isFullValidationReady(item) && (
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
                                          className={`rounded-xl border p-2.5 system-xs-regular ${stageBoxClass(step.state)}`}
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
                                    <p className="rounded-md border border-divider-subtle bg-components-panel-bg px-2.5 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                      工作区编号：{item.result.workspace_id}
                                    </p>
                                  )}
                                  {item.result.knowledge_base_id && (
                                    <p className="rounded-md border border-divider-subtle bg-components-panel-bg px-2.5 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                      知识库编号：{item.result.knowledge_base_id}
                                    </p>
                                  )}
                                  {item.result.connection_id && (
                                    <p className="rounded-md border border-divider-subtle bg-components-panel-bg px-2.5 py-0.5 font-mono text-[11px] break-all text-text-secondary">
                                      连接编号：{item.result.connection_id}
                                    </p>
                                  )}
                                  {item.result.app_id && (
                                    <p className="rounded-md border border-divider-subtle bg-components-panel-bg px-2.5 py-0.5 font-mono text-[11px] break-all text-text-secondary">
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
                                        <div className="min-w-52 flex-1 space-y-1 system-xs-medium text-text-secondary">
                                          <span>消费者来源模板：{item.tenant_id}</span>
                                          <Select
                                            value={consumerSource || null}
                                            onValueChange={(value) =>
                                              setConsumerSource(value ?? '')
                                            }
                                          >
                                            <SelectTrigger
                                              aria-label={`消费者来源模板：${item.tenant_id}`}
                                              size="medium"
                                              className="border border-divider-regular bg-components-panel-bg text-xs"
                                            >
                                              <SelectValue placeholder="请选择已发布模板">
                                                {(() => {
                                                  const found = consumerSources.find(
                                                    (s) => s.app_id === consumerSource,
                                                  )
                                                  return found
                                                    ? `${found.name} · v${found.version_no}`
                                                    : '请选择已发布模板'
                                                })()}
                                              </SelectValue>
                                            </SelectTrigger>
                                            <SelectContent className="w-(--anchor-width)">
                                              {consumerSources.map((source) => (
                                                <SelectItem
                                                  key={source.app_id}
                                                  value={source.app_id}
                                                >
                                                  <SelectItemText>
                                                    {source.name} · v{source.version_no}
                                                  </SelectItemText>
                                                  <SelectItemIndicator />
                                                </SelectItem>
                                              ))}
                                            </SelectContent>
                                          </Select>
                                        </div>
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
