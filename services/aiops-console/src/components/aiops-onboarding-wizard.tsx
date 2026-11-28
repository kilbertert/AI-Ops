import { Button } from './ui/button'
import { Checkbox } from './ui/checkbox'
import { Input } from './ui/input'
import { StatusDot } from './ui/status-dot'
import { useEffect, useState } from 'react'
import { manage } from '../lib/manage'
import { LiquidGlassSurface } from './liquid-glass'

type WizardStep = 1 | 2 | 3 | 4 | 5
type HostEnrollMode = 'ephemeral_password' | 'bootstrap_script'

export const WIZARD_DRAFT_STORAGE_KEY = 'aiops-onboarding-wizard-draft:v1'

type HostView = {
  environment: string
  host_alias: string
  address: string
  ssh_port: number
  ssh_user: string
  host_fingerprint_sha256: string
  enrollment_mode: string
  status: string
  hostname: string
  python_minor: string
  service_user_present: boolean
  venv_ready: boolean
  nginx_flavor: string
  rotate_root_password_recommended: boolean
}

type DiscoveryCheck = {
  id: string
  category: string
  state: 'passed' | 'blocked' | 'warning'
  summary: string
}

type DiscoveryReport = {
  environment: string
  ready: boolean
  state: string
  host: {
    hostname: string
    python_minor: string
    timezone: string
    nginx_flavor: string
    nginx_extension_dir: string
  }
  checks: DiscoveryCheck[]
  db_remediation_plan: {
    has_auto_fixable_db_gaps: boolean
    remediation_ids: string[]
    sql_statements: string[]
    confirmation_digest: string
  }
  discovered: {
    tenants: Array<{ id: string; name: string; code?: string; status?: string }>
    healthy_tsdata_url: string | null
    sample_candidates: {
      orders: Array<{ order_no: string; device_id?: string }>
      offline_devices: Array<{ device_id: string; event_count?: number }>
      consumer_orders: Array<{ order_no: string; user_id_suffix?: string }>
    }
  }
}

type InfraResult = {
  job_id?: string
  state?: string
  provisioned?: boolean
  environment: string
  passed: boolean
  failed_step: string
  consumer_kb_id?: string
  operator_kb_id?: string
  tenant_id?: string
  gateway_url?: string
  request?: {
    egress_ip?: string
    api_domain?: string
    gateway_url?: string
    tenant_id?: string
    tenant_name?: string
    customer_name?: string
    gateway_internal_addr?: string
  }
  steps: Array<{
    step: string
    state: string
    reason?: string
    anon_assistant_status?: number
    upms_tenant_list_status?: number
  }>
}

type PublishResult = {
  job_id?: string
  state?: string
  environment?: string
  tenant_id?: string
  gateway_url?: string
  passed?: boolean
  published?: boolean
  failed_step?: string
  request?: {
    tenant_id?: string
    tenant_name?: string
    customer_name?: string
    gateway_url?: string
  }
  result?: {
    published?: boolean
    tenant_id?: string
    gateway_url?: string
    registry_revision?: number
  }
  steps?: Array<{ step: string; state: string; reason?: string }>
}

type WizardNonSecretDraft = {
  step: WizardStep
  customerName: string
  envId: string
  serverIp: string
  apiDomain: string
  adminDomain: string
  internalGateway: string
  sshPort: string
  sshUser: string
  enrollMode: HostEnrollMode
  selectedTenantId: string
  selectedTenantName: string
  consumerKbId: string
  operatorKbId: string
  starterInstalled: boolean
  orderNo: string
  deviceId: string
  consumerOrderNo: string
}

export function checkOperatorTokenTenant(rawToken: string, expectedTenantId: string): string {
  const cleaned = rawToken
    .trim()
    .replace(/^Bearer\s+/i, '')
    .trim()
  if (!cleaned) return ''
  const parts = cleaned.split('.')
  if (parts.length !== 3 || !parts[1]) return ''
  try {
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padLen = (4 - (b64.length % 4)) % 4
    const decoded = atob(b64 + '='.repeat(padLen))
    const claims = JSON.parse(decoded) as Record<string, unknown>
    if (!claims || typeof claims !== 'object') return ''
    const rawTid = claims.tenant_id ?? claims.tenantId
    if (rawTid === undefined || rawTid === null || String(rawTid).trim() === '') return ''
    const tokenTid = String(rawTid).trim()
    if (tokenTid === '-1') {
      return '当前令牌属于系统超级管理员（tenant_id=-1），将被客户侧网关拦截；请改用目标业务租户下的管家账号登录客户后台后再提取令牌。'
    }
    const targetTid = expectedTenantId.trim()
    if (targetTid && tokenTid !== targetTid) {
      return `当前令牌所属租户为 ${tokenTid}，与选定目标租户 ${targetTid} 不一致；请切换为目标租户账号后再提取令牌。`
    }
    return ''
  } catch {
    return ''
  }
}

function readSavedDraft(): Partial<WizardNonSecretDraft> | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(WIZARD_DRAFT_STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? (parsed as Partial<WizardNonSecretDraft>) : null
  } catch {
    return null
  }
}

function arrayBufferToBase64(buf: ArrayBuffer): string {
  const bytes = new Uint8Array(buf)
  const chunks: string[] = []
  const chunkSize = 0x2000
  for (let i = 0; i < bytes.length; i += chunkSize) {
    chunks.push(String.fromCharCode(...bytes.subarray(i, i + chunkSize)))
  }
  return btoa(chunks.join(''))
}

function buildRecentOfflineWindow(days = 7): { start: string; end: string } {
  const fmt = (ms: number) => {
    const offsetMs = 8 * 3600 * 1000
    const d = new Date(ms + offsetMs)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}+08:00`
  }
  const now = Date.now()
  return { start: fmt(now - days * 86400 * 1000), end: fmt(now) }
}

type SelfValidationResult = {
  environment: string
  tenant_id: string
  passed: boolean
  capabilities: Record<string, { state: string; run_id?: string }>
  credentials_persisted: boolean
  operator_session_revoked: boolean
  business_accuracy: string
}

type SmokeResult = {
  gateway_url: string
  tenant_id: string
  passed: boolean
  operator_session_revoked?: boolean
  checks: Array<{
    id: string
    passed: boolean
    actual_http?: number
    expected_http?: number
    qa_id?: string
  }>
  business_accuracy: string
}

export type OnboardingWizardProps = {
  initialEnvironment?: string
  initialName?: string
  initialApiDomain?: string
  onClose?: () => void
  onOpenAdvancedConfig?: () => void
  onOpenLegacyOnboard?: () => void
}

const STEP_LABELS: Array<{ id: WizardStep; title: string; subtitle: string }> = [
  { id: 1, title: '1. 接入主机', subtitle: '5 行输入 · 双模式引导' },
  { id: 2, title: '2. 体检与修复', subtitle: '自动发现 · 二次确认修库' },
  { id: 3, title: '3. 基建与部署', subtitle: '制品池 · 白名单 · Nginx' },
  { id: 4, title: '4. 知识与自验', subtitle: '传文档 · 免 JSON 三验证' },
  { id: 5, title: '5. 发布与终验', subtitle: '公网冒烟 · 交付回执' },
]

export default function OnboardingWizard({
  initialEnvironment = '',
  initialName = '',
  initialApiDomain = '',
  onClose,
  onOpenAdvancedConfig,
  onOpenLegacyOnboard,
}: OnboardingWizardProps) {
  const [savedDraft] = useState<Partial<WizardNonSecretDraft> | null>(() => readSavedDraft())

  const [step, setStep] = useState<WizardStep>(
    savedDraft?.step && [1, 2, 3, 4, 5].includes(savedDraft.step) ? savedDraft.step : 1,
  )
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  // Step 1: 运维手里真实的 5 行输入（只保存非敏感字段到 sessionStorage 以支持刷新恢复）
  const [customerName, setCustomerName] = useState(savedDraft?.customerName || initialName)
  const [envId, setEnvId] = useState(savedDraft?.envId || initialEnvironment)
  const [serverIp, setServerIp] = useState(savedDraft?.serverIp || '')
  const [apiDomain, setApiDomain] = useState(savedDraft?.apiDomain || initialApiDomain)
  const [adminDomain, setAdminDomain] = useState(savedDraft?.adminDomain || '')
  const [internalGateway, setInternalGateway] = useState(savedDraft?.internalGateway || '')
  const [sshPort, setSshPort] = useState(savedDraft?.sshPort || '22')
  const [sshUser, setSshUser] = useState(savedDraft?.sshUser || 'root')
  const [enrollMode, setEnrollMode] = useState<HostEnrollMode>(
    savedDraft?.enrollMode === 'bootstrap_script' ? 'bootstrap_script' : 'ephemeral_password',
  )
  const [rootPassword, setRootPassword] = useState('')
  const [callbackBaseUrl, setCallbackBaseUrl] = useState('https://api.mall.qushiyun.com')
  const [bootstrapOneLiner, setBootstrapOneLiner] = useState('')
  const [enrolledHost, setEnrolledHost] = useState<HostView | null>(null)

  // Step 2: 自动探测与受限库修复（所有数据库/Redis 口令仅内存瞬时传递，用完即清空）
  const [discovery, setDiscovery] = useState<DiscoveryReport | null>(null)
  const [selectedTenantId, setSelectedTenantId] = useState(savedDraft?.selectedTenantId || '')
  const [selectedTenantName, setSelectedTenantName] = useState(savedDraft?.selectedTenantName || '')
  const [mysqlHost, setMysqlHost] = useState('')
  const [mysqlPort, setMysqlPort] = useState('3306')
  const [mysqlUser, setMysqlUser] = useState('aiops_ro')
  const [mysqlPassword, setMysqlPassword] = useState('')
  const [redisHost, setRedisHost] = useState('')
  const [redisPort, setRedisPort] = useState('6379')
  const [redisPassword, setRedisPassword] = useState('')
  const [adminMysqlHost, setAdminMysqlHost] = useState('')
  const [adminMysqlPort, setAdminMysqlPort] = useState('3306')
  const [adminMysqlUser, setAdminMysqlUser] = useState('')
  const [adminMysqlPassword, setAdminMysqlPassword] = useState('')
  const [confirmDbFix, setConfirmDbFix] = useState(false)
  const [dbFixed, setDbFixed] = useState(false)

  // Step 3: 制品池与基建编排
  const [poolCount, setPoolCount] = useState<number | null>(null)
  const [infraResult, setInfraResult] = useState<InfraResult | null>(null)

  // Step 4: 内置知识库上传与免 JSON 自验
  const [consumerKbId, setConsumerKbId] = useState(savedDraft?.consumerKbId || '')
  const [operatorKbId, setOperatorKbId] = useState(savedDraft?.operatorKbId || '')
  const [starterInstalled, setStarterInstalled] = useState(Boolean(savedDraft?.starterInstalled))
  const [consumerDocStatus, setConsumerDocStatus] = useState('')
  const [operatorDocStatus, setOperatorDocStatus] = useState('')
  const [orderNo, setOrderNo] = useState(savedDraft?.orderNo || '')
  const [deviceId, setDeviceId] = useState(savedDraft?.deviceId || '')
  const [consumerOrderNo, setConsumerOrderNo] = useState(savedDraft?.consumerOrderNo || '')
  const [consumerPhone, setConsumerPhone] = useState('')
  const [consumerCode, setConsumerCode] = useState('8888')
  const [ephemeralOpToken, setEphemeralOpToken] = useState('')
  const [selfValidation, setSelfValidation] = useState<SelfValidationResult | null>(null)

  // Step 5: 正式发布与公网 HTTPS 终验（可选填入临时凭据跑满 4 项）
  const [publishResult, setPublishResult] = useState<PublishResult | null>(null)
  const [smokeResult, setSmokeResult] = useState<SmokeResult | null>(null)
  const [smokeConsumerPhone, setSmokeConsumerPhone] = useState('')
  const [smokeConsumerCode, setSmokeConsumerCode] = useState('8888')
  const [smokeOperatorToken, setSmokeOperatorToken] = useState('')
  const [copiedKey, setCopiedKey] = useState('')

  function copySnippet(key: string, text: string) {
    if (!text) return
    const done = () => {
      setCopiedKey(key)
      window.setTimeout(() => setCopiedKey((cur) => (cur === key ? '' : cur)), 1500)
    }
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(text).then(done).catch(done)
      return
    }
    done()
  }

  const cleanEnv = envId.trim().toLowerCase()
  const gatewayHttpsUrl = apiDomain.trim()
    ? apiDomain.trim().startsWith('https://')
      ? apiDomain.trim()
      : `https://${apiDomain.trim()}`
    : ''

  // 非敏感进度草稿同步到 sessionStorage（严禁写入任何口令、手机号、验证码或令牌）
  useEffect(() => {
    if (typeof window === 'undefined') return
    try {
      const hasAnyInput = Boolean(
        cleanEnv ||
        customerName.trim() ||
        serverIp.trim() ||
        apiDomain.trim() ||
        selectedTenantId.trim(),
      )
      if (!hasAnyInput) {
        window.sessionStorage.removeItem(WIZARD_DRAFT_STORAGE_KEY)
        return
      }
      const draftDoc: WizardNonSecretDraft = {
        step,
        customerName,
        envId,
        serverIp,
        apiDomain,
        adminDomain,
        internalGateway,
        sshPort,
        sshUser,
        enrollMode,
        selectedTenantId,
        selectedTenantName,
        consumerKbId,
        operatorKbId,
        starterInstalled,
        orderNo,
        deviceId,
        consumerOrderNo,
      }
      window.sessionStorage.setItem(WIZARD_DRAFT_STORAGE_KEY, JSON.stringify(draftDoc))
    } catch {
      // sessionStorage 不可用时静默降级
    }
  }, [
    step,
    customerName,
    envId,
    serverIp,
    apiDomain,
    adminDomain,
    internalGateway,
    sshPort,
    sshUser,
    enrollMode,
    selectedTenantId,
    selectedTenantName,
    consumerKbId,
    operatorKbId,
    starterInstalled,
    orderNo,
    deviceId,
    consumerOrderNo,
    cleanEnv,
  ])

  async function hydrateFromServer(targetEnvRaw: string, options?: { silent?: boolean }) {
    const target = targetEnvRaw.trim().toLowerCase()
    if (!target) {
      if (!options?.silent) setError('请先填写环境代号后再恢复服务端进度')
      return
    }
    if (!options?.silent) {
      setError('')
      setNotice('')
      setBusy(true)
    }
    try {
      const hostRes = (await manage('environment-wizard', {
        wizard_action: 'host_status',
        wizard_payload: { environment: target },
      })) as { enrolled?: boolean; host?: HostView | null }

      let targetStep: WizardStep = 1
      if (hostRes?.host) {
        setEnrolledHost(hostRes.host)
        if (hostRes.host.address) setServerIp(hostRes.host.address)
        if (hostRes.host.ssh_port) setSshPort(String(hostRes.host.ssh_port))
        if (hostRes.host.ssh_user) setSshUser(hostRes.host.ssh_user)
        if (hostRes.enrolled) targetStep = 2
      } else {
        setEnrolledHost(null)
      }

      const infraRes = (await manage('environment-wizard', {
        wizard_action: 'infra_status',
        wizard_payload: { environment: target, action: 'infra_provision' },
      })) as InfraResult
      if (
        infraRes &&
        (infraRes.job_id || infraRes.provisioned || (infraRes.steps?.length ?? 0) > 0)
      ) {
        setInfraResult(infraRes)
        if (infraRes.consumer_kb_id) setConsumerKbId(infraRes.consumer_kb_id)
        if (infraRes.operator_kb_id) setOperatorKbId(infraRes.operator_kb_id)
        const req = infraRes.request || {}
        const tid = infraRes.tenant_id || req.tenant_id || ''
        if (tid) setSelectedTenantId(tid)
        if (req.tenant_name) setSelectedTenantName(req.tenant_name)
        if (req.customer_name) setCustomerName(req.customer_name)
        if (req.api_domain) setApiDomain(req.api_domain)
        if (req.gateway_internal_addr) setInternalGateway(req.gateway_internal_addr)
        if (infraRes.passed) targetStep = Math.max(targetStep, 4) as WizardStep
        else targetStep = Math.max(targetStep, 3) as WizardStep
      } else {
        setInfraResult(null)
      }

      const pubRes = (await manage('environment-wizard', {
        wizard_action: 'infra_status',
        wizard_payload: { environment: target, action: 'publish_environment' },
      })) as PublishResult
      if (pubRes && (pubRes.job_id || pubRes.published || pubRes.result?.published)) {
        setPublishResult(pubRes)
        const pubReq = pubRes.request || {}
        const pubTid = pubRes.tenant_id || pubRes.result?.tenant_id || pubReq.tenant_id || ''
        if (pubTid) setSelectedTenantId(pubTid)
        if (pubReq.tenant_name) setSelectedTenantName(pubReq.tenant_name)
        if (pubReq.customer_name) setCustomerName(pubReq.customer_name)
        const gwUrl = pubRes.gateway_url || pubRes.result?.gateway_url || pubReq.gateway_url || ''
        if (gwUrl) setApiDomain(gwUrl.replace(/^https?:\/\//, ''))
        if (pubRes.passed || pubRes.published || pubRes.result?.published) {
          targetStep = 5
        }
      } else {
        setPublishResult(null)
      }

      setStep(targetStep)
      if (!options?.silent || targetStep > 1) {
        setNotice(`已从服务端恢复环境 ${target} 的当前开通进度（第 ${targetStep} 步）`)
      }
    } catch (e) {
      if (!options?.silent) {
        setError(e instanceof Error ? e.message : '恢复服务端进度失败，请稍后重试')
      }
    } finally {
      if (!options?.silent) setBusy(false)
    }
  }

  // 若页面刷新前正在填写某一环境（sessionStorage 含 envId），挂载时自动向服务端回读真实进度
  useEffect(() => {
    const draftEnv = (savedDraft?.envId || '').trim()
    if (!draftEnv) return
    void hydrateFromServer(draftEnv, { silent: true })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function handleResetWizard() {
    if (typeof window !== 'undefined') {
      try {
        window.sessionStorage.removeItem(WIZARD_DRAFT_STORAGE_KEY)
      } catch {
        // 忽略存储异常
      }
    }
    setStep(1)
    setError('')
    setNotice('已清空向导本地草稿，可录入新客户环境')
    setCustomerName('')
    setEnvId('')
    setServerIp('')
    setApiDomain('')
    setAdminDomain('')
    setInternalGateway('')
    setSshPort('22')
    setSshUser('root')
    setRootPassword('')
    setBootstrapOneLiner('')
    setEnrolledHost(null)
    setDiscovery(null)
    setSelectedTenantId('')
    setSelectedTenantName('')
    setConsumerKbId('')
    setOperatorKbId('')
    setStarterInstalled(false)
    setConsumerDocStatus('')
    setOperatorDocStatus('')
    setOrderNo('')
    setDeviceId('')
    setConsumerOrderNo('')
    setConsumerPhone('')
    setEphemeralOpToken('')
    setSelfValidation(null)
    setInfraResult(null)
    setPublishResult(null)
    setSmokeResult(null)
    setSmokeConsumerPhone('')
    setSmokeOperatorToken('')
  }

  async function callWizard(wizard_action: string, wizard_payload: Record<string, unknown>) {
    setError('')
    setNotice('')
    setBusy(true)
    try {
      return await manage('environment-wizard', { wizard_action, wizard_payload })
    } catch (e) {
      setError(e instanceof Error ? e.message : '操作未完成，请稍后重试')
      return null
    } finally {
      setBusy(false)
    }
  }

  async function handleEnrollPassword() {
    if (!cleanEnv || !serverIp.trim() || !rootPassword) {
      setError('请先填写环境代号、服务器 IP 与一次性 SSH 口令')
      return
    }
    const pwd = rootPassword
    setRootPassword('') // 立刻从前端状态清空，不留在浏览器内存
    const res = await callWizard('host_enroll_password', {
      environment: cleanEnv,
      address: serverIp.trim(),
      password: pwd,
      ssh_port: Number(sshPort) || 22,
      ssh_user: sshUser.trim() || 'root',
    })
    if (res?.host) {
      setEnrolledHost(res.host)
      setNotice('主机已完成指纹钉扎与免密引导（一次性口令已销毁，绝未落盘）')
      setStep(2)
    }
  }

  async function handleGenerateScript() {
    if (!cleanEnv || !serverIp.trim()) {
      setError('请先填写环境代号与服务器 IP')
      return
    }
    const res = await callWizard('host_create_bootstrap_script', {
      environment: cleanEnv,
      address: serverIp.trim(),
      callback_base_url: callbackBaseUrl.trim(),
      ssh_port: Number(sshPort) || 22,
      ssh_user: sshUser.trim() || 'root',
    })
    if (res?.one_liner) {
      setBootstrapOneLiner(res.one_liner)
      if (res.host) setEnrolledHost(res.host)
      setNotice('已生成 15 分钟单次有效引导命令，请发给客户运维在本机执行后点击「刷新接入状态」')
    }
  }

  async function handleRefreshHostStatus() {
    if (!cleanEnv) return
    const res = await callWizard('host_status', { environment: cleanEnv })
    if (res?.host) {
      setEnrolledHost(res.host)
      if (res.enrolled) {
        setNotice('已确认客户主机完成引导并钉扎指纹，可进入下一步体检')
        setStep(2)
      } else {
        setNotice('尚未收到客户侧引导回调，请确认命令已执行完成')
      }
    }
  }

  async function handleRunDiscovery() {
    if (!cleanEnv || !apiDomain.trim()) {
      setError('请先确认环境代号与客户 API 域名')
      return
    }
    const mPwd = mysqlPassword
    const rPwd = redisPassword
    setMysqlPassword('')
    setRedisPassword('')
    const discoverPayload: Record<string, unknown> = {
      environment: cleanEnv,
      api_domain: apiDomain.trim().replace(/^https?:\/\//, ''),
      internal_gateway_addr: internalGateway.trim(),
    }
    if (mysqlHost.trim()) discoverPayload.mysql_host = mysqlHost.trim()
    if (mysqlPort.trim()) discoverPayload.mysql_port = Number(mysqlPort) || 3306
    if (mysqlUser.trim()) discoverPayload.mysql_user = mysqlUser.trim()
    if (mPwd) discoverPayload.mysql_password = mPwd
    if (redisHost.trim()) discoverPayload.redis_host = redisHost.trim()
    if (redisPort.trim()) discoverPayload.redis_port = Number(redisPort) || 6379
    if (rPwd) discoverPayload.redis_password = rPwd

    const res = (await callWizard('discover', discoverPayload)) as DiscoveryReport | null
    if (res) {
      setDiscovery(res)
      const firstTenant = res.discovered?.tenants?.[0]
      if (firstTenant?.id && !selectedTenantId) {
        setSelectedTenantId(firstTenant.id)
        if (!selectedTenantName && firstTenant.name) setSelectedTenantName(firstTenant.name)
      }
      const firstOrder = res.discovered?.sample_candidates?.orders?.[0]?.order_no
      if (firstOrder && !orderNo) setOrderNo(firstOrder)
      const firstDev = res.discovered?.sample_candidates?.offline_devices?.[0]?.device_id
      if (firstDev && !deviceId) setDeviceId(firstDev)
      const firstCOrder = res.discovered?.sample_candidates?.consumer_orders?.[0]?.order_no
      if (firstCOrder && !consumerOrderNo) setConsumerOrderNo(firstCOrder)
      setConfirmDbFix(false)
      setNotice(
        res.ready
          ? '环境体检全绿，可直接进入第 3 步一键配套基建'
          : '发现待修复数据面缺口，请核对下方白名单 SQL 并确认修复',
      )
    }
  }

  async function handleConfirmDbRemediation() {
    if (!discovery || !confirmDbFix) {
      setError('请先勾选二次确认框后再执行受限库修复')
      return
    }
    const plan = discovery.db_remediation_plan
    const aPwd = adminMysqlPassword
    setAdminMysqlPassword('')
    const remPayload: Record<string, unknown> = {
      environment: cleanEnv,
      remediation_ids: plan.remediation_ids,
      sql_statements: plan.sql_statements,
      confirmed_by_operator: true,
      confirmation_digest: plan.confirmation_digest,
    }
    if (adminMysqlHost.trim()) remPayload.admin_mysql_host = adminMysqlHost.trim()
    if (adminMysqlPort.trim()) remPayload.admin_mysql_port = Number(adminMysqlPort) || 3306
    if (adminMysqlUser.trim()) remPayload.admin_mysql_user = adminMysqlUser.trim()
    if (aPwd) remPayload.admin_mysql_password = aPwd

    const res = await callWizard('db_remediate', remPayload)
    if (res?.executed) {
      setDbFixed(true)
      setNotice(
        res.verified_after_fix
          ? '受限权限/无锁索引已自动执行并通过 aiops_ro 复验！'
          : '已执行白名单 SQL，请重新运行环境体检确认状态',
      )
      if (res.verified_after_fix) {
        setDiscovery({
          ...discovery,
          ready: true,
          state: 'passed',
          db_remediation_plan: { ...plan, has_auto_fixable_db_gaps: false },
        })
      }
    }
  }

  async function pollWizardJob<
    T extends { job_id?: string; state?: string; passed?: boolean; failed_step?: string },
  >(
    action: 'infra_provision' | 'publish_environment',
    initial: T,
    onTick: (latest: T) => void,
  ): Promise<T | null> {
    let latest: T = initial
    onTick(latest)
    if (latest.state !== 'queued' && latest.state !== 'running') {
      return latest
    }
    for (let i = 0; i < 120; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 3000))
      try {
        const polled = (await manage('environment-wizard', {
          wizard_action: 'infra_status',
          wizard_payload: {
            environment: cleanEnv,
            action,
            ...(latest.job_id ? { job_id: latest.job_id } : {}),
          },
        })) as T
        latest = polled
        onTick(latest)
        if (latest.state !== 'queued' && latest.state !== 'running') {
          return latest
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : '轮询任务状态失败，请稍后重试')
        return null
      }
    }
    setError('异步任务执行超时，请点击状态刷新确认结果')
    return latest
  }

  async function handleProvisionInfra() {
    if (!cleanEnv || !serverIp.trim() || !gatewayHttpsUrl) {
      setError('请确认环境代号、服务器 IP 与 HTTPS API 域名已填写')
      return
    }
    const pool = await callWizard('artifact_pool_status', { environment: cleanEnv })
    if (typeof pool?.count === 'number') setPoolCount(pool.count)
    setError('')
    setNotice('')
    setBusy(true)
    try {
      const initial = (await manage('environment-wizard', {
        wizard_action: 'infra_provision',
        wizard_payload: {
          environment: cleanEnv,
          egress_ip: serverIp.trim(),
          api_domain: apiDomain.trim().replace(/^https?:\/\//, ''),
          gateway_url: gatewayHttpsUrl,
          tenant_id: selectedTenantId.trim(),
          tenant_name: selectedTenantName.trim(),
          customer_name: customerName.trim() || cleanEnv,
          python_minor: enrolledHost?.python_minor || discovery?.host?.python_minor || '3.12',
          gateway_internal_addr: internalGateway.trim(),
        },
      })) as InfraResult
      const finalRes = await pollWizardJob('infra_provision', initial, setInfraResult)
      if (finalRes) {
        setInfraResult(finalRes)
        if (finalRes.consumer_kb_id) setConsumerKbId(finalRes.consumer_kb_id)
        if (finalRes.operator_kb_id) setOperatorKbId(finalRes.operator_kb_id)
        if (finalRes.tenant_id && !selectedTenantId) setSelectedTenantId(finalRes.tenant_id)
        if (finalRes.passed) {
          setNotice('白名单、专属执行节点、客户机冷装、双 Nginx 扩展与即刻探活已全部通过！')
        } else {
          setError(`基建编排在步骤 ${finalRes.failed_step || '未知'} 阻断，请查看明细修复后重试`)
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '基建编排启动失败，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  async function handleInstallStarterFaq() {
    const res = await callWizard('kb_install_starter_faq', {
      environment: cleanEnv,
      tenant_id: selectedTenantId.trim(),
      kb_id: consumerKbId.trim() || `kb-consumer-${cleanEnv}`,
    })
    if (res?.document_id) {
      setStarterInstalled(true)
      if (res.kb_id) setConsumerKbId(String(res.kb_id))
      setConsumerDocStatus(`已装入默认试跑模板（${res.filename} · ${res.chunk_count} 切片）`)
      setNotice('对客试跑 FAQ 模板已就绪，正式对外服务后建议替换为客户官方 FAQ 文档')
    }
  }

  async function handleUploadDoc(role: 'consumer' | 'operator', file: File | null) {
    if (!file) return
    const buf = await file.arrayBuffer()
    const kbId =
      role === 'consumer'
        ? consumerKbId.trim() || `kb-consumer-${cleanEnv}`
        : operatorKbId.trim() || `kb-operator-${cleanEnv}`
    const res = await callWizard('kb_upload', {
      environment: cleanEnv,
      tenant_id: selectedTenantId.trim(),
      target_role: role,
      kb_id: kbId,
      filename: file.name,
      content_base64: arrayBufferToBase64(buf),
    })
    if (res?.document_id) {
      if (res.kb_id) {
        if (role === 'consumer') setConsumerKbId(String(res.kb_id))
        else setOperatorKbId(String(res.kb_id))
      }
      const summary = `已上传 ${res.filename}（${res.chunk_count} 切片）`
      if (role === 'consumer') setConsumerDocStatus(summary)
      else setOperatorDocStatus(summary)
      setNotice(`${role === 'consumer' ? '对客' : '管家'}知识库文档上传成功`)
    }
  }

  async function handleSelfValidate() {
    if (ephemeralOpToken.trim()) {
      const tokenWarning = checkOperatorTokenTenant(ephemeralOpToken, selectedTenantId)
      if (tokenWarning) {
        setError(tokenWarning)
        return
      }
    }
    if (!selectedTenantId || !orderNo || !deviceId || !consumerOrderNo) {
      setError('请先确认租户与自动推荐的验收样本（订单号/离线设备号/C端订单号）')
      return
    }
    const opTok = ephemeralOpToken
    const phone = consumerPhone
    const code = consumerCode
    setEphemeralOpToken('')
    setConsumerPhone('')
    const win = buildRecentOfflineWindow(7)
    const res = (await callWizard('self_validate', {
      environment: cleanEnv,
      tenant_id: selectedTenantId,
      company_gateway_url: gatewayHttpsUrl,
      order_no: orderNo.trim(),
      device_id: deviceId.trim(),
      offline_start: win.start,
      offline_end: win.end,
      consumer_order_no: consumerOrderNo.trim(),
      consumer_reference_id: starterInstalled ? 'starter-faq' : 'uploaded-faq',
      consumer_phone: phone.trim(),
      consumer_code: code.trim(),
      operator_token: opTok.trim(),
    })) as SelfValidationResult | null
    if (res) {
      setSelfValidation(res)
      if (res.passed) {
        setNotice('三能力自验全绿，临时管家会话已自动吊销！可进入第 5 步正式发布与公网终验')
        setStep(5)
      }
    }
  }

  async function handlePublishEnvironment() {
    if (!cleanEnv || !selectedTenantId || !gatewayHttpsUrl) {
      setError('请先确认环境代号、租户编号与 HTTPS API 域名')
      return
    }
    setError('')
    setNotice('')
    setBusy(true)
    try {
      const initial = (await manage('environment-wizard', {
        wizard_action: 'publish_environment',
        wizard_payload: {
          environment: cleanEnv,
          tenant_id: selectedTenantId.trim(),
          tenant_name: selectedTenantName.trim() || selectedTenantId.trim(),
          customer_name: customerName.trim() || cleanEnv,
          gateway_url: gatewayHttpsUrl,
        },
      })) as PublishResult
      const finalPub = await pollWizardJob('publish_environment', initial, setPublishResult)
      if (finalPub) {
        setPublishResult(finalPub)
        if (finalPub.passed || finalPub.published) {
          setNotice('客户环境与租户已正式启用并同步活跃路由，请执行公网终验！')
        } else {
          setError(`正式发布在步骤 ${finalPub.failed_step || '未知'} 阻断，请核对后重试`)
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '正式发布未完成，请稍后重试')
    } finally {
      setBusy(false)
    }
  }

  async function handlePostPublishSmoke() {
    if (!gatewayHttpsUrl || !selectedTenantId) {
      setError('请先确认 HTTPS API 域名与目标租户 ID')
      return
    }
    if (smokeOperatorToken.trim()) {
      const tokenWarning = checkOperatorTokenTenant(smokeOperatorToken, selectedTenantId)
      if (tokenWarning) {
        setError(tokenWarning)
        return
      }
    }
    const phone = smokeConsumerPhone.trim()
    const code = smokeConsumerCode.trim()
    const opTok = smokeOperatorToken.trim()
    setSmokeConsumerPhone('')
    setSmokeOperatorToken('')
    const smokePayload: Record<string, unknown> = {
      environment: cleanEnv,
      gateway_url: gatewayHttpsUrl,
      tenant_id: selectedTenantId,
    }
    if (phone && code) {
      smokePayload.consumer_phone = phone
      smokePayload.consumer_code = code
    }
    if (opTok) {
      smokePayload.operator_token = opTok
    }
    const res = (await callWizard('post_publish_smoke', smokePayload)) as SmokeResult | null
    if (res) {
      setSmokeResult(res)
      setNotice(
        res.passed
          ? '公网正式 HTTPS 终验冒烟全部通过！客户环境已正式可投入使用'
          : '部分公网冒烟检查未通过，请排查明细',
      )
    }
  }

  return (
    <LiquidGlassSurface
      as="section"
      variant="elevated"
      aria-label="新客户 5 步自助开通向导"
      className="space-y-5 overflow-hidden rounded-2xl p-6"
    >
      {/* 顶栏 */}
      <div className="flex flex-wrap items-start justify-between gap-4 border-b border-divider-subtle pb-4">
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <span
            aria-hidden="true"
            className="aiops-glass-pill flex size-10 shrink-0 items-center justify-center rounded-xl text-text-accent"
          >
            <span className="i-ri-magic-line size-5" />
          </span>
          <div className="min-w-0">
            <h3 className="system-lg-semibold text-text-primary">
              新客户自助开通向导（5 步零命令行）
            </h3>
            <p className="mt-0.5 system-xs-regular text-text-tertiary">
              只需录入 5
              项客户基础信息即可完成安全引导、数据面体检、基建配套、三能力自验与公网终验；底层参数折叠于高级工程视图。
            </p>
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <Button
            type="button"
            variant="ghost"
            size="small"
            disabled={busy || !cleanEnv}
            onClick={() => void hydrateFromServer(cleanEnv)}
          >
            <span aria-hidden="true" className="mr-1 i-ri-restart-line size-3.5" />
            恢复服务端进度
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="small"
            disabled={busy}
            onClick={handleResetWizard}
          >
            重置表单
          </Button>
          {onOpenAdvancedConfig && (
            <Button type="button" variant="ghost" size="small" onClick={onOpenAdvancedConfig}>
              高级工程视图
            </Button>
          )}
          {onClose && (
            <Button type="button" variant="secondary" size="small" onClick={onClose}>
              返回总览
            </Button>
          )}
        </div>
      </div>

      {/* 5 步进度条 */}
      <nav aria-label="开通步骤" className="grid gap-2 sm:grid-cols-2 lg:grid-cols-5">
        {STEP_LABELS.map((item) => {
          const active = step === item.id
          const done = step > item.id
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setStep(item.id)}
              className={`aiops-glass-interactive group relative flex cursor-pointer items-start gap-2 rounded-xl border px-2.5 py-2.5 text-left transition-all ${
                active
                  ? 'aiops-glass-pill border-state-accent-solid/70 shadow-sm'
                  : done
                    ? 'border-state-success-active/45 bg-components-badge-bg-green-soft/65 hover:bg-components-badge-bg-green-soft'
                    : 'aiops-glass-card hover:border-divider-regular'
              }`}
            >
              <span
                aria-hidden="true"
                className={`mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
                  active
                    ? 'bg-state-accent-solid text-text-primary-on-surface ring-2 ring-state-accent-hover'
                    : done
                      ? 'bg-state-success-solid text-text-primary-on-surface'
                      : 'bg-components-badge-bg-gray-soft text-text-tertiary'
                }`}
              >
                {done ? <span className="i-ri-check-line size-3" /> : item.id}
              </span>
              <div className="min-w-0">
                <div
                  className={`system-xs-semibold ${
                    active ? 'text-text-accent' : 'text-text-primary'
                  }`}
                >
                  {item.title}
                </div>
                <div className="mt-0.5 text-[10.5px] leading-snug text-text-tertiary">
                  {item.subtitle}
                </div>
              </div>
            </button>
          )
        })}
      </nav>

      {error && (
        <div
          role="alert"
          className="flex items-center gap-2.5 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-4 py-3 system-xs-medium text-text-destructive"
        >
          <span aria-hidden="true" className="i-ri-error-warning-fill size-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="flex items-center gap-2.5 rounded-xl border border-state-success-active/60 bg-state-success-hover px-4 py-3 system-xs-medium text-text-success">
          <span aria-hidden="true" className="i-ri-checkbox-circle-fill size-4 shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      {/* STEP 1 */}
      {step === 1 && (
        <div className="space-y-4">
          <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4">
            <div className="flex items-center gap-2 system-xs-semibold text-text-primary">
              <span aria-hidden="true" className="i-ri-information-line size-4 text-text-accent" />
              <span>客户环境基础信息</span>
            </div>
            <div className="grid gap-3.5 sm:grid-cols-2">
              <label className="space-y-1.5">
                <span className="system-xs-medium text-text-secondary">客户名称</span>
                <Input
                  aria-label="客户名称"
                  placeholder="如：示例充电运营客户"
                  value={customerName}
                  onChange={(e) => setCustomerName(e.target.value)}
                />
              </label>
              <label className="space-y-1.5">
                <span className="system-xs-medium text-text-secondary">
                  环境代号（小写字母/数字）
                </span>
                <Input
                  aria-label="环境代号"
                  placeholder="如：acme"
                  value={envId}
                  onChange={(e) => setEnvId(e.target.value)}
                />
              </label>
              <label className="space-y-1.5">
                <span className="system-xs-medium text-text-secondary">服务器公网 IP</span>
                <Input
                  aria-label="服务器公网 IP"
                  placeholder="如：203.0.113.10"
                  value={serverIp}
                  onChange={(e) => setServerIp(e.target.value)}
                />
              </label>
              <label className="space-y-1.5">
                <span className="system-xs-medium text-text-secondary">客户 API 域名</span>
                <Input
                  aria-label="客户 API 域名"
                  placeholder="如：api.customer.example.com"
                  value={apiDomain}
                  onChange={(e) => setApiDomain(e.target.value)}
                />
              </label>
              <label className="space-y-1.5">
                <span className="system-xs-medium text-text-secondary">
                  客户管理后台域名（可选）
                </span>
                <Input
                  aria-label="客户管理后台域名"
                  placeholder="如：admin.customer.example.com"
                  value={adminDomain}
                  onChange={(e) => setAdminDomain(e.target.value)}
                />
              </label>
              <label className="space-y-1.5">
                <span className="system-xs-medium text-text-secondary">网关内网地址</span>
                <Input
                  aria-label="网关内网地址"
                  placeholder="如：10.0.0.10:30899"
                  value={internalGateway}
                  onChange={(e) => setInternalGateway(e.target.value)}
                />
              </label>
            </div>
          </div>

          <div className="space-y-3.5 rounded-xl border border-divider-subtle bg-background-section-burn/50 p-4">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div className="flex items-center gap-2 system-xs-semibold text-text-primary">
                <span
                  aria-hidden="true"
                  className="i-ri-shield-keyhole-line size-4 text-text-accent"
                />
                <span>主机安全引导模式</span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  type="button"
                  size="small"
                  variant={enrollMode === 'ephemeral_password' ? 'primary' : 'secondary'}
                  onClick={() => setEnrollMode('ephemeral_password')}
                >
                  模式 A：一次性 root 密码（内存即用即毁）
                </Button>
                <Button
                  type="button"
                  size="small"
                  variant={enrollMode === 'bootstrap_script' ? 'primary' : 'secondary'}
                  onClick={() => setEnrollMode('bootstrap_script')}
                >
                  模式 B：客户侧单行引导脚本（零密码）
                </Button>
              </div>
            </div>

            {enrollMode === 'ephemeral_password' ? (
              <div className="space-y-3 rounded-xl bg-components-panel-bg/85 p-4">
                <p className="system-xs-regular text-text-tertiary">
                  口令仅在服务端内存瞬时用于钉扎主机指纹、下发专属 Ed25519 公钥及补齐
                  aiops/venv，随即清空丢弃，绝不落盘或入库。
                </p>
                <div className="grid gap-3 sm:grid-cols-3">
                  <label className="space-y-1">
                    <span className="system-xs-medium text-text-secondary">SSH 用户</span>
                    <Input value={sshUser} onChange={(e) => setSshUser(e.target.value)} />
                  </label>
                  <label className="space-y-1">
                    <span className="system-xs-medium text-text-secondary">SSH 端口</span>
                    <Input value={sshPort} onChange={(e) => setSshPort(e.target.value)} />
                  </label>
                  <label className="space-y-1">
                    <span className="system-xs-medium text-text-secondary">一次性 SSH 口令</span>
                    <Input
                      aria-label="一次性 SSH 口令"
                      type="password"
                      autoComplete="new-password"
                      placeholder="仅内存瞬时使用"
                      value={rootPassword}
                      onChange={(e) => setRootPassword(e.target.value)}
                    />
                  </label>
                </div>
                <Button
                  type="button"
                  variant="primary"
                  disabled={busy}
                  onClick={handleEnrollPassword}
                >
                  {busy ? '正在引导并钉扎指纹…' : '立即安全引导并进入下一步'}
                </Button>
              </div>
            ) : (
              <div className="space-y-3 rounded-xl bg-components-panel-bg/85 p-4">
                <p className="system-xs-regular text-text-tertiary">
                  面向高安全客户：无需提供 root 密码。生成 15
                  分钟单次消费引导命令交客户运维本机执行。
                </p>
                <label className="block space-y-1">
                  <span className="system-xs-medium text-text-secondary">
                    控制面回调 HTTPS 基地址
                  </span>
                  <Input
                    aria-label="控制面回调 HTTPS 基地址"
                    value={callbackBaseUrl}
                    onChange={(e) => setCallbackBaseUrl(e.target.value)}
                  />
                </label>
                <div className="flex flex-wrap gap-2">
                  <Button
                    type="button"
                    variant="primary"
                    disabled={busy}
                    onClick={handleGenerateScript}
                  >
                    生成单次引导命令
                  </Button>
                  <Button
                    type="button"
                    variant="secondary"
                    disabled={busy}
                    onClick={handleRefreshHostStatus}
                  >
                    刷新接入状态
                  </Button>
                </div>
                {bootstrapOneLiner && (
                  <div className="overflow-hidden rounded-xl border border-divider-subtle bg-background-section-burn">
                    <div className="flex items-center justify-between border-b border-divider-subtle px-3 py-1.5 text-[11px] text-text-tertiary">
                      <span className="flex items-center gap-1.5">
                        <span aria-hidden="true" className="i-ri-terminal-box-line size-3.5" />
                        <span>单次引导命令（15 分钟有效）</span>
                      </span>
                      <button
                        type="button"
                        onClick={() => copySnippet('bootstrap', bootstrapOneLiner)}
                        className="inline-flex cursor-pointer items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium text-text-accent hover:bg-components-panel-bg"
                      >
                        <span
                          aria-hidden="true"
                          className={`size-3 ${copiedKey === 'bootstrap' ? 'i-ri-check-line' : 'i-ri-file-copy-line'}`}
                        />
                        <span>{copiedKey === 'bootstrap' ? '已复制' : '复制命令'}</span>
                      </button>
                    </div>
                    <pre className="overflow-x-auto p-3 font-mono text-xs text-text-primary">
                      {bootstrapOneLiner}
                    </pre>
                  </div>
                )}
              </div>
            )}
          </div>

          {enrolledHost && (
            <div className="flex flex-wrap items-center gap-2.5 rounded-xl border border-state-success-active/40 bg-components-badge-bg-green-soft/45 px-4 py-3 system-xs-regular text-text-secondary">
              <span
                aria-hidden="true"
                className="i-ri-shield-check-line size-4 shrink-0 text-text-success"
              />
              <span>
                已登记主机：<strong className="text-text-primary">{enrolledHost.host_alias}</strong>
                （状态：{enrolledHost.status}，指纹：
                <code className="font-mono">
                  {enrolledHost.host_fingerprint_sha256 || '待回调'}
                </code>
                ，Python {enrolledHost.python_minor || '待探测'}，Nginx：
                {enrolledHost.nginx_flavor || '待探测'}）
              </span>
            </div>
          )}
        </div>
      )}

      {/* STEP 2 */}
      {step === 2 && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-divider-subtle bg-background-section-burn/40 p-4">
            <p className="max-w-3xl system-xs-regular text-text-secondary">
              自动探测客户租户、MySQL 最小只读权限（含{' '}
              <code className="font-mono">qumall_upms</code>{' '}
              三张平台表）、必选无锁索引与本机直连时序服务端口（可选传入瞬时只读与管理员 DB
              凭据，仅用于当前隧道体检与二次确认修复，用完即焚）。
            </p>
            <Button type="button" variant="primary" disabled={busy} onClick={handleRunDiscovery}>
              <span aria-hidden="true" className="mr-1.5 i-ri-pulse-line size-4" />
              {busy ? '正在探测体检…' : '运行自动环境探测与体检'}
            </Button>
          </div>

          <div className="grid gap-3.5 rounded-xl border border-divider-subtle bg-background-section-burn/30 p-4 sm:grid-cols-3">
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">
                目标租户 ID（可由体检自动识别）
              </span>
              <Input
                aria-label="目标租户 ID"
                placeholder="如 1000000000000000001"
                value={selectedTenantId}
                onChange={(e) => setSelectedTenantId(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">目标租户名称</span>
              <Input
                aria-label="目标租户名称"
                placeholder="如 示例租户"
                value={selectedTenantName}
                onChange={(e) => setSelectedTenantName(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">MySQL 主机（可选）</span>
              <Input
                aria-label="MySQL 主机"
                placeholder="如 127.0.0.1 或内网 DB IP"
                value={mysqlHost}
                onChange={(e) => setMysqlHost(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">MySQL 端口 / 只读账号</span>
              <div className="flex gap-2">
                <Input
                  aria-label="MySQL 端口"
                  value={mysqlPort}
                  onChange={(e) => setMysqlPort(e.target.value)}
                />
                <Input
                  aria-label="MySQL 只读账号"
                  value={mysqlUser}
                  onChange={(e) => setMysqlUser(e.target.value)}
                />
              </div>
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">
                MySQL 只读口令（仅服务端 0600 模板）
              </span>
              <Input
                aria-label="MySQL 只读口令"
                type="password"
                value={mysqlPassword}
                onChange={(e) => setMysqlPassword(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">
                Redis 主机 / 端口 / 口令（可选）
              </span>
              <div className="flex gap-1.5">
                <Input
                  aria-label="Redis 主机"
                  placeholder="127.0.0.1"
                  value={redisHost}
                  onChange={(e) => setRedisHost(e.target.value)}
                />
                <Input
                  aria-label="Redis 端口"
                  value={redisPort}
                  onChange={(e) => setRedisPort(e.target.value)}
                />
                <Input
                  aria-label="Redis 口令"
                  type="password"
                  placeholder="口令"
                  value={redisPassword}
                  onChange={(e) => setRedisPassword(e.target.value)}
                />
              </div>
            </label>
          </div>

          {discovery && (
            <div className="space-y-4">
              {discovery.discovered.tenants.length > 0 && (
                <div className="rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4">
                  <div className="mb-2.5 system-xs-semibold text-text-primary">
                    自动发现的客户租户
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {discovery.discovered.tenants.map((t) => {
                      const picked = selectedTenantId === t.id
                      return (
                        <button
                          key={t.id}
                          type="button"
                          onClick={() => setSelectedTenantId(t.id)}
                          className={`inline-flex cursor-pointer items-center gap-1.5 rounded-lg border px-3 py-1.5 system-xs-medium transition-colors ${
                            picked
                              ? 'border-state-accent-solid bg-components-badge-bg-blue-light-soft text-text-accent'
                              : 'border-divider-subtle bg-components-panel-bg text-text-secondary hover:border-divider-regular'
                          }`}
                        >
                          {picked && (
                            <span aria-hidden="true" className="i-ri-check-line size-3.5" />
                          )}
                          <span>
                            {t.name}（{t.id}）
                          </span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              )}

              <div className="space-y-2 rounded-xl border border-divider-subtle p-4">
                <div className="mb-2 system-xs-semibold text-text-primary">
                  数据面与基础设施体检单
                </div>
                {discovery.checks.map((c) => (
                  <div
                    key={c.id}
                    className={`flex items-center justify-between gap-2 rounded-lg border px-3 py-2 text-xs ${
                      c.state === 'passed'
                        ? 'border-state-success-active/35 bg-components-badge-bg-green-soft/45'
                        : c.state === 'blocked'
                          ? 'border-state-destructive-border bg-components-badge-bg-red-soft/50'
                          : 'border-state-warning-active/35 bg-components-badge-bg-orange-soft/50'
                    }`}
                  >
                    <span className="flex items-center gap-2 text-text-primary">
                      <StatusDot
                        status={
                          c.state === 'passed'
                            ? 'success'
                            : c.state === 'blocked'
                              ? 'error'
                              : 'warning'
                        }
                        size="small"
                      />
                      <span>{c.summary}</span>
                    </span>
                    <span className="font-mono text-[11px] text-text-tertiary">{c.state}</span>
                  </div>
                ))}
              </div>

              {discovery.db_remediation_plan.has_auto_fixable_db_gaps && (
                <div className="space-y-3.5 rounded-xl border border-state-warning-active/50 bg-components-badge-bg-orange-soft p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2 system-xs-semibold text-text-warning">
                      <span aria-hidden="true" className="i-ri-error-warning-line size-4" />
                      <span>
                        引导期受限白名单修复预览（仅最小 SELECT 授权与 ALGORITHM=INPLACE, LOCK=NONE
                        无锁索引）
                      </span>
                    </div>
                    <button
                      type="button"
                      onClick={() =>
                        copySnippet('sql', discovery.db_remediation_plan.sql_statements.join('\n'))
                      }
                      className="inline-flex cursor-pointer items-center gap-1 rounded-md bg-components-panel-bg/80 px-2 py-1 text-[11px] font-medium text-text-primary hover:bg-components-panel-bg"
                    >
                      <span
                        aria-hidden="true"
                        className={`size-3 ${copiedKey === 'sql' ? 'i-ri-check-line text-text-success' : 'i-ri-file-copy-line'}`}
                      />
                      <span>{copiedKey === 'sql' ? '已复制' : '复制 SQL'}</span>
                    </button>
                  </div>
                  <pre className="overflow-x-auto rounded-lg border border-divider-subtle bg-components-panel-bg p-3 font-mono text-xs text-text-primary">
                    {discovery.db_remediation_plan.sql_statements.join('\n')}
                  </pre>
                  <div className="grid gap-3 sm:grid-cols-4">
                    <Input
                      aria-label="管理员 DB 主机"
                      placeholder="管理员 DB 主机（同上可留空）"
                      value={adminMysqlHost}
                      onChange={(e) => setAdminMysqlHost(e.target.value)}
                    />
                    <Input
                      aria-label="管理员 DB 端口"
                      placeholder="3306"
                      value={adminMysqlPort}
                      onChange={(e) => setAdminMysqlPort(e.target.value)}
                    />
                    <Input
                      aria-label="管理员 DB 用户"
                      placeholder="如 root"
                      value={adminMysqlUser}
                      onChange={(e) => setAdminMysqlUser(e.target.value)}
                    />
                    <Input
                      aria-label="管理员 DB 密码（用后即焚）"
                      type="password"
                      placeholder="管理员 DB 密码"
                      value={adminMysqlPassword}
                      onChange={(e) => setAdminMysqlPassword(e.target.value)}
                    />
                  </div>
                  <label className="flex cursor-pointer items-center gap-2.5 rounded-lg bg-components-panel-bg/85 px-3 py-2.5 system-xs-medium text-text-primary">
                    <Checkbox
                      aria-label="二次确认受限白名单 SQL"
                      checked={confirmDbFix}
                      onCheckedChange={(value) => setConfirmDbFix(value === true)}
                    />
                    <span>我已核对以上受限白名单 SQL，二次确认授权在引导期自动代执行并复验</span>
                  </label>
                  <Button
                    type="button"
                    variant="primary"
                    disabled={busy || !confirmDbFix}
                    onClick={handleConfirmDbRemediation}
                  >
                    {busy ? '正在执行并复验…' : '二次确认：自动代执行并复验'}
                  </Button>
                </div>
              )}

              {(discovery.ready || dbFixed) && (
                <Button type="button" variant="primary" onClick={() => setStep(3)}>
                  体检全绿 · 进入第 3 步基建与一键部署
                </Button>
              )}
            </div>
          )}
        </div>
      )}

      {/* STEP 3 */}
      {step === 3 && (
        <div className="space-y-4">
          <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/40 p-4">
            <p className="system-xs-regular text-text-secondary">
              自动从控制面离线制品池匹配对应 Python 小版本制品，并顺序完成：36/41 白名单放行 → 41
              专属执行节点 → 客户机双 Nginx 扩展（含 nginx -t 回滚门）→ 公网即刻探活。
            </p>
            <div className="flex flex-wrap items-center gap-2.5">
              <Button
                type="button"
                variant="primary"
                disabled={busy}
                onClick={handleProvisionInfra}
              >
                <span aria-hidden="true" className="mr-1.5 i-ri-rocket-2-line size-4" />
                {busy ? '正在编排周边基建…' : '一键配套周边基建并探活'}
              </Button>
              {onOpenLegacyOnboard && (
                <Button type="button" variant="secondary" onClick={onOpenLegacyOnboard}>
                  查看 11 阶段冷装/一键开通任务明细
                </Button>
              )}
              {poolCount !== null && (
                <span className="rounded-full bg-components-panel-bg px-3 py-1 system-xs-regular text-text-tertiary">
                  控制面离线制品池已就绪条目数：{poolCount}
                </span>
              )}
            </div>
          </div>
          {infraResult && (
            <div className="space-y-2.5 rounded-xl border border-divider-subtle p-4">
              {infraResult.steps.map((s) => (
                <div
                  key={s.step}
                  className={`flex items-center justify-between rounded-lg border px-3 py-2 text-xs ${
                    s.state === 'passed'
                      ? 'border-state-success-active/35 bg-components-badge-bg-green-soft/45'
                      : s.state === 'failed'
                        ? 'border-state-destructive-border bg-components-badge-bg-red-soft/50'
                        : 'border-divider-subtle bg-background-section-burn/50'
                  }`}
                >
                  <span className="flex items-center gap-2 font-medium text-text-primary">
                    <span
                      aria-hidden="true"
                      className={`size-3.5 ${
                        s.state === 'passed'
                          ? 'i-ri-checkbox-circle-fill text-text-success'
                          : s.state === 'failed'
                            ? 'i-ri-close-circle-fill text-text-destructive'
                            : 'i-ri-time-line text-text-tertiary'
                      }`}
                    />
                    <span>{s.step}</span>
                  </span>
                  <span className="text-text-secondary">
                    {s.state}
                    {s.anon_assistant_status
                      ? ` (匿名=${s.anon_assistant_status}, 原路由=${s.upms_tenant_list_status})`
                      : ''}
                  </span>
                </div>
              ))}
              {infraResult.passed && (
                <div className="pt-2">
                  <Button type="button" variant="primary" onClick={() => setStep(4)}>
                    基建全绿 · 进入第 4 步知识库与三能力自验
                  </Button>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* STEP 4 */}
      {step === 4 && (
        <div className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4">
              <div className="flex items-center gap-2 system-xs-semibold text-text-primary">
                <span aria-hidden="true" className="i-ri-book-open-line size-4 text-text-accent" />
                <span>对客知识库（隔离）</span>
              </div>
              <Input
                placeholder="对客知识库 ID（默认自动匹配）"
                value={consumerKbId}
                onChange={(e) => setConsumerKbId(e.target.value)}
              />
              <div className="space-y-2.5 rounded-lg border border-dashed border-divider-regular bg-components-panel-bg/80 p-3">
                <Button
                  type="button"
                  size="small"
                  variant="secondary"
                  disabled={busy}
                  onClick={handleInstallStarterFaq}
                >
                  一键装入默认充电试跑 FAQ 模板
                </Button>
                <div className="flex items-center gap-2.5 text-xs text-text-tertiary">
                  <span aria-hidden="true" className="i-ri-upload-cloud-2-line size-4 shrink-0" />
                  <input
                    aria-label="上传对客知识库文档"
                    type="file"
                    accept=".md,.txt,.docx,.pdf"
                    className="block w-full cursor-pointer text-xs text-text-secondary file:mr-3 file:cursor-pointer file:rounded-lg file:border file:border-divider-regular file:bg-components-button-secondary-bg file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-text-secondary file:transition-colors hover:file:bg-components-button-secondary-bg-hover"
                    onChange={(e) => handleUploadDoc('consumer', e.target.files?.[0] || null)}
                  />
                </div>
              </div>
              {consumerDocStatus && (
                <div className="text-xs font-medium text-text-success">{consumerDocStatus}</div>
              )}
            </div>

            <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4">
              <div className="flex items-center gap-2 system-xs-semibold text-text-primary">
                <span aria-hidden="true" className="i-ri-settings-4-line size-4 text-text-accent" />
                <span>管家运维知识库（隔离）</span>
              </div>
              <Input
                placeholder="管家知识库 ID（默认自动匹配）"
                value={operatorKbId}
                onChange={(e) => setOperatorKbId(e.target.value)}
              />
              <div className="flex items-center gap-2.5 rounded-lg border border-dashed border-divider-regular bg-components-panel-bg/80 p-3 text-xs text-text-tertiary">
                <span aria-hidden="true" className="i-ri-upload-cloud-2-line size-4 shrink-0" />
                <input
                  aria-label="上传管家运维知识库文档"
                  type="file"
                  accept=".md,.txt,.docx,.pdf"
                  className="block w-full cursor-pointer text-xs text-text-secondary file:mr-3 file:cursor-pointer file:rounded-lg file:border file:border-divider-regular file:bg-components-button-secondary-bg file:px-3 file:py-1.5 file:text-xs file:font-medium file:text-text-secondary file:transition-colors hover:file:bg-components-button-secondary-bg-hover"
                  onChange={(e) => handleUploadDoc('operator', e.target.files?.[0] || null)}
                />
              </div>
              {operatorDocStatus && (
                <div className="text-xs font-medium text-text-success">{operatorDocStatus}</div>
              )}
            </div>
          </div>

          <div className="space-y-3.5 rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4">
            <div className="flex items-center gap-2 system-xs-semibold text-text-primary">
              <span aria-hidden="true" className="i-ri-shield-check-line size-4 text-text-accent" />
              <span>免 JSON 瞬时三能力自验（凭证用完即自动吊销销毁）</span>
            </div>
            <p className="rounded-lg bg-components-panel-bg/80 px-3 py-2 text-[11px] text-text-tertiary">
              临时管家令牌须在客户后台用<strong>目标业务租户账号</strong>（勿用超管{' '}
              <code className="font-mono">system</code>，其 JWT{' '}
              <code className="font-mono">tenant_id=-1</code>）完成验证码登录后提取。
            </p>
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">
                  验收订单号（含过程统计）
                </span>
                <Input
                  aria-label="验收订单号"
                  value={orderNo}
                  onChange={(e) => setOrderNo(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">离线设备号</span>
                <Input
                  aria-label="离线设备号"
                  value={deviceId}
                  onChange={(e) => setDeviceId(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">C 端验收订单号</span>
                <Input
                  aria-label="C 端验收订单号"
                  value={consumerOrderNo}
                  onChange={(e) => setConsumerOrderNo(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">
                  C 端测试手机号（仅内存换会话）
                </span>
                <Input
                  aria-label="C 端测试手机号"
                  value={consumerPhone}
                  onChange={(e) => setConsumerPhone(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">验证码</span>
                <Input
                  aria-label="验证码"
                  value={consumerCode}
                  onChange={(e) => setConsumerCode(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">
                  临时管家令牌（验完自动吊销）
                </span>
                <Input
                  aria-label="临时管家令牌"
                  type="password"
                  value={ephemeralOpToken}
                  onChange={(e) => setEphemeralOpToken(e.target.value)}
                />
              </label>
            </div>
            <Button type="button" variant="primary" disabled={busy} onClick={handleSelfValidate}>
              {busy ? '正在执行三能力自验…' : '一键执行三能力自验（免写 JSON）'}
            </Button>
            {selfValidation && (
              <div className="rounded-lg border border-divider-subtle bg-components-panel-bg px-3.5 py-2.5 text-xs text-text-secondary">
                自验结论：{selfValidation.passed ? '三能力全绿通过' : '存在未通过项'} ·
                临时管家会话已吊销：
                {selfValidation.operator_session_revoked ? '是' : '否'} · 凭据落盘：
                {selfValidation.credentials_persisted ? '是' : '零落盘'}
              </div>
            )}
          </div>
        </div>
      )}

      {/* STEP 5 */}
      {step === 5 && (
        <div className="space-y-4">
          <div className="space-y-3.5 rounded-xl border border-divider-subtle bg-background-section-burn/35 p-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="max-w-3xl system-xs-regular text-text-secondary">
                先点击正式发布启用 41 主目录及客户机活跃路由，再向公网域名{' '}
                <code className="font-mono">{gatewayHttpsUrl || 'https://<api_domain>'}</code>{' '}
                发起公网终验冒烟（默认执行匿名 401 + 原路由 200 基础 2
                项；可选填入下方瞬时测试凭证跑满发布后 4 项真问答，用完立即清零吊销）。
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  type="button"
                  variant="primary"
                  disabled={busy}
                  onClick={handlePublishEnvironment}
                >
                  {busy ? '正在正式发布…' : '一键正式发布并启用租户'}
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  disabled={busy}
                  onClick={handlePostPublishSmoke}
                >
                  {busy ? '正在执行公网冒烟…' : '执行公网正式 HTTPS 4 项终验'}
                </Button>
              </div>
            </div>

            <div className="grid gap-3 border-t border-divider-subtle pt-3 sm:grid-cols-3">
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">
                  发布后 C 端测试手机号（可选）
                </span>
                <Input
                  aria-label="发布后 C 端测试手机号"
                  placeholder="留空仅跑基础 2 项"
                  value={smokeConsumerPhone}
                  onChange={(e) => setSmokeConsumerPhone(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">发布后验证码（可选）</span>
                <Input
                  aria-label="发布后验证码"
                  value={smokeConsumerCode}
                  onChange={(e) => setSmokeConsumerCode(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">
                  发布后临时管家令牌（可选，自动吊销）
                </span>
                <Input
                  aria-label="发布后临时管家令牌"
                  type="password"
                  placeholder="留空跳过管家真问答"
                  value={smokeOperatorToken}
                  onChange={(e) => setSmokeOperatorToken(e.target.value)}
                />
              </label>
            </div>
          </div>

          {publishResult && (
            <div className="space-y-2 rounded-xl border border-divider-subtle bg-components-panel-bg p-4 text-xs text-text-secondary">
              <div className="font-medium text-text-primary">
                发布状态：
                {publishResult.passed || publishResult.published
                  ? '已正式启用'
                  : publishResult.state || '处理中'}
              </div>
              {publishResult.steps?.map((s) => (
                <div
                  key={s.step}
                  className="flex justify-between rounded bg-background-section-burn/60 px-2.5 py-1"
                >
                  <span>{s.step}</span>
                  <span>{s.state}</span>
                </div>
              ))}
            </div>
          )}

          {smokeResult && (
            <div className="space-y-3.5 overflow-hidden rounded-2xl border border-state-success-active/45 bg-background-section-burn/45 p-5">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle pb-3">
                <div className="flex items-center gap-2 system-sm-semibold text-text-primary">
                  <span
                    aria-hidden="true"
                    className={`size-5 ${smokeResult.passed ? 'i-ri-checkbox-circle-fill text-text-success' : 'i-ri-error-warning-fill text-text-warning'}`}
                  />
                  <span>
                    交付回执卡片 · {customerName || cleanEnv}（
                    {smokeResult.passed ? '公网终验通过' : '待排查'}）
                  </span>
                </div>
                <span className="rounded-full bg-components-badge-bg-green-soft px-3 py-0.5 text-[11px] font-medium text-text-success">
                  {smokeResult.checks.length >= 4
                    ? `全量 ${smokeResult.checks.length} 项通过`
                    : `基础 ${smokeResult.checks.length} 项已测（未填测试会话，真实问答见第 4 步）`}
                </span>
              </div>
              <div className="space-y-1.5">
                {smokeResult.checks.map((c) => (
                  <div
                    key={c.id}
                    className="flex items-center justify-between rounded-lg border border-divider-subtle bg-components-panel-bg px-3 py-2 text-xs"
                  >
                    <span className="font-mono text-text-primary">{c.id}</span>
                    <span
                      className={
                        c.passed
                          ? 'font-medium text-text-success'
                          : 'font-medium text-text-destructive'
                      }
                    >
                      {c.passed ? '通过' : '失败'} {c.actual_http ? `(HTTP ${c.actual_http})` : ''}
                    </span>
                  </div>
                ))}
              </div>
              <ul className="list-disc space-y-1 rounded-xl border border-divider-subtle bg-components-panel-bg/85 py-3 pr-4 pl-8 text-xs text-text-tertiary">
                <li>
                  诚实边界：业务语义准确率当前为 <strong>{smokeResult.business_accuracy}</strong>
                  （待工程师/业务方结合真实故障人工签收）。
                </li>
                {typeof smokeResult.operator_session_revoked === 'boolean' && (
                  <li>
                    临时管家令牌自动吊销：
                    {smokeResult.operator_session_revoked ? '已吊销' : '未确认'} · 零落盘
                  </li>
                )}
                {enrolledHost?.rotate_root_password_recommended && (
                  <li>
                    安全提醒：本次使用了模式 A 一次性口令引导，建议客户运维轮换服务器 root 密码。
                  </li>
                )}
                {starterInstalled && (
                  <li>
                    知识库提醒：对客库当前含临时试跑 FAQ 模板，建议上线后替换为客户官方客服文档。
                  </li>
                )}
              </ul>
            </div>
          )}
        </div>
      )}
    </LiquidGlassSurface>
  )
}
