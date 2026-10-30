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
}

export const entryLabels: Record<EntryKey, string> = {
  consumer: '客户端客服',
  operator: '管家',
}

export const messages: Record<string, string> = {
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
}

export const steps: Record<string, string> = {
  workspace: '准备工作区',
  knowledge: '准备知识库空间',
  dependencies: '配置模型和工具',
  application: '创建智能体',
  register: '登记应用',
  publish: '开通',
  'consumer:application': '准备客户端应用',
  'consumer:key': '申请并保存凭据',
  'consumer:register': '登记客户端应用',
  'consumer:publish': '发布客户端客服',
  'consumer:failed': '客户端未完成',
  'operator:application': '准备管家应用',
  'operator:register': '登记管家应用',
  'operator:publish': '发布管家',
  'operator:failed': '管家未完成',
  complete: '配置完成',
}

export async function manage(action: string, payload: Record<string, unknown> = {}) {
  const cookie = document.cookie
    .split(';')
    .map((x) => x.trim())
    .find((x) => x.startsWith('__Host-csrf_token=') || x.startsWith('csrf_token='))
  const response = await fetch('/__aiops/manage', {
    method: 'POST',
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': cookie ? decodeURIComponent(cookie.slice(cookie.indexOf('=') + 1)) : '',
    },
    body: JSON.stringify({ action, ...payload }),
  })
  const body = await response.json()
  if (!response.ok)
    throw new Error(
      messages[body.error?.code] || body.reason || '管理操作未完成，请重新登录或联系平台管理员',
    )
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
