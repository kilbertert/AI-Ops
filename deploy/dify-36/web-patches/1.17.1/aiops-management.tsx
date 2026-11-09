'use client'

import type {
  EntryKey,
  EntryProgress,
  EntrySources,
  Source,
  Status,
} from '@/service/aiops-management'
import { Button } from '@langgenius/dify-ui/button'
import { Checkbox } from '@langgenius/dify-ui/checkbox'
import {
  Combobox,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxInputGroup,
  ComboboxItem,
  ComboboxItemIndicator,
  ComboboxItemText,
  ComboboxList,
  ComboboxPopup,
  ComboboxPortal,
  ComboboxPositioner,
  ComboboxTrigger,
  ComboboxValue,
} from '@langgenius/dify-ui/combobox'
import { Input } from '@langgenius/dify-ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@langgenius/dify-ui/popover'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { StatusDot } from '@langgenius/dify-ui/status-dot'
import { useEffect, useState } from 'react'
import { entryLabels, failureText, manage, messages, steps } from '@/service/aiops-management'

type Item = { id: string; name: string }
type Operation = {
  id: string
  name: string
  tenant_name: string
  state: string
  step: string
  scope?: string
  entries?: Partial<Record<EntryKey, EntryProgress>>
  app_id?: string
  published: boolean
  error_code?: string
  failed_step?: string
  failure_kind?: string
}

/** 没有 `entries[]` 的旧后端：退回"只有管家"的形状，页面照常可用。 */
function entryList(catalogue: { sources: Source[]; entries?: EntrySources[] }): EntrySources[] {
  if (catalogue.entries?.length) return catalogue.entries
  return [{ entry: 'operator', label: entryLabels.operator, sources: catalogue.sources }]
}

function defaultName(tenantName: string, chosen: EntryKey[]) {
  if (chosen.length === 2) return `${tenantName}－客服与管家`
  return `${tenantName}－${chosen[0] === 'consumer' ? '客服' : '订单管家'}`
}

/**
 * 失败时的定位串：**在哪一步、哪一类异常**。
 *
 * 只给通用码（`SETUP_STEP_FAILED`）时，同一句"这一端未完成"对应十几种完全不同的缺陷，
 * 事后只能靠人在生产上手工复现 —— 而复现动作本身会改变现场。步骤名与异常类名都不是
 * 上游原文，给页面不泄漏任何内容；确切的栈在 journal 里。
 */
function failureDetail(step?: string, kind?: string): string {
  const where = step ? steps[step] || step : ''
  if (where && kind) return `${where}出错（${kind}）`
  if (where) return `${where}出错`
  return kind ? `出错（${kind}）` : ''
}

/**
 * 一端的状态要分开说四件事：资源准备好了吗、这一端发布了吗、渠道接入了吗、真实用户验过吗。
 * 合成一句"已启用"会让刚开通、还没人接入口的客户看起来已经能用。
 */
function entrySummary(progress: EntryProgress | undefined): string {
  if (!progress) return '等待中'
  if (progress.state === 'failed') {
    const generic = failureText({ error_code: progress.error_code }, '这一端未完成')
    const detail =
      progress.error_code === 'SETUP_STEP_FAILED'
        ? failureDetail(progress.failed_step, progress.failure_kind)
        : ''
    return detail ? `${generic}：${detail}` : generic
  }
  if (!progress.app_id) return '等待中'
  if (!progress.published) return progress.draft_only ? '草稿（原正式版继续服务）' : '尚未发布'
  if (progress.channel !== 'connected')
    return progress.channel_note
      ? `已发布 · ${progress.channel_note}`
      : '已发布 · AI 已准备，渠道待接入'
  return `已启用 · 正式 V${progress.version_no ?? '?'}`
}

/** `entrySummary` 四态对应的灯色，文案与灯永远同源。 */
function entryTone(progress: EntryProgress | undefined) {
  if (!progress || !progress.app_id) return 'disabled' as const
  if (progress.state === 'failed') return 'error' as const
  if (progress.published && progress.channel === 'connected') return 'success' as const
  return 'warning' as const
}

const fieldLabel = 'system-xs-medium text-text-secondary'

type SearchOption = { value: string; label: string }

/**
 * 可搜索的选择器：客户名单有几百行，纯下拉要挨个滚动。
 * 触发钮保持 select 的外观与 aria-label，弹层顶部放检索框，输入即过滤。
 */
function SearchSelect({
  label,
  placeholder,
  value,
  onChange,
  options,
}: {
  label: string
  placeholder: string
  value: string
  onChange: (value: string) => void
  options: SearchOption[]
}) {
  return (
    <Combobox
      items={options}
      itemToStringLabel={(option: SearchOption) => option.label}
      itemToStringValue={(option: SearchOption) => option.value}
      value={options.find((option) => option.value === value) ?? null}
      onValueChange={(option: SearchOption | null) => onChange(option?.value ?? '')}
    >
      <ComboboxTrigger aria-label={label} className="w-full">
        <ComboboxValue placeholder={placeholder} />
      </ComboboxTrigger>
      <ComboboxPortal>
        <ComboboxPositioner>
          <ComboboxPopup aria-label={label}>
            <div className="p-1 pb-0">
              <ComboboxInputGroup className="h-8 min-h-8 px-2">
                <span
                  aria-hidden
                  className="i-ri-search-line mr-0.5 size-4 shrink-0 text-components-input-text-placeholder"
                />
                <ComboboxInput
                  aria-label={`搜索${label}`}
                  placeholder="输入关键字筛选…"
                  className="block h-4.5 grow px-1 py-0 system-sm-regular text-components-input-text-filled"
                />
              </ComboboxInputGroup>
            </div>
            <ComboboxList<SearchOption>>
              {(option) => (
                <ComboboxItem key={option.value} value={option}>
                  <ComboboxItemText>{option.label}</ComboboxItemText>
                  <ComboboxItemIndicator />
                </ComboboxItem>
              )}
            </ComboboxList>
            <ComboboxEmpty>没有匹配的客户</ComboboxEmpty>
          </ComboboxPopup>
        </ComboboxPositioner>
      </ComboboxPortal>
    </Combobox>
  )
}

export function ManagedPublish({
  status,
  save,
  disabled = false,
}: {
  status: Status
  save: () => Promise<unknown>
  disabled?: boolean
}) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [versions, setVersions] = useState<number[]>([])
  const [selected, setSelected] = useState('')
  const entry: EntryKey = status.entry || 'operator'
  const change = async (version?: number) => {
    setBusy(true)
    try {
      await manage('version', {
        app_id: status.app_id,
        revision: status.route_revision,
        ...(version ? { version_no: version } : {}),
      })
      window.dispatchEvent(new CustomEvent('aiops-published'))
      setMessage(version ? '历史版本已恢复' : '正式版本已撤回')
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '操作未完成')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div
      data-aiops-managed="true"
      data-aiops-app={status.app_id}
      data-aiops-entry={entry}
      data-aiops-revision={status.route_revision}
      className="flex flex-wrap items-center gap-2"
    >
      <span data-aiops-entry-label="true" className="system-sm-semibold text-text-primary">
        {entryLabels[entry]}
      </span>
      <span className="system-xs-regular text-text-tertiary">
        {status.model} ·{' '}
        {status.model_options.max_tokens
          ? `${status.model_options.max_tokens} token`
          : '输出上限：模型默认'}{' '}
        · {status.published ? `正式 V${status.version_no}` : '尚未发布'}
      </span>
      {entry === 'consumer' && (
        <span
          data-aiops-knowledge="true"
          className="system-xs-regular text-text-tertiary"
        >
          对客知识库 {status.knowledge_base_ids?.length || 0} 个
        </span>
      )}
      {entry === 'consumer' && status.channel !== 'connected' && (
        <span data-aiops-channel="pending" className="system-xs-regular text-text-warning">
          {status.channel_note || 'AI 已准备，渠道待接入'}
        </span>
      )}
      <Button
        variant="primary"
        size="small"
        loading={busy}
        disabled={disabled}
        onClick={async () => {
          setBusy(true)
          setMessage('')
          try {
            await save()
            setMessage(`已发布到${entryLabels[entry]}`)
          } catch (e) {
            setMessage(e instanceof Error ? e.message : '操作未完成')
          } finally {
            setBusy(false)
          }
        }}
      >
        {busy ? '发布中…' : `发布到${entryLabels[entry]}`}
      </Button>
      <Popover>
        <PopoverTrigger
          render={<Button variant="secondary" size="small">版本操作</Button>}
        />
        <PopoverContent placement="bottom-end">
          <div className="flex w-56 flex-col gap-2">
            <Button
              variant="tertiary"
              size="small"
              disabled={busy || !status.published}
              onClick={() => change()}
            >
              撤回当前正式版本
            </Button>
            <Button
              variant="tertiary"
              size="small"
              onClick={async () => {
                try {
                  setVersions((await manage('versions', { app_id: status.app_id })).versions)
                } catch (e) {
                  setMessage(e instanceof Error ? e.message : '操作未完成')
                }
              }}
            >
              查看历史
            </Button>
            <Select value={selected} onValueChange={(v) => setSelected(v || '')}>
              <SelectTrigger aria-label="历史版本" className="w-full">
                <SelectValue placeholder="选择版本" />
              </SelectTrigger>
              <SelectContent>
                {versions.map((v) => (
                  <SelectItem key={v} value={String(v)}>
                    <SelectItemText>V{v}</SelectItemText>
                    <SelectItemIndicator />
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <Button
              variant="secondary"
              size="small"
              disabled={busy || !selected}
              onClick={() => change(Number(selected))}
            >
              恢复所选版本
            </Button>
          </div>
        </PopoverContent>
      </Popover>
      <span role="status" className="system-xs-regular text-text-secondary">
        {message}
      </span>
    </div>
  )
}

export default function TenantSetup() {
  const [catalogue, setCatalogue] = useState<{
    platform: boolean
    tenants: Item[]
    sources: Source[]
    entries?: EntrySources[]
    operations: Operation[]
    workspaces?: { workspace_id: string; tenant_id: string; name: string }[]
  } | null>(null)
  const [open, setOpen] = useState(false)
  const [selectedWorkspace, setSelectedWorkspace] = useState('')
  const [tenant, setTenant] = useState('')
  const [name, setName] = useState('')
  const [chosen, setChosen] = useState<Record<EntryKey, boolean>>({
    consumer: true,
    operator: true,
  })
  const [sources, setSources] = useState<Partial<Record<EntryKey, string>>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const entries = catalogue ? entryList(catalogue) : []
  const byEntry = (key: EntryKey) => entries.find((item) => item.entry === key)
  const picked = entries.filter((item) => chosen[item.entry]).map((item) => item.entry)
  const ready = picked.length > 0 && picked.every((key) => sources[key])
  const enter = async (knowledge = false) => {
    setBusy(true)
    try {
      await manage('workspace', { workspace_id: selectedWorkspace })
      window.location.assign(
        knowledge ? `/__aiops/knowledge-open?workspace=${selectedWorkspace}` : '/apps',
      )
    } catch (e) {
      setError(e instanceof Error ? e.message : '进入工作区失败')
    } finally {
      setBusy(false)
    }
  }
  const load = () =>
    manage('catalogue')
      .then((body) => {
        setCatalogue(body)
        // 没有已发布来源的一端默认不勾：勾了也提交不了，只会让人以为坏了。
        setChosen((current) => {
          const next = { ...current }
          for (const item of entryList(body)) if (!item.sources.length) next[item.entry] = false
          return next
        })
      })
      .catch((e) => setError(e instanceof Error ? e.message : '操作未完成'))
  useEffect(() => {
    load()
  }, [])
  useEffect(() => {
    if (!open || !catalogue?.operations.some((op) => ['pending', 'running'].includes(op.state)))
      return
    const timer = setInterval(async () => {
      try {
        const operations = await Promise.all(
          catalogue.operations.map((op) =>
            ['pending', 'running'].includes(op.state) ? manage('operation', { id: op.id }) : op,
          ),
        )
        setCatalogue((current) => (current ? { ...current, operations } : current))
      } catch (e) {
        setError(e instanceof Error ? e.message : '读取进度失败')
      }
    }, 2500)
    return () => clearInterval(timer)
  }, [open, catalogue])
  if (error && !catalogue)
    return (
      <p role="alert" className="system-sm-regular px-6 py-2 text-text-warning">
        {error}
      </p>
    )
  if (!catalogue?.platform) return null
  return (
    <div className="px-6 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Button variant="primary" size="medium" onClick={() => setOpen(!open)}>
          {open ? '收起客户 AI 服务' : '复制并开通客户 AI 服务'}
        </Button>
        <div className="flex w-52">
          <SearchSelect
            label="已开通客户"
            placeholder="选择已开通客户"
            value={selectedWorkspace}
            onChange={setSelectedWorkspace}
            options={(catalogue.workspaces || []).map((w) => ({
              value: w.workspace_id,
              label: w.name,
            }))}
          />
        </div>
        <Button
          variant="secondary"
          size="medium"
          disabled={busy || !selectedWorkspace}
          onClick={() => enter()}
        >
          进入工作区
        </Button>
        <Button
          variant="secondary"
          size="medium"
          disabled={busy || !selectedWorkspace}
          onClick={() => enter(true)}
        >
          进入知识库
        </Button>
      </div>
      {!open && error && (
        <p role="alert" className="system-sm-regular mt-2 text-text-destructive">
          {error}
        </p>
      )}
      {open && (
        <section
          aria-label="开通客户 AI 服务"
          className="mt-3 rounded-xl border-[0.5px] border-components-card-border bg-components-card-bg p-4 shadow-xs shadow-shadow-shadow-3"
        >
          <fieldset className="mb-3 flex flex-wrap items-center gap-4">
            <legend className="system-sm-medium text-text-secondary">开通范围</legend>
            {entries.map((item) => (
              <label key={item.entry} className="flex items-center gap-2">
                <Checkbox
                  aria-label={item.label}
                  checked={Boolean(chosen[item.entry])}
                  disabled={!item.sources.length}
                  onCheckedChange={(checked) =>
                    setChosen({ ...chosen, [item.entry]: checked === true })
                  }
                />
                <span className="system-sm-regular text-text-primary">{item.label}</span>
                {!item.sources.length && (
                  <span className="system-xs-regular text-text-tertiary">
                    （暂无已发布来源，可稍后补开）
                  </span>
                )}
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap items-end gap-3">
            {entries
              .filter((item) => chosen[item.entry])
              .map((item) => (
                <div key={item.entry} className="flex w-56 flex-col gap-1">
                  <span className={fieldLabel}>{item.label}来源</span>
                  <Select
                    value={sources[item.entry] || ''}
                    onValueChange={(v) => setSources({ ...sources, [item.entry]: v || '' })}
                  >
                    <SelectTrigger aria-label={`${item.label}来源`} className="w-full">
                      <SelectValue placeholder={`选择已发布${item.label}`} />
                    </SelectTrigger>
                    <SelectContent>
                      {item.sources.map((s) => (
                        <SelectItem key={s.app_id} value={s.app_id}>
                          <SelectItemText>
                            {s.name} · V{s.version_no}
                          </SelectItemText>
                          <SelectItemIndicator />
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              ))}
            <div className="flex w-40 flex-col gap-1">
              <span className={fieldLabel}>客户</span>
              <SearchSelect
                label="客户"
                placeholder="选择客户"
                value={tenant}
                onChange={(next) => {
                  setTenant(next)
                  const tenantName = catalogue.tenants.find((t) => t.id === next)?.name || ''
                  setName(defaultName(tenantName, picked.length ? picked : ['operator']))
                }}
                options={catalogue.tenants.map((t) => ({ value: t.id, label: t.name }))}
              />
            </div>
            <div className="flex w-56 flex-col gap-1">
              <span className={fieldLabel}>应用名称</span>
              <Input
                aria-label="应用名称"
                maxLength={128}
                value={name}
                onValueChange={setName}
              />
            </div>
            <Button
              variant="primary"
              size="medium"
              loading={busy}
              disabled={!ready || !tenant || !name.trim()}
              onClick={async () => {
                setBusy(true)
                setError('')
                try {
                  const payload: Record<string, unknown> = {}
                  for (const key of picked) {
                    const appId = sources[key]
                    const version = byEntry(key)?.sources.find(
                      (s) => s.app_id === appId,
                    )?.version_no
                    payload[key] = { app_id: appId, version_no: version }
                  }
                  const scope = picked.length === 2 ? 'both' : picked[0]
                  const fingerprint = `${scope}:${JSON.stringify(payload)}:${tenant}:${name.trim()}`
                  let saved
                  try {
                    saved = JSON.parse(sessionStorage.getItem('aiops-setup-request') || 'null')
                  } catch {
                    sessionStorage.removeItem('aiops-setup-request')
                  }
                  const requestKey =
                    saved?.fingerprint === fingerprint
                      ? saved.key
                      : crypto.randomUUID().replaceAll('-', '')
                  sessionStorage.setItem(
                    'aiops-setup-request',
                    JSON.stringify({ fingerprint, key: requestKey }),
                  )
                  await manage('create', {
                    scope,
                    sources: payload,
                    tenant_id: tenant,
                    name: name.trim(),
                    request_key: requestKey,
                  })
                  await load()
                } catch (e) {
                  setError(e instanceof Error ? e.message : '操作未完成')
                } finally {
                  setBusy(false)
                }
              }}
            >
              {busy ? '提交中…' : '复制并开通'}
            </Button>
          </div>
          {error ? (
            <p role="alert" className="system-sm-regular my-2 text-text-destructive">
              {error}
            </p>
          ) : null}
          {catalogue.operations.length > 0 && (
            <div className="mt-3 flex flex-col gap-2">
              {catalogue.operations.map((op) => {
                const progress = op.entries || {}
                const anyEntryFailed = Object.values(progress).some(
                  (v) => v?.state === 'failed',
                )
                return (
                  <div
                    key={op.id}
                    className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border-[0.5px] border-divider-regular bg-components-panel-bg px-4 py-3"
                  >
                    <span className="system-sm-semibold text-text-primary">
                      {op.tenant_name} · {op.name}
                    </span>
                    {/* 步骤名只在进行中是有用信息；completed 时重复、failed 时误导 */}
                    {['pending', 'running'].includes(op.state) && (
                      <span className="system-xs-regular text-text-tertiary">
                        {steps[op.step] || '准备中'}
                      </span>
                    )}
                    {op.state === 'completed' && (
                      <span className="system-xs-regular flex items-center gap-1.5 text-text-secondary">
                        <StatusDot status="success" size="medium" />
                        已完成
                      </span>
                    )}
                    {op.state === 'partial' && (
                      <span
                        role="status"
                        data-aiops-partial="true"
                        className="system-xs-regular flex items-center gap-1.5 text-text-warning"
                      >
                        <StatusDot status="warning" size="medium" />
                        {messages.SETUP_PARTIAL}
                      </span>
                    )}
                    {op.state === 'failed' && (
                      <span className="system-xs-regular flex items-center gap-1.5 text-text-destructive">
                        <StatusDot status="error" size="medium" />
                        未完成
                      </span>
                    )}
                    {Object.entries(progress).map(([key, value]) => (
                      <span
                        key={key}
                        data-aiops-progress={key}
                        className="system-xs-regular flex items-center gap-1.5 text-text-secondary"
                      >
                        <StatusDot status={entryTone(value)} size="medium" />
                        <span>
                          {entryLabels[key as EntryKey] || key}：{entrySummary(value)}
                        </span>
                      </span>
                    ))}
                    {/* 入口级失败的行级错误与入口 chip 文案同源，只在没入口失败时补行级原因 */}
                    {op.state === 'failed' && !anyEntryFailed && (
                      <span className="system-xs-regular text-text-destructive">
                        {failureText({ error_code: op.error_code }, '当前步骤未完成')}
                        {op.error_code === 'SETUP_STEP_FAILED' &&
                          failureDetail(op.failed_step, op.failure_kind) &&
                          `：${failureDetail(op.failed_step, op.failure_kind)}`}
                      </span>
                    )}
                    {['failed', 'partial'].includes(op.state) && (
                      <Button
                        variant="ghost-accent"
                        size="small"
                        onClick={async () => {
                          try {
                            await manage('retry', { id: op.id })
                            await load()
                          } catch (e) {
                            setError(e instanceof Error ? e.message : '操作未完成')
                          }
                        }}
                      >
                        重试
                      </Button>
                    )}
                  </div>
                )
              })}
              {catalogue.operations.some((op) => op.state === 'completed') && (
                <p className="system-xs-regular text-text-tertiary">
                  开通完成的应用，客户正常登录后可在自己的工作区查看。
                </p>
              )}
            </div>
          )}
        </section>
      )}
    </div>
  )
}
