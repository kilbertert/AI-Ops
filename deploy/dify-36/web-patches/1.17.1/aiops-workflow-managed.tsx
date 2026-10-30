'use client'

import type { Status } from '@/service/aiops-management'
import { useEffect, useRef, useState } from 'react'
import { ManagedPublish } from '@/app/components/aiops-management'
import { manage } from '@/service/aiops-management'

type Props = {
  status: Status
  save: () => Promise<unknown>
  sync: () => Promise<unknown>
  disabled?: boolean
}
const finished = new Set(['completed', 'failed', 'cancelled', 'expired', 'inconclusive'])

export function WorkflowManagedStrip({ status, save, sync, disabled = false }: Props) {
  const [question, setQuestion] = useState('')
  const [order, setOrder] = useState('')
  const [trial, setTrial] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const currentTrialRef = useRef('')
  useEffect(() => {
    currentTrialRef.current = trial
  }, [trial])
  useEffect(() => {
    if (!trial) return
    let active = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const result = await manage('trial-result', { app_id: status.app_id, trial_id: trial })
        if (!active) return
        if (finished.has(result.status)) {
          setMessage(
            result.result?.report?.summary ||
              (result.status === 'cancelled'
                ? '预览已停止'
                : '本次预览未完成，请核对问题、订单权限或联系平台管理员'),
          )
          setTrial('')
        } else timer = setTimeout(poll, 1000)
      } catch (e) {
        if (active) {
          setMessage(e instanceof Error ? e.message : '预览结果暂时不可读')
          timer = setTimeout(poll, 3000)
        }
      }
    }
    void poll()
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [trial, status.app_id])
  useEffect(
    () => () => {
      if (currentTrialRef.current)
        void manage('trial-cancel', {
          app_id: status.app_id,
          trial_id: currentTrialRef.current,
        }).catch(() => {})
    },
    [status.app_id],
  )
  if (!status.managed) return null
  const start = async () => {
    setBusy(true)
    setMessage('正在保存并准备预览…')
    try {
      if (!(await sync())) throw new Error('草稿未保存成功，请重试')
      const result = await manage('trial', {
        app_id: status.app_id,
        question,
        order_no: order.trim(),
      })
      setTrial(result.trial_id)
      setMessage('预览进行中…')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '预览未能开始')
    } finally {
      setBusy(false)
    }
  }
  const cancel = async () => {
    setBusy(true)
    try {
      await manage('trial-cancel', { app_id: status.app_id, trial_id: trial })
      setTrial('')
      setMessage('预览已停止')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '停止未完成，请重试')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section aria-label="受管发布" data-aiops-workflow-managed={status.app_id}>
      <ManagedPublish status={status} save={save} disabled={disabled || !!trial} />
      <KnowledgeAccess appId={status.app_id} disabled={disabled} />
      <details className="relative">
        <summary className="cursor-pointer">预览客户端客服</summary>
        <div className="absolute right-0 z-50 flex w-96 flex-col gap-3 rounded-lg border bg-components-panel-bg p-4 shadow-lg">
          <label>
            问题
            <textarea
              aria-label="预览问题"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              disabled={busy || !!trial}
              maxLength={4000}
              className="w-full rounded border p-2"
            />
          </label>
          <label>
            订单号（查单时填写）
            <input
              aria-label="预览订单号"
              value={order}
              onChange={(e) => setOrder(e.target.value)}
              disabled={busy || !!trial}
              maxLength={64}
              className="w-full rounded border p-2"
            />
          </label>
          <button
            type="button"
            disabled={disabled || busy || (!trial && !question.trim())}
            onClick={trial ? cancel : start}
            className="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-50"
          >
            {trial ? '停止预览' : '开始预览'}
          </button>
          <div
            role="status"
            aria-label="预览结果"
            className="max-h-80 overflow-y-auto whitespace-pre-wrap"
          >
            {message}
          </div>
        </div>
      </details>
    </section>
  )
}

export default WorkflowManagedStrip

function KnowledgeAccess({ appId, disabled }: { appId: string; disabled: boolean }) {
  const [libraries, setLibraries] = useState<{ id: string; name: string; allowed: boolean }[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const load = async () => {
    setBusy(true)
    try {
      setLibraries((await manage('knowledge', { app_id: appId })).libraries)
      setMessage('')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '读取知识库失败')
    } finally {
      setBusy(false)
    }
  }
  const change = async (id: string, allowed: boolean) => {
    setBusy(true)
    try {
      await manage('knowledge-access', { app_id: appId, knowledge_base_id: id, allowed })
      setLibraries((current) => current.map((row) => (row.id === id ? { ...row, allowed } : row)))
      setMessage(allowed ? '已允许客户端使用，请在知识节点选库后发布' : '已停止客户端使用该知识库')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '知识授权未保存')
    } finally {
      setBusy(false)
    }
  }
  return (
    <details
      className="relative"
      onToggle={(event) => {
        if (event.currentTarget.open) void load()
      }}
    >
      <summary className="cursor-pointer">知识库使用范围</summary>
      <div className="absolute right-0 z-50 flex max-h-96 w-96 flex-col gap-3 overflow-y-auto rounded-lg border bg-components-panel-bg p-4 shadow-lg">
        <p>只有允许对客的知识库才可供客户端检索。关闭后立即停止使用；新增资料先在知识空间上传。</p>
        {libraries.map((row) => (
          <label key={row.id} className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={row.allowed}
              disabled={disabled || busy}
              onChange={(event) => void change(row.id, event.target.checked)}
            />
            {row.name}
          </label>
        ))}
        {!busy && !libraries.length && <p>当前客户暂无知识库</p>}
        <span role="status" aria-label="知识授权结果">
          {message}
        </span>
      </div>
    </details>
  )
}
