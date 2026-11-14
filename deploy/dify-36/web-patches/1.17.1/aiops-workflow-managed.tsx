'use client'

import type { AgentNodeType } from '@/app/components/workflow/nodes/agent/types'
import type { ToolNodeType } from '@/app/components/workflow/nodes/tool/types'
import type { Node } from '@/app/components/workflow/types'
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
import { fetchWorkflowDraft } from '@/service/workflow'

type Props = {
  status: Status
  save: () => Promise<unknown>
  sync: () => Promise<unknown>
  disabled?: boolean
  nodes: Node[]
  updateNode: (payload: { id: string; data: Record<string, unknown> }) => void
}
const finished = new Set(['completed', 'failed', 'cancelled', 'expired', 'inconclusive'])

export function WorkflowManagedStrip({
  status,
  save,
  sync,
  nodes,
  updateNode,
  disabled = false,
}: Props) {
  const [question, setQuestion] = useState('')
  const [order, setOrder] = useState('')
  const [trial, setTrial] = useState('')
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [selectionBlocked, setSelectionBlocked] = useState(true)
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
        <DrawerTrigger render={<Button variant="secondary" size="small" />}>受管发布</DrawerTrigger>
        <DrawerPortal>
          <DrawerBackdrop className="fixed" />
          <DrawerViewport>
            <DrawerPopup>
              <div className="flex items-center justify-between border-b-[0.5px] border-divider-regular px-6 py-4">
                <DrawerTitle className="system-lg-semibold text-text-primary">受管发布</DrawerTitle>
                <DrawerCloseButton aria-label="关闭受管发布" />
              </div>
              <DrawerContent>
                <div className="flex flex-col gap-6">
                  <KnowledgeAccess
                    appId={status.app_id}
                    disabled={disabled || busy || !!trial}
                    nodes={nodes}
                    updateNode={updateNode}
                    sync={sync}
                    onBlocked={setSelectionBlocked}
                  />
                  <ManagedPublish
                    status={status}
                    save={async () => {
                      setBusy(true)
                      try {
                        return await save()
                      } finally {
                        setBusy(false)
                      }
                    }}
                    disabled={disabled || !!trial || selectionBlocked}
                  />
                  <div className="border-t-[0.5px] border-divider-regular" />
                  <div className="flex flex-col gap-3">
                    <span className="system-sm-semibold text-text-primary">预览客户端客服</span>
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
                      disabled={disabled || (!trial && (!question.trim() || selectionBlocked))}
                      onClick={trial ? cancel : start}
                    >
                      {trial ? '停止预览' : '开始预览'}
                    </Button>
                    <div
                      role="status"
                      aria-label="预览结果"
                      className="max-h-80 overflow-y-auto system-sm-regular whitespace-pre-wrap text-text-secondary"
                    >
                      {message}
                    </div>
                  </div>
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

function KnowledgeAccess({
  appId,
  disabled,
  nodes,
  updateNode,
  sync,
  onBlocked,
}: {
  appId: string
  disabled: boolean
  nodes: Node[]
  updateNode: Props['updateNode']
  sync: Props['sync']
  onBlocked: (blocked: boolean) => void
}) {
  const [libraries, setLibraries] = useState<{ id: string; name: string; allowed: boolean }[]>([])
  const [busy, setBusy] = useState(true)
  const [message, setMessage] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [saveFailed, setSaveFailed] = useState(false)
  const node =
    nodes.find((item) => item.id === 'answer' && item.data.type === 'agent') ??
    nodes.find(
      (item) =>
        item.data.type === 'tool' &&
        'tool_name' in item.data &&
        item.data.tool_name === 'consumer_knowledge',
    )
  const nodeData = node?.data as Partial<AgentNodeType & ToolNodeType> | undefined
  const tools = nodeData?.agent_parameters?.tools?.value
  const tool = Array.isArray(tools)
    ? tools.find((item: { tool_name?: string }) => item.tool_name === 'consumer_knowledge')
    : undefined
  const settings = tool?.settings ?? nodeData?.tool_configurations
  const raw = settings?.knowledge_base_ids
  const config = raw?.type ? raw : raw?.value
  const selected: string[] =
    config?.type === 'constant' && Array.isArray(config.value)
      ? config.value.filter((id: unknown): id is string => typeof id === 'string')
      : []
  const invalid = selected.filter((id) => !libraries.some((row) => row.id === id && row.allowed))
  const blocked =
    busy ||
    !loaded ||
    saveFailed ||
    !node ||
    !selected.length ||
    selected.length > 20 ||
    invalid.length > 0
  useEffect(() => onBlocked(blocked), [blocked, onBlocked])
  // 抽屉内容只在打开时挂载，挂载即加载 = 原来的「popover 打开时加载」同一时机。
  const load = useCallback(async () => {
    setBusy(true)
    try {
      setLibraries((await manage('knowledge', { app_id: appId })).libraries ?? [])
      setLoaded(true)
      setMessage('')
    } catch (e) {
      setLoaded(false)
      setMessage(e instanceof Error ? e.message : '读取知识库失败')
    } finally {
      setBusy(false)
    }
  }, [appId])
  useEffect(() => {
    void load()
  }, [load])
  const choose = async (ids: string[]) => {
    if (!node || disabled || busy) return
    const value = { type: 'constant', value: ids }
    const next = { ...settings, knowledge_base_ids: raw?.type ? value : { ...raw, value } }
    const data = tool
      ? {
          agent_parameters: {
            ...nodeData?.agent_parameters,
            tools: {
              ...nodeData?.agent_parameters?.tools,
              value: tools.map((item: { settings?: unknown }) =>
                item === tool ? { ...item, settings: next } : item,
              ),
            },
          },
        }
      : { tool_configurations: next }
    setBusy(true)
    setSaveFailed(true)
    try {
      updateNode({ id: node.id, data })
      const saved = await sync()
      if (!saved) throw new Error('草稿未保存，请重试；本次选库尚未发布')
      const current = await fetchWorkflowDraft(`/apps/${appId}/workflows/draft`)
      const savedNode = current.graph.nodes.find((item) => item.id === node.id)
      const savedData = savedNode?.data as Partial<AgentNodeType & ToolNodeType> | undefined
      const savedSettings = tool
        ? savedData?.agent_parameters?.tools?.value?.find(
            (item: { tool_name?: string }) => item.tool_name === 'consumer_knowledge',
          )?.settings
        : savedData?.tool_configurations
      const savedRaw = savedSettings?.knowledge_base_ids
      const savedConfig = savedRaw?.type ? savedRaw : savedRaw?.value
      if (JSON.stringify(savedConfig?.value) !== JSON.stringify(ids))
        throw new Error('选库未保存或已被其他操作修改，请刷新确认')
      setSaveFailed(false)
      setMessage('本应用选库已保存为草稿，发布后才会生效')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '草稿未保存，请重试')
    } finally {
      setBusy(false)
    }
  }
  const change = async (id: string, allowed: boolean) => {
    setBusy(true)
    try {
      await manage('knowledge-access', { app_id: appId, knowledge_base_id: id, allowed })
      setLibraries((current) => current.map((row) => (row.id === id ? { ...row, allowed } : row)))
      setMessage(
        allowed ? '已允许客户端使用；请在上方为本应用选用，保存后发布' : '已停止客户端使用该知识库',
      )
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '知识授权未保存')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="flex flex-col gap-3">
      <span className="system-sm-semibold text-text-primary">本应用选用的知识库</span>
      <p className="system-xs-regular text-text-tertiary">
        在此选择本次发布要使用的库；提示词与选库一起保存、一起发布。
      </p>
      {!node && <p role="alert">未找到可选库的客服节点，请检查编排</p>}
      {libraries.map((row) => (
        <label key={row.id} className="flex items-center gap-2">
          <Checkbox
            aria-label={`本应用选用：${row.name}`}
            checked={selected.includes(row.id)}
            disabled={disabled || busy || (!row.allowed && !selected.includes(row.id))}
            onCheckedChange={(checked) =>
              void choose(
                checked === true ? [...selected, row.id] : selected.filter((id) => id !== row.id),
              )
            }
          />
          <span>
            {row.name}
            {!row.allowed && '（尚未允许对客）'}
          </span>
        </label>
      ))}
      {selected
        .filter((id) => !libraries.some((row) => row.id === id))
        .map((id, index) => (
          <div key={id} className="flex items-center gap-2">
            <span>已失效的知识库引用 {index + 1}</span>
            <Button
              size="small"
              variant="secondary"
              disabled={disabled || busy}
              onClick={() => void choose(selected.filter((value) => value !== id))}
            >
              移除失效引用 {index + 1}
            </Button>
          </div>
        ))}
      {loaded && (!selected.length || invalid.length > 0) && (
        <p role="alert">请为本应用选择有效且允许对客的知识库；本次提示词与选库尚未发布。</p>
      )}
      {saveFailed && (
        <Button size="small" disabled={disabled || busy} onClick={() => void choose(selected)}>
          重试保存选库
        </Button>
      )}
      <Button size="small" variant="secondary" disabled={busy} onClick={() => void load()}>
        刷新知识库列表
      </Button>
      <span className="system-sm-semibold text-text-primary">租户知识库对客授权</span>
      <p className="system-xs-regular text-text-tertiary">
        这里仅管理本租户的对客权限，不会改变本应用选库。关闭会立即停止所有客户端使用该库。
      </p>
      {libraries.map((row) => (
        <label key={row.id} className="flex items-center gap-2">
          <Checkbox
            aria-label={`允许对客：${row.name}`}
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
