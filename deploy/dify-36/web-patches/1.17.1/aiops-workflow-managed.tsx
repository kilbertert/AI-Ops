'use client'

import type { EntryKey } from '@/service/aiops-management'
import { useParams } from 'next/navigation'
import { entryLabels, messages, steps, useManagedApp } from '@/service/aiops-management'
import { ManagedPublish } from '@/app/components/aiops-management'

/**
 * 编排页（`/app/[appId]/workflow`）顶部的受管条。
 *
 * 独立验收 R3 指出：第一轮只把 `ManagedPublish` 挂到了 `Configuration` 视图，而**固定版本
 * 的 Chatflow 编辑页渲染的是 `WorkflowApp`** —— 于是客服应用在它真正被编辑的那一页上
 * 没有发布入口，只能靠"另行打开配置页"这种没人会想到的路径。
 *
 * 这个组件刻意做得很薄：它只回答"这个应用受不受管、属于哪一端、该发哪一版"，
 * 发布动作仍然走 `ManagedPublish`（与配置页同一份代码、同一条桥、同一个 API）。
 * 不受管的应用（普通 Dify 应用）渲染 `null` —— 一行都不改它们的行为。
 */
export function WorkflowManagedStrip() {
  const params = useParams<{ appId?: string }>()
  const appId = typeof params?.appId === 'string' ? params.appId : ''
  const { status, error } = useManagedApp(appId)
  if (!appId) return null
  if (error) {
    return (
      <p role="alert" className="px-6 py-2 text-sm text-amber-700">
        {error}
      </p>
    )
  }
  if (!status?.managed) return null
  const entry: EntryKey = status.entry || 'operator'
  return (
    <section
      aria-label="受管发布"
      data-aiops-workflow-managed={status.app_id}
      className="flex flex-wrap items-center gap-3 border-b border-divider-regular px-6 py-2"
    >
      <span className="text-sm text-text-secondary">
        {entryLabels[entry]} · {status.published ? `正式 V${status.version_no}` : '尚未发布'}
      </span>
      {entry === 'consumer' && status.channel !== 'connected' && (
        <span className="text-xs text-text-tertiary" data-aiops-channel="pending">
          {status.channel_note || 'AI 已准备，渠道待接入'}
        </span>
      )}
      <ManagedPublish status={status} save={async () => publishCurrent(status)} />
    </section>
  )
}

async function publishCurrent(status: { app_id: string; route_revision: number; draft_sha256?: string }) {
  const { manage } = await import('@/service/aiops-management')
  // 客户端应用的发布摘要来自 `status`：页面看到什么就发回什么，服务端再算一次。
  // 管家应用的后端会自己核对草稿指纹，这里不额外传。
  return manage('publish', {
    app_id: status.app_id,
    revision: status.route_revision,
    ...(status.draft_sha256 ? { draft: status.draft_sha256 } : {}),
  })
}

export { messages, steps }
export default WorkflowManagedStrip
