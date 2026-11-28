'use client'

import { useEffect, useState } from 'react'
import { usePathname } from '@/next/navigation'

function readCsrfCookie(): string {
  const m = document.cookie.match(/(?:^|;\s*)(?:__Host-)?csrf_token=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

async function fetchCurrentWorkspaceId(): Promise<string> {
  try {
    const csrf = readCsrfCookie()
    const res = await fetch('/console/api/workspaces/current/summary', {
      credentials: 'include',
      headers: csrf ? { 'X-CSRF-Token': csrf } : {},
    })
    if (!res.ok) return ''
    const data = (await res.json()) as { id?: unknown }
    return typeof data?.id === 'string' ? data.id : ''
  } catch {
    return ''
  }
}

/**
 * 知识库页右下角「打开 RAGFlow」浮动卡片（原 Nginx sub_filter 脚本已正式迁入 React 组件，ADR-0366）。
 *
 * - 仅在 `/datasets` 与 `/datasets/...` 路由渲染，离开即卸载；
 * - 收起状态仅存于组件内存，回到 `/datasets` 自动恢复；
 * - 点击同步预开 `about:blank` 保持浏览器手势，再带 `/console/api/workspaces/current/summary` 结果跳往 `/__aiops/knowledge-open`。
 */
export function AiopsKnowledgeOpenCard() {
  const pathname = usePathname() || ''
  const isDatasetsPage = pathname === '/datasets' || pathname.startsWith('/datasets/')
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    if (!isDatasetsPage) setDismissed(false)
  }, [isDatasetsPage])

  if (!isDatasetsPage || dismissed) return null

  const handleOpenClick = (ev: React.MouseEvent<HTMLAnchorElement>) => {
    ev.preventDefault()
    const w = window.open('about:blank', '_blank')
    const base = '/__aiops/knowledge-open'
    const navigate = (ws: string) => {
      const target = ws ? `${base}?workspace=${encodeURIComponent(ws)}` : base
      if (w) {
        try {
          w.location.href = target
          return
        } catch {
          // 若受策略阻断则退回当前页跳转
        }
      }
      window.location.href = target
    }
    void fetchCurrentWorkspaceId().then(navigate, () => navigate(''))
  }

  return (
    <div
      id="aiops-kb-open-card"
      className="fixed right-5 bottom-5 z-[2147483000] max-w-[260px] rounded-[10px] border border-[#3a3f4b] bg-[#1c1f26] px-3.5 py-3 text-[13px] leading-normal text-[#f2f4f8] shadow-[0_6px_24px_rgba(0,0,0,0.35)]"
    >
      <button
        type="button"
        aria-label="收起"
        onClick={() => setDismissed(true)}
        className="absolute top-1.5 right-2 cursor-pointer border-0 bg-transparent p-0.5 text-[15px] leading-none text-[#9aa4b2] hover:text-white"
      >
        ×
      </button>
      <div className="mb-1 pr-4 font-semibold">知识库在 RAGFlow 里管理</div>
      <div className="mb-2.5 text-xs text-[#9aa4b2]">
        建库、传文档、解析都在 RAGFlow 完成，回来这里就能选。
      </div>
      <a
        href="/__aiops/knowledge-open"
        target="_blank"
        rel="noopener noreferrer"
        onClick={handleOpenClick}
        className="inline-block rounded-md bg-[#2f6feb] px-3 py-1.5 text-[12.5px] font-medium text-white no-underline hover:bg-[#3879f5]"
      >
        打开 RAGFlow
      </a>
    </div>
  )
}
