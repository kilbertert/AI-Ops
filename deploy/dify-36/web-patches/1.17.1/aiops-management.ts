'use client'

import { useEffect, useState } from 'react'

export type EntryKey = 'consumer' | 'operator'

export type Status = {
  published_prompt: string
  published_tools: string[]
  app_id: string
  managed: boolean
  platform: boolean
  model: string
  profile: string
  model_options: { max_tokens?: number }
  version_no?: number
  route_revision: number
  published: boolean
  /** 这个受管应用属于哪一端。缺省按管家（旧后端）。 */
  entry?: EntryKey
  knowledge_base_ids?: string[]
  /** `connected` / `pending`：渠道是否已经接入，来自环境声明，不由"已发布"推导。 */
  channel?: string
  channel_note?: string
  readiness?: string
  live_app_id?: string
  live_version_no?: number
  draft_sha256?: string
}

export type Source = { app_id: string; name: string; version_no: number }
export type EntrySources = {
  entry: EntryKey
  label: string
  scenario?: string
  sources: Source[]
}
export type EntryProgress = {
  app_id?: string
  version_no?: number
  published: boolean
  draft_only: boolean
  knowledge?: string
  channel?: string
  channel_note?: string
  state?: string
  error_code?: string
  failed_step?: string
  failure_kind?: string
}

export const entryLabels: Record<EntryKey, string> = {
  consumer: '客户端客服',
  operator: '管家',
}

export const messages: Record<string, string> = {
  ENVIRONMENT_REGISTRY_MIGRATION_REQUIRED: '环境目录尚未升级，请联系平台运维迁移',
  ENVIRONMENT_MANAGEMENT_UNAVAILABLE: '环境目录尚未准备，请联系平台运维',
  ENVIRONMENT_MANAGEMENT_FORBIDDEN: '当前账号没有环境管理授权',
  ENVIRONMENT_REGISTRY_REVISION_CONFLICT: '目录已被更新，请刷新后重新确认修改',
  ENVIRONMENT_REGISTRY_UPDATE_REFUSED: '目录更新被拒绝，请核对名称或联系平台运维',
  ENVIRONMENT_TENANT_NOT_REGISTERED: '该租户尚未登记到此环境',
  SETUP_KEY_DIRECTORY_UNWRITABLE: '应用凭据存储不可写，请联系平台管理员修复后重试',
  SETUP_KNOWLEDGE_FAILED: '知识库准备或模型校验失败，请重试；持续失败请联系平台管理员',
  SETUP_KNOWLEDGE_REQUIRED: '请重试以补齐默认知识库空间',
  SETUP_KNOWLEDGE_CONFIG_REQUIRED: '平台知识库连接未就绪，请联系平台管理员',
  SETUP_KNOWLEDGE_BINDING_REQUIRED: '这一端的应用没有可绑定的对客知识库，请先在客户知识空间准备',
  SETUP_KNOWLEDGE_CROSS_TENANT: '来源应用的知识库不属于目标客户，已拒绝复制',
  SETUP_CONSUMER_CONTENT_REQUIRED: '来源客服编排不是动态知识回答，暂不能自动开通这一端',
  SETUP_PLATFORM_PERMISSION_REQUIRED: '当前账号没有平台开通权限',
  SETUP_TENANT_FORBIDDEN: '当前账号无权管理这个客户',
  SETUP_TENANT_BUSY: '这个客户已有开通任务，请查看下方进度',
  SETUP_SOURCE_REQUIRED: '请为选中的每一端选择来源正式版本',
  SETUP_SOURCE_NOT_PUBLISHED: '所选来源还不是该端的正式版本，请刷新后重新选择',
  SETUP_SOURCE_NOT_CONSUMER: '所选来源不是客户端客服编排，请换一份来源',
  SETUP_DRAFT_CHANGED: '草稿已被修改，请刷新确认后重新发布',
  SETUP_SOURCE_CHANGED: '来源正式版本已变化，请刷新后重新选择',
  SETUP_ROUTE_CHANGED: '正式版本已变化，请刷新确认后重新发布',
  SETUP_EXECUTOR_UNAVAILABLE: '执行组件尚未就绪，请稍后重试或联系平台管理员',
  SETUP_EXECUTOR_INCOMPATIBLE: '执行组件版本不匹配，请联系平台管理员升级后再发布',
  SETUP_KNOWLEDGE_CREATE_UNCONFIRMED:
    '知识库创建结果正在核对，请稍后重试；持续未确认请联系平台管理员',
  SETUP_MODEL_CONNECTION_REQUIRED: '客户端所需模型尚未配置，请联系平台管理员',
  SETUP_BUSINESS_CONNECTION_REQUIRED: '该客户的业务环境连接尚未配置，请联系平台管理员',
  SETUP_BUSINESS_ENVIRONMENT_AMBIGUOUS:
    '平台登记了多个业务环境且未指定默认，请联系平台管理员在清单里指定后再开通',
  SETUP_CONSUMER_PLUGIN_REQUIRED: '客户端知识连接尚未配置，请联系平台管理员',
  SETUP_CONSUMER_PLUGIN_FAILED: '客户端知识连接校验失败，请重试或联系平台管理员',
  SETUP_PROVIDER_OUTDATED: '管家工具目录与系统冲突，请联系平台管理员核对；未覆盖现有工具',
  DIFY_PLUGIN_VERSION_CONFLICT: '客户空间已有其他插件版本，请联系平台管理员完成兼容升级后重试',
  DIFY_PLUGIN_PACKAGE_MISSING: '平台缺少来源版本所需的插件包，请联系平台管理员补齐后重试',
  DIFY_PLUGIN_INSTALL_INCOMPLETE: '所需插件版本尚未就绪，请重试；持续失败请联系平台管理员',
  DIFY_PLUGIN_INSTALL_FAILED: '平台安装依赖插件失败，请联系平台管理员检查插件包后重试',
  DIFY_PLUGIN_INSTALL_TIMEOUT: '插件安装尚未完成，请稍后重试',
  DIFY_CONSOLE_UNREACHABLE: '编排服务暂时不可达，请稍后重试',
  DIFY_CREDENTIAL_INVALID: '模型或工具连接凭据校验失败，请联系平台管理员',
  SETUP_MODEL_CONFLICT: '模型配置与平台模板不一致，请联系平台管理员',
  SETUP_IMPORT_UPGRADE_REQUIRED: '平台导入服务尚未升级，请联系平台管理员',
  SETUP_KEY_LIMIT: '该应用的 Service 凭据已达上限，请联系平台管理员清理',
  SETUP_KEY_AMBIGUOUS: 'Service 凭据归属不明，已停止切换正式版本，请联系平台管理员',
  SETUP_KEY_DIRECTORY_REQUIRED: '平台未配置凭据目录，请联系平台管理员',
  SETUP_AGENT_UNAVAILABLE: '这一端的智能体还不存在，请联系平台管理员',
  SETUP_PARTIAL: '部分完成：失败的一端可单独重试，已完成的一端不会被重建',
  SETUP_STEP_FAILED: '当前步骤未完成，可以重试；持续失败请联系平台管理员',
  OPERATOR_SITE_SCOPE_REQUIRED: '当前账号未分配可诊断场站，请联系公司管理员分配或切换账号',
  OPERATOR_TRIAL_LOGIN_REQUIRED: '公司登录已过期，请重新登录后继续',
  // 以下按后端可产出的 code 维护；漏收录的 code 由 `failureText` 原样带码兜底，
  // 缺译文只影响可读性，不再冒充"请重新登录"的会话问题。
  SETUP_APP_NOT_MANAGED: '该应用不在受管应用登记中，请核对应用归属或联系平台管理员',
  SETUP_APP_CONFLICT:
    '该应用的登记身份与既有记录冲突（归属或端不一致），请核对应用登记或联系平台管理员',
  SETUP_ACTOR_CHANGED: '开通任务信息已变化，请刷新后重试',
  SETUP_BUSY: '有任务正在执行，请稍后重试',
  SETUP_CHANNEL_DECLARATION_INVALID: '渠道接入声明无效，请联系平台管理员核对清单',
  SETUP_CREDENTIAL_REQUIRED: '发布服务凭据未配置，请联系平台管理员',
  SETUP_DISABLED: '自助管理未启用，请联系平台管理员',
  SETUP_ENTRY_UNKNOWN: '应用入口类型无法识别，请联系平台管理员',
  SETUP_SCOPE_UNKNOWN: '开通范围无法识别，请联系平台管理员',
  SETUP_IDENTITY_MISMATCH: '公司身份与应用账号不匹配，请重新登录或联系平台管理员',
  SETUP_IMPORT_PENDING: '应用导入尚未完成，请稍后重试',
  SETUP_JOB_MISSING: '开通任务记录缺失，请联系平台管理员',
  SETUP_KEY_LIST_UNAVAILABLE: '应用凭据清单读取失败，请稍后重试或联系平台管理员',
  SETUP_KEY_UNAVAILABLE: '应用凭据不可用，请联系平台管理员',
  SETUP_KNOWLEDGE_CONNECTION_FAILED: '知识库连接校验失败，请重试或联系平台管理员',
  SETUP_KNOWLEDGE_NOT_SUPPORTED: '该应用含有暂不支持的知识库声明，请联系平台管理员',
  SETUP_LEASE_LOST: '任务执行权已失效，请重试',
  SETUP_OPERATION_NOT_FOUND: '开通任务不存在或不属于当前账号',
  SETUP_PROVIDER_UNAVAILABLE: '工具目录暂时不可用，请稍后重试',
  SETUP_REQUEST_CHANGED: '开通请求已变化，请重新发起',
  SETUP_SOURCE_NOT_AVAILABLE: '该端没有可用的来源应用，请先完成来源发布',
  SETUP_VERSION_MISMATCH: '所选版本不属于该应用，请刷新后重试',
  SETUP_WORKSPACE_PENDING: '工作区尚未就绪，请稍后重试',
  SETUP_UNAVAILABLE: '管理服务暂时不可用，请稍后重试',
  COMPANY_DIRECTORY_UNAVAILABLE: '公司目录服务暂时不可用，请稍后重试',
  // 路由指针层：撤回/并发改动/配置冲突都以「操作」的面目出现，不是登录问题。
  ROUTE_BINDING_RETRACTED: '该版本已撤回；要重新上线请用「恢复历史版本」',
  ROUTE_BINDING_CONFLICT: '正式版本在页面打开后已变化，请刷新后重试',
  ROUTE_BINDING_MISSING: '正式版本指针缺失，请联系平台管理员',
  ROUTE_BINDING_UNAVAILABLE: '正式版本指针暂时不可用，请稍后重试',
  ROUTE_ENGINE_UNKNOWN: '路由执行组件类型未知，请联系平台管理员',
  CHAT_ROUTE_BINDING_COLLIDES_WITH_FILE: '该客户路由仍登记在配置文件中，请联系平台管理员核对',
  DIFY_KEY_DIR_UNSET: '应用凭据目录未配置，请联系平台管理员',
  DIFY_CHAT_BASE_URL_UNSET: '聊天执行地址未配置，请联系平台管理员',
  DIFY_ROUTE_NOT_ADVANCED: '发布产物缺少可绑定的版本身份，请联系平台管理员',
  // 管家发布的 NodeRouteConflict 把英文短句当 code 用，逐句对应。
  'node route revision missing': '正式版本指针缺失，请联系平台管理员',
  'node route revision changed': '正式版本已变化，请刷新后重新发布',
  'node route retracted': '该版本已撤回；要重新上线请用「恢复历史版本」',
  'node target environment changed': '执行目标环境已变化，请联系平台管理员',
}

export const steps: Record<string, string> = {
  workspace: '准备工作区',
  knowledge: '准备知识库空间',
  dependencies: '配置模型和工具',
  application: '创建智能体',
  register: '登记应用',
  publish: '开通',
  'consumer:application': '准备客户端应用',
  'consumer:source': '核对客户端来源',
  'consumer:dependencies': '准备客户端依赖',
  'consumer:knowledge': '分配客户知识库',
  'consumer:key': '申请并保存凭据',
  'consumer:register': '登记客户端应用',
  'consumer:publish': '发布客户端客服',
  'consumer:failed': '客户端未完成',
  'operator:dependencies': '准备管家依赖',
  'operator:application': '准备管家应用',
  'operator:register': '登记管家应用',
  'operator:publish': '发布管家',
  'operator:failed': '管家未完成',
  complete: '配置完成',
}

type ErrorBody = {
  error?: { code?: unknown; message?: unknown } | null
  error_code?: unknown
  reason?: unknown
  detail?: unknown
}

/**
 * 错误响应 → 给用户看的句子。
 *
 * 后端只承诺 `error.code`；`reason`（桥拒绝）、`error.message`（标准信封）、
 * `detail`（框架拒绝）各只有一部分路径给。顺序：**本表译文 → 后端正文 →
 * 原样带 code → 兜底**。这样后端每加一个 code 不会自动变成用户可见的假话 —
 * 最差的显示是「兜底（CODE）」，一张截图就能定位，而不是一句误导性的
 * 「请重新登录」。
 */
export function failureText(body: unknown, fallback = '管理操作未完成，请稍后重试'): string {
  const data = (body ?? {}) as ErrorBody
  const code = String(data.error?.code ?? data.error_code ?? '')
  if (code && messages[code]) return messages[code]
  const detail = [data.reason, data.error?.message, data.detail].find(
    (v): v is string => typeof v === 'string' && v.trim().length > 0,
  )
  if (detail) return code ? `${detail}（${code}）` : detail
  return code ? `${fallback}（${code}）` : fallback
}

export async function manage(action: string, payload: Record<string, unknown> = {}) {
  const cookie = document.cookie
    .split(';')
    .map((x) => x.trim())
    .find((x) => x.startsWith('__Host-csrf_token=') || x.startsWith('csrf_token='))
  let response: Response
  try {
    response = await fetch('/__aiops/manage', {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': cookie ? decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1)) : '',
      },
      body: JSON.stringify({ action, ...payload }),
    })
  } catch (e) {
    // fetch 的拒绝只有一种语义：传输层失败（同源下即断网或服务不可达）。
    // 业务拒绝永远走 JSON 响应体。TypeError 之外的原样上抛 —— 那是调用方/替身的
    // 异常，不是这条通道的语义，吞掉会让测试替身测不到自己的拒绝。
    if (e instanceof TypeError) throw new Error('无法连接管理服务，请检查网络后重试')
    throw e
  }
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(failureText(body, `管理操作未完成（${response.status}），请稍后重试`))
  }
  // 2xx 但响应体不是 JSON：桥/代理层故障，不是业务答复。原来的 `Unexpected token`
  // 原文直接上屏，现在说人话。
  if (body === null || typeof body !== 'object') throw new Error('管理服务返回了无法解析的响应')
  return body
}

export async function publishManaged(
  appId: string,
  config: { pre_prompt?: string; agent_mode?: unknown },
  revision: number,
) {
  if (!Number.isInteger(revision) || revision < 0) throw new Error('请刷新后重新发布')
  // 与后端共享规范化JSON；不让另一个标签页修改的草稿被无感发布。
  const canonical = (v: unknown): unknown =>
    Array.isArray(v)
      ? v.map(canonical)
      : v && typeof v === 'object'
        ? Object.fromEntries(
            Object.keys(v)
              .sort()
              .map((k) => [k, canonical((v as Record<string, unknown>)[k])]),
          )
        : v
  const bytes = new TextEncoder().encode(
    JSON.stringify(
      canonical({
        pre_prompt: config.pre_prompt,
        tools: (
          (
            config.agent_mode as {
              tools?: {
                provider_id?: string
                provider_type?: string
                tool_name?: string
                enabled?: boolean
              }[]
            }
          )?.tools || []
        ).map((t) => ({
          provider_id: t.provider_id,
          provider_type: t.provider_type,
          tool_name: t.tool_name,
          enabled: t.enabled !== false,
        })),
      }),
    ),
  )
  const digest = await crypto.subtle.digest('SHA-256', bytes)
  const draft = Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, '0')).join('')
  const result = await manage('publish', { app_id: appId, revision, draft })
  window.dispatchEvent(new CustomEvent('aiops-published', { detail: appId }))
  return result
}

export function useManagedApp(appId: string) {
  const [status, setStatus] = useState<Status | null>(null)
  const [error, setError] = useState('')
  const [readiness, setReadiness] = useState('')
  useEffect(() => {
    let active = true
    if (!appId) return
    const load = () =>
      manage('status', { app_id: appId })
        .then((r) => {
          if (active) {
            setStatus({ ...r, app_id: appId })
            setError('')
            if (r.managed)
              manage('readiness', { app_id: appId })
                .then((v) => {
                  if (active) setReadiness(v.reason)
                })
                .catch((e) => {
                  if (active) setReadiness(e.message)
                })
          }
        })
        .catch((e) => {
          if (active) setError(e.message)
        })
    load()
    window.addEventListener('aiops-published', load)
    return () => {
      active = false
      window.removeEventListener('aiops-published', load)
    }
  }, [appId])
  return { status: status?.app_id === appId ? status : null, error, readiness }
}

export async function publishManagedWorkflow(appId: string, revision: number, savedHash: string) {
  // 调用方先等待原生保存成功；重新读摘要，避免拿页面载入时的旧摘要发布。
  const current: Status = await manage('status', { app_id: appId })
  if (!current.managed || current.entry !== 'consumer' || !current.draft_sha256)
    throw new Error('无法核对当前草稿，请刷新后重试')
  const { fetchWorkflowDraft } = await import('@/service/workflow')
  const draft = await fetchWorkflowDraft(`/apps/${appId}/workflows/draft`)
  if (draft.hash !== savedHash) throw new Error('草稿已被其他操作修改，请确认后重新发布')
  const result = await manage('publish', { app_id: appId, revision, draft: current.draft_sha256 })
  window.dispatchEvent(new CustomEvent('aiops-published', { detail: appId }))
  return result
}
