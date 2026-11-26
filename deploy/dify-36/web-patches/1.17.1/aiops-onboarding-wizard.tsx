'use client'

import { Button } from '@langgenius/dify-ui/button'
import { Input } from '@langgenius/dify-ui/input'
import { StatusDot } from '@langgenius/dify-ui/status-dot'
import { useState } from 'react'
import { manage } from '@/service/aiops-management'

type WizardStep = 1 | 2 | 3 | 4 | 5
type HostEnrollMode = 'ephemeral_password' | 'bootstrap_script'

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
  environment: string
  passed: boolean
  failed_step: string
  steps: Array<{
    step: string
    state: string
    reason?: string
    anon_assistant_status?: number
    upms_tenant_list_status?: number
  }>
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
  { id: 5, title: '5. 发布与终验', subtitle: '公网 4 项冒烟 · 交付回执' },
]

export default function OnboardingWizard({
  initialEnvironment = '',
  initialName = '',
  initialApiDomain = '',
  onClose,
  onOpenAdvancedConfig,
  onOpenLegacyOnboard,
}: OnboardingWizardProps) {
  const [step, setStep] = useState<WizardStep>(1)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  // Step 1: 运维手里真实的 5 行输入
  const [customerName, setCustomerName] = useState(initialName)
  const [envId, setEnvId] = useState(initialEnvironment)
  const [serverIp, setServerIp] = useState('')
  const [apiDomain, setApiDomain] = useState(initialApiDomain)
  const [adminDomain, setAdminDomain] = useState('')
  const [internalGateway, setInternalGateway] = useState('')
  const [sshPort, setSshPort] = useState('22')
  const [sshUser, setSshUser] = useState('root')
  const [enrollMode, setEnrollMode] = useState<HostEnrollMode>('ephemeral_password')
  const [rootPassword, setRootPassword] = useState('')
  const [callbackBaseUrl, setCallbackBaseUrl] = useState(
    typeof window !== 'undefined' ? window.location.origin : 'https://dify.ranlei.work',
  )
  const [bootstrapOneLiner, setBootstrapOneLiner] = useState('')
  const [enrolledHost, setEnrolledHost] = useState<HostView | null>(null)

  // Step 2: 自动探测与受限库修复
  const [discovery, setDiscovery] = useState<DiscoveryReport | null>(null)
  const [selectedTenantId, setSelectedTenantId] = useState('')
  const [confirmDbFix, setConfirmDbFix] = useState(false)
  const [dbFixed, setDbFixed] = useState(false)

  // Step 3: 制品池与基建编排
  const [poolCount, setPoolCount] = useState<number | null>(null)
  const [infraResult, setInfraResult] = useState<InfraResult | null>(null)

  // Step 4: 内置知识库上传与免 JSON 自验
  const [consumerKbId, setConsumerKbId] = useState('')
  const [operatorKbId, setOperatorKbId] = useState('')
  const [starterInstalled, setStarterInstalled] = useState(false)
  const [consumerDocStatus, setConsumerDocStatus] = useState('')
  const [operatorDocStatus, setOperatorDocStatus] = useState('')
  const [orderNo, setOrderNo] = useState('')
  const [deviceId, setDeviceId] = useState('')
  const [consumerOrderNo, setConsumerOrderNo] = useState('')
  const [consumerPhone, setConsumerPhone] = useState('')
  const [consumerCode, setConsumerCode] = useState('8888')
  const [ephemeralOpToken, setEphemeralOpToken] = useState('')
  const [selfValidation, setSelfValidation] = useState<SelfValidationResult | null>(null)

  // Step 5: 公网 HTTPS 4 项冒烟与交付回执
  const [smokeResult, setSmokeResult] = useState<SmokeResult | null>(null)

  const cleanEnv = envId.trim().toLowerCase()
  const gatewayHttpsUrl = apiDomain.trim()
    ? apiDomain.trim().startsWith('https://')
      ? apiDomain.trim()
      : `https://${apiDomain.trim()}`
    : ''

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
    const res = (await callWizard('discover', {
      environment: cleanEnv,
      api_domain: apiDomain.trim().replace(/^https?:\/\//, ''),
      internal_gateway_addr: internalGateway.trim(),
    })) as DiscoveryReport | null
    if (res) {
      setDiscovery(res)
      const firstTenant = res.discovered?.tenants?.[0]?.id
      if (firstTenant && !selectedTenantId) setSelectedTenantId(firstTenant)
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
    const res = await callWizard('db_remediate', {
      environment: cleanEnv,
      remediation_ids: plan.remediation_ids,
      sql_statements: plan.sql_statements,
      confirmed_by_operator: true,
      confirmation_digest: plan.confirmation_digest,
    })
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

  async function handleProvisionInfra() {
    if (!cleanEnv || !serverIp.trim() || !gatewayHttpsUrl) {
      setError('请确认环境代号、服务器 IP 与 HTTPS API 域名已填写')
      return
    }
    const pool = await callWizard('artifact_pool_status', { environment: cleanEnv })
    if (typeof pool?.count === 'number') setPoolCount(pool.count)
    const res = (await callWizard('infra_provision', {
      environment: cleanEnv,
      egress_ip: serverIp.trim(),
      api_domain: apiDomain.trim().replace(/^https?:\/\//, ''),
      gateway_url: gatewayHttpsUrl,
    })) as InfraResult | null
    if (res) {
      setInfraResult(res)
      if (res.passed) {
        setNotice('白名单、专属执行节点、客户机双 Nginx 扩展与即刻探活已全部通过！')
      } else {
        setError(`基建编排在步骤 ${res.failed_step} 阻断，请查看明细修复后重试`)
      }
    }
  }

  async function handleInstallStarterFaq() {
    const res = await callWizard('kb_install_starter_faq', {
      environment: cleanEnv,
      kb_id: consumerKbId.trim() || `kb-consumer-${cleanEnv}`,
    })
    if (res?.document_id) {
      setStarterInstalled(true)
      setConsumerDocStatus(`已装入默认试跑模板（${res.filename} · ${res.chunk_count} 切片）`)
      setNotice('对客试跑 FAQ 模板已就绪，正式对外服务后建议替换为客户官方 FAQ 文档')
    }
  }

  async function handleUploadDoc(role: 'consumer' | 'operator', file: File | null) {
    if (!file) return
    const text = await file.text()
    const kbId =
      role === 'consumer'
        ? consumerKbId.trim() || `kb-consumer-${cleanEnv}`
        : operatorKbId.trim() || `kb-operator-${cleanEnv}`
    const res = await callWizard('kb_upload', {
      environment: cleanEnv,
      target_role: role,
      kb_id: kbId,
      filename: file.name,
      content_base64: btoa(unescape(encodeURIComponent(text))),
    })
    if (res?.document_id) {
      const summary = `已上传 ${res.filename}（${res.chunk_count} 切片）`
      if (role === 'consumer') setConsumerDocStatus(summary)
      else setOperatorDocStatus(summary)
      setNotice(`${role === 'consumer' ? '对客' : '管家'}知识库文档上传成功`)
    }
  }

  async function handleSelfValidate() {
    if (!selectedTenantId || !orderNo || !deviceId || !consumerOrderNo) {
      setError('请先确认租户与自动推荐的验收样本（订单号/离线设备号/C端订单号）')
      return
    }
    const opTok = ephemeralOpToken
    const phone = consumerPhone
    const code = consumerCode
    setEphemeralOpToken('')
    setConsumerPhone('')
    const res = (await callWizard('self_validate', {
      environment: cleanEnv,
      tenant_id: selectedTenantId,
      company_gateway_url: gatewayHttpsUrl,
      order_no: orderNo.trim(),
      device_id: deviceId.trim(),
      offline_start: '2026-11-20T00:00:00+08:00',
      offline_end: '2026-11-26T23:59:59+08:00',
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

  async function handlePostPublishSmoke() {
    if (!gatewayHttpsUrl || !selectedTenantId) {
      setError('请先确认 HTTPS API 域名与目标租户 ID')
      return
    }
    const res = (await callWizard('post_publish_smoke', {
      environment: cleanEnv,
      gateway_url: gatewayHttpsUrl,
      tenant_id: selectedTenantId,
    })) as SmokeResult | null
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
    <section
      aria-label="新客户 5 步自助开通向导"
      className="space-y-5 rounded-2xl border border-components-panel-border bg-components-panel-bg p-6 shadow-xs"
    >
      {/* 顶栏 */}
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-divider-subtle pb-4">
        <div>
          <h3 className="system-lg-semibold text-text-primary">新客户自助开通向导（5 步零命令行）</h3>
          <p className="mt-0.5 system-xs-regular text-text-tertiary">
            只需录入 5 项客户基础信息即可完成安全引导、数据面体检、基建配套、三能力自验与公网终验；底层参数折叠于高级工程视图。
          </p>
        </div>
        <div className="flex items-center gap-2">
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
      <nav aria-label="开通步骤" className="grid gap-2 sm:grid-cols-5">
        {STEP_LABELS.map((item) => {
          const active = step === item.id
          const done = step > item.id
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => setStep(item.id)}
              className={`rounded-xl border px-3 py-2.5 text-left transition-colors ${
                active
                  ? 'border-state-accent-solid bg-components-badge-bg-blue-light-soft'
                  : done
                    ? 'border-divider-subtle bg-components-badge-bg-green-soft'
                    : 'border-divider-subtle bg-background-section'
              }`}
            >
              <div className="system-xs-semibold text-text-primary">{item.title}</div>
              <div className="mt-0.5 text-[11px] text-text-tertiary">{item.subtitle}</div>
            </button>
          )
        })}
      </nav>

      {error && (
        <div
          role="alert"
          className="rounded-xl bg-components-badge-bg-red-soft px-4 py-2.5 system-xs-medium text-text-destructive"
        >
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-xl bg-components-badge-bg-green-soft px-4 py-2.5 system-xs-medium text-text-success">
          {notice}
        </div>
      )}

      {/* STEP 1 */}
      {step === 1 && (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">客户名称</span>
              <Input
                aria-label="客户名称"
                placeholder="如：捷佳四轮国内充电"
                value={customerName}
                onChange={(e) => setCustomerName(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">环境代号（小写字母/数字）</span>
              <Input
                aria-label="环境代号"
                placeholder="如：jiejia"
                value={envId}
                onChange={(e) => setEnvId(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">服务器公网 IP</span>
              <Input
                aria-label="服务器公网 IP"
                placeholder="如：8.148.223.139"
                value={serverIp}
                onChange={(e) => setServerIp(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">客户 API 域名</span>
              <Input
                aria-label="客户 API 域名"
                placeholder="如：api.juxichongdian.cn"
                value={apiDomain}
                onChange={(e) => setApiDomain(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">客户管理后台域名（可选）</span>
              <Input
                aria-label="客户管理后台域名"
                placeholder="如：admin.juxichongdian.cn"
                value={adminDomain}
                onChange={(e) => setAdminDomain(e.target.value)}
              />
            </label>
            <label className="space-y-1">
              <span className="system-xs-medium text-text-secondary">网关内网地址</span>
              <Input
                aria-label="网关内网地址"
                placeholder="如：10.0.0.31:30899"
                value={internalGateway}
                onChange={(e) => setInternalGateway(e.target.value)}
              />
            </label>
          </div>

          <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section p-4">
            <div className="flex flex-wrap items-center gap-2">
              <span className="system-xs-semibold text-text-primary">主机安全引导模式：</span>
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

            {enrollMode === 'ephemeral_password' ? (
              <div className="space-y-3">
                <p className="system-xs-regular text-text-tertiary">
                  口令仅在服务端内存瞬时用于钉扎主机指纹、下发专属 Ed25519 公钥及补齐 aiops/venv，随即清空丢弃，绝不落盘或入库。
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
                <Button type="button" variant="primary" disabled={busy} onClick={handleEnrollPassword}>
                  {busy ? '正在引导并钉扎指纹…' : '立即安全引导并进入下一步'}
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                <p className="system-xs-regular text-text-tertiary">
                  面向高安全客户：无需提供 root 密码。生成 15 分钟单次消费引导命令交客户运维本机执行。
                </p>
                <label className="block space-y-1">
                  <span className="system-xs-medium text-text-secondary">控制面回调 HTTPS 基地址</span>
                  <Input
                    aria-label="控制面回调 HTTPS 基地址"
                    value={callbackBaseUrl}
                    onChange={(e) => setCallbackBaseUrl(e.target.value)}
                  />
                </label>
                <div className="flex flex-wrap gap-2">
                  <Button type="button" variant="primary" disabled={busy} onClick={handleGenerateScript}>
                    生成单次引导命令
                  </Button>
                  <Button type="button" variant="secondary" disabled={busy} onClick={handleRefreshHostStatus}>
                    刷新接入状态
                  </Button>
                </div>
                {bootstrapOneLiner && (
                  <pre className="overflow-x-auto rounded-lg bg-components-input-bg-normal p-3 text-xs text-text-primary">
                    {bootstrapOneLiner}
                  </pre>
                )}
              </div>
            )}
          </div>

          {enrolledHost && (
            <div className="rounded-xl border border-divider-subtle p-3.5 system-xs-regular text-text-secondary">
              已登记主机：<strong>{enrolledHost.host_alias}</strong>（状态：{enrolledHost.status}，指纹：
              <code>{enrolledHost.host_fingerprint_sha256 || '待回调'}</code>，Python{' '}
              {enrolledHost.python_minor || '待探测'}，Nginx：{enrolledHost.nginx_flavor || '待探测'}）
            </div>
          )}
        </div>
      )}

      {/* STEP 2 */}
      {step === 2 && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="system-xs-regular text-text-secondary">
              自动探测客户租户、MySQL 最小只读权限（含 <code>qumall_upms</code> 三张平台表）、必选无锁索引与本机直连时序服务端口。
            </p>
            <Button type="button" variant="primary" disabled={busy} onClick={handleRunDiscovery}>
              {busy ? '正在探测体检…' : '运行自动环境探测与体检'}
            </Button>
          </div>

          {discovery && (
            <div className="space-y-4">
              {discovery.discovered.tenants.length > 0 && (
                <div className="rounded-xl border border-divider-subtle p-4">
                  <div className="mb-2 system-xs-semibold text-text-primary">自动发现的客户租户</div>
                  <div className="flex flex-wrap gap-2">
                    {discovery.discovered.tenants.map((t) => (
                      <button
                        key={t.id}
                        type="button"
                        onClick={() => setSelectedTenantId(t.id)}
                        className={`rounded-lg border px-3 py-1.5 system-xs-medium ${
                          selectedTenantId === t.id
                            ? 'border-state-accent-solid bg-components-badge-bg-blue-light-soft text-text-accent'
                            : 'border-divider-subtle text-text-secondary'
                        }`}
                      >
                        {t.name}（{t.id}）
                      </button>
                    ))}
                  </div>
                </div>
              )}

              <div className="space-y-2 rounded-xl border border-divider-subtle p-4">
                <div className="system-xs-semibold text-text-primary">数据面与基础设施体检单</div>
                {discovery.checks.map((c) => (
                  <div key={c.id} className="flex items-center justify-between gap-2 text-xs">
                    <span className="flex items-center gap-2 text-text-primary">
                      <StatusDot
                        status={c.state === 'passed' ? 'success' : c.state === 'blocked' ? 'error' : 'warning'}
                        size="small"
                      />
                      {c.summary}
                    </span>
                    <span className="text-text-tertiary">{c.state}</span>
                  </div>
                ))}
              </div>

              {discovery.db_remediation_plan.has_auto_fixable_db_gaps && (
                <div className="space-y-3 rounded-xl border border-divider-subtle bg-components-badge-bg-orange-soft p-4">
                  <div className="system-xs-semibold text-text-warning">
                    引导期受限白名单修复预览（仅最小 SELECT 授权与 ALGORITHM=INPLACE, LOCK=NONE 无锁索引）
                  </div>
                  <pre className="overflow-x-auto rounded-lg bg-components-panel-bg p-3 text-xs text-text-primary">
                    {discovery.db_remediation_plan.sql_statements.join('\n')}
                  </pre>
                  <label className="flex cursor-pointer items-center gap-2 system-xs-medium text-text-primary">
                    <input
                      type="checkbox"
                      aria-label="二次确认受限白名单 SQL"
                      checked={confirmDbFix}
                      onChange={(e) => setConfirmDbFix(e.target.checked)}
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
          <p className="system-xs-regular text-text-secondary">
            自动从控制面离线制品池匹配对应 Python 小版本制品，并顺序完成：36/41 白名单放行 → 41 专属执行节点 → 客户机双 Nginx 扩展（含 nginx -t 回滚门）→ 公网即刻探活。
          </p>
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="primary" disabled={busy} onClick={handleProvisionInfra}>
              {busy ? '正在编排周边基建…' : '一键配套周边基建并探活'}
            </Button>
            {onOpenLegacyOnboard && (
              <Button type="button" variant="secondary" onClick={onOpenLegacyOnboard}>
                查看 11 阶段冷装/一键开通任务明细
              </Button>
            )}
          </div>
          {poolCount !== null && (
            <div className="system-xs-regular text-text-tertiary">
              控制面离线制品池已就绪条目数：{poolCount}
            </div>
          )}
          {infraResult && (
            <div className="space-y-2 rounded-xl border border-divider-subtle p-4">
              {infraResult.steps.map((s) => (
                <div key={s.step} className="flex items-center justify-between text-xs">
                  <span className="font-medium text-text-primary">{s.step}</span>
                  <span className="text-text-secondary">
                    {s.state}
                    {s.anon_assistant_status ? ` (匿名=${s.anon_assistant_status}, 原路由=${s.upms_tenant_list_status})` : ''}
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
            <div className="space-y-2 rounded-xl border border-divider-subtle p-4">
              <div className="system-xs-semibold text-text-primary">对客知识库（隔离）</div>
              <Input
                placeholder="对客知识库 ID（默认自动匹配）"
                value={consumerKbId}
                onChange={(e) => setConsumerKbId(e.target.value)}
              />
              <div className="flex flex-wrap items-center gap-2">
                <Button type="button" size="small" variant="secondary" disabled={busy} onClick={handleInstallStarterFaq}>
                  一键装入默认充电试跑 FAQ 模板
                </Button>
                <input
                  aria-label="上传对客知识库文档"
                  type="file"
                  accept=".md,.txt,.docx,.pdf"
                  className="text-xs"
                  onChange={(e) => handleUploadDoc('consumer', e.target.files?.[0] || null)}
                />
              </div>
              {consumerDocStatus && <div className="text-xs text-text-success">{consumerDocStatus}</div>}
            </div>

            <div className="space-y-2 rounded-xl border border-divider-subtle p-4">
              <div className="system-xs-semibold text-text-primary">管家运维知识库（隔离）</div>
              <Input
                placeholder="管家知识库 ID（默认自动匹配）"
                value={operatorKbId}
                onChange={(e) => setOperatorKbId(e.target.value)}
              />
              <input
                aria-label="上传管家运维知识库文档"
                type="file"
                accept=".md,.txt,.docx,.pdf"
                className="text-xs"
                onChange={(e) => handleUploadDoc('operator', e.target.files?.[0] || null)}
              />
              {operatorDocStatus && <div className="text-xs text-text-success">{operatorDocStatus}</div>}
            </div>
          </div>

          <div className="space-y-3 rounded-xl border border-divider-subtle p-4">
            <div className="system-xs-semibold text-text-primary">免 JSON 瞬时三能力自验（凭证用完即自动吊销销毁）</div>
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">验收订单号（含过程统计）</span>
                <Input aria-label="验收订单号" value={orderNo} onChange={(e) => setOrderNo(e.target.value)} />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">离线设备号</span>
                <Input aria-label="离线设备号" value={deviceId} onChange={(e) => setDeviceId(e.target.value)} />
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
                <span className="system-xs-medium text-text-secondary">C 端测试手机号（仅内存换会话）</span>
                <Input
                  aria-label="C 端测试手机号"
                  value={consumerPhone}
                  onChange={(e) => setConsumerPhone(e.target.value)}
                />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">验证码</span>
                <Input aria-label="验证码" value={consumerCode} onChange={(e) => setConsumerCode(e.target.value)} />
              </label>
              <label className="space-y-1">
                <span className="system-xs-medium text-text-secondary">临时管家令牌（验完自动吊销）</span>
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
              <div className="text-xs text-text-secondary">
                自验结论：{selfValidation.passed ? '三能力全绿通过' : '存在未通过项'} · 临时管家会话已吊销：
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
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="system-xs-regular text-text-secondary">
              正式发布后自动向公网域名 <code>{gatewayHttpsUrl || 'https://<api_domain>'}</code> 发起 4 项正式 HTTPS 终验冒烟。
            </p>
            <Button type="button" variant="primary" disabled={busy} onClick={handlePostPublishSmoke}>
              {busy ? '正在执行公网 4 项冒烟…' : '执行公网正式 HTTPS 4 项终验'}
            </Button>
          </div>

          {smokeResult && (
            <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section p-4">
              <div className="system-sm-semibold text-text-primary">
                交付回执卡片 · {customerName || cleanEnv}（{smokeResult.passed ? '公网终验通过' : '待排查'}）
              </div>
              <div className="space-y-1.5">
                {smokeResult.checks.map((c) => (
                  <div key={c.id} className="flex items-center justify-between text-xs">
                    <span className="text-text-primary">{c.id}</span>
                    <span className={c.passed ? 'text-text-success' : 'text-text-destructive'}>
                      {c.passed ? '通过' : '失败'} {c.actual_http ? `(HTTP ${c.actual_http})` : ''}
                    </span>
                  </div>
                ))}
              </div>
              <ul className="list-disc space-y-1 pl-5 text-xs text-text-tertiary">
                <li>
                  诚实边界：业务语义准确率当前为 <strong>{smokeResult.business_accuracy}</strong>（待工程师/业务方结合真实故障人工签收）。
                </li>
                {enrolledHost?.rotate_root_password_recommended && (
                  <li>安全提醒：本次使用了模式 A 一次性口令引导，建议客户运维轮换服务器 root 密码。</li>
                )}
                {starterInstalled && (
                  <li>知识库提醒：对客库当前含临时试跑 FAQ 模板，建议上线后替换为客户官方客服文档。</li>
                )}
              </ul>
            </div>
          )}
        </div>
      )}
    </section>
  )
}
