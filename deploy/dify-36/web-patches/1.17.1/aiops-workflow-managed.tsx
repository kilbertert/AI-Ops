'use client'

import type { Status } from '@/service/aiops-management'
import { Button } from '@langgenius/dify-ui/button'
import { Checkbox } from '@langgenius/dify-ui/checkbox'
import {
  Drawer,
  DrawerBackdrop,
  DrawerCloseButton,
  DrawerContent,
  DrawerPopup,
  DrawerPortal,
  DrawerTitle,
  DrawerTrigger,
  DrawerViewport,
} from '@langgenius/dify-ui/drawer'
import { Input } from '@langgenius/dify-ui/input'
import { Textarea } from '@langgenius/dify-ui/textarea'
import { useCallback, useEffect, useRef, useState } from 'react'
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
  // 头行只有这一个触发钮：受管操作收进右侧抽屉，不再与上游按钮抢一行。
  return (
    <section
      aria-label="受管发布"
      data-aiops-workflow-managed={status.app_id}
      className="flex items-center"
    >
      <Drawer swipeDirection="right">
        <DrawerTrigger
          render={<Button variant="secondary" size="small" />}
        >
          受管发布
        </DrawerTrigger>
        <DrawerPortal>
          <DrawerBackdrop className="fixed" />
          <DrawerViewport>
            <DrawerPopup>
              <div className="flex items-center justify-between border-b-[0.5px] border-divider-regular px-6 py-4">
                <DrawerTitle className="system-lg-semibold text-text-primary">
                  受管发布
                </DrawerTitle>
                <DrawerCloseButton aria-label="关闭受管发布" />
              </div>
              <DrawerContent>
                <div className="flex flex-col gap-6">
                  <ManagedPublish status={status} save={save} disabled={disabled || !!trial} />
                  <div className="border-t-[0.5px] border-divider-regular" />
                  <div className="flex flex-col gap-3">
                    <span className="system-sm-semibold text-text-primary">
                      预览客户端客服
                    </span>
                    <div className="flex flex-col gap-1">
                      <span className="system-xs-medium text-text-secondary">问题</span>
                      <Textarea
                        aria-label="预览问题"
                        value={question}
                        onValueChange={setQuestion}
                        disabled={busy || !!trial}
                        maxLength={4000}
                        rows={3}
                      />
                    </div>
                    <div className="flex flex-col gap-1">
                      <span className="system-xs-medium text-text-secondary">
                        订单号（查单时填写）
                      </span>
                      <Input
                        aria-label="预览订单号"
                        value={order}
                        onValueChange={setOrder}
                        disabled={busy || !!trial}
                        maxLength={64}
                      />
                    </div>
                    <Button
                      variant="primary"
                      size="small"
                      loading={busy}
                      disabled={disabled || (!trial && !question.trim())}
                      onClick={trial ? cancel : start}
                    >
                      {trial ? '停止预览' : '开始预览'}
                    </Button>
                    <div
                      role="status"
                      aria-label="预览结果"
                      className="system-sm-regular max-h-80 overflow-y-auto whitespace-pre-wrap text-text-secondary"
                    >
                      {message}
                    </div>
                  </div>
                  <div className="border-t-[0.5px] border-divider-regular" />
                  <KnowledgeAccess appId={status.app_id} disabled={disabled} />
                </div>
              </DrawerContent>
            </DrawerPopup>
          </DrawerViewport>
        </DrawerPortal>
      </Drawer>
    </section>
  )
}

export default WorkflowManagedStrip

function KnowledgeAccess({ appId, disabled }: { appId: string; disabled: boolean }) {
  const [libraries, setLibraries] = useState<{ id: string; name: string; allowed: boolean }[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  // 抽屉内容只在打开时挂载，挂载即加载 = 原来的「popover 打开时加载」同一时机。
  const load = useCallback(async () => {
    setBusy(true)
    try {
      setLibraries((await manage('knowledge', { app_id: appId })).libraries ?? [])
      setMessage('')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '读取知识库失败')
    } finally {
      setBusy(false)
    }
  }, [appId])
  useEffect(() => {
    void load()
  }, [load])
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
    <div className="flex flex-col gap-3">
      <span className="system-sm-semibold text-text-primary">知识库使用范围</span>
      <p className="system-xs-regular text-text-tertiary">
        只有允许对客的知识库才可供客户端检索。关闭后立即停止使用；新增资料先在知识空间上传。
      </p>
      {libraries.map((row) => (
        <label key={row.id} className="flex items-center gap-2">
          <Checkbox
            aria-label={row.name}
            checked={row.allowed}
            disabled={disabled || busy}
            onCheckedChange={(checked) => void change(row.id, checked === true)}
          />
          <span className="system-sm-regular text-text-primary">{row.name}</span>
        </label>
      ))}
      {!busy && !libraries.length && (
        <p className="system-xs-regular text-text-tertiary">当前客户暂无知识库</p>
      )}
      <span
        role="status"
        aria-label="知识授权结果"
        className="system-xs-regular text-text-secondary"
      >
        {message}
      </span>
    </div>
  )
}
