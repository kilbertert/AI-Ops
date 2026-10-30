'use client'

import type { EntryKey, EntryProgress, EntrySources, Source, Status } from '@/service/aiops-management'
import { useEffect, useState } from 'react'
import { entryLabels, manage, messages, steps } from '@/service/aiops-management'

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
}

/** 没有 `entries[]` 的旧后端：退回"只有管家"的形状，页面照常可用。 */
function entryList(catalogue: {
  sources: Source[]
  entries?: EntrySources[]
}): EntrySources[] {
  if (catalogue.entries?.length) return catalogue.entries
  return [{ entry: 'operator', label: entryLabels.operator, sources: catalogue.sources }]
}

function defaultName(tenantName: string, chosen: EntryKey[]) {
  if (chosen.length === 2) return `${tenantName}－客服与管家`
  return `${tenantName}－${chosen[0] === 'consumer' ? '客服' : '订单管家'}`
}

/**
 * 一端的状态要分开说四件事：资源准备好了吗、这一端发布了吗、渠道接入了吗、真实用户验过吗。
 * 合成一句"已启用"会让刚开通、还没人接入口的客户看起来已经能用。
 */
function entrySummary(progress: EntryProgress | undefined): string {
  if (!progress) return '等待中'
  if (progress.state === 'failed')
    return messages[progress.error_code || ''] || '这一端未完成'
  if (!progress.app_id) return '等待中'
  if (!progress.published) return progress.draft_only ? '草稿（原正式版继续服务）' : '尚未发布'
  if (progress.channel !== 'connected')
    return progress.channel_note ? `已发布 · ${progress.channel_note}` : '已发布 · AI 已准备，渠道待接入'
  return `已启用 · 正式 V${progress.version_no ?? '?'}`
}

export function ManagedPublish({ status, save }: { status: Status; save: () => Promise<unknown> }) {
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
      className="flex flex-wrap items-center gap-2 text-sm"
    >
      <span data-aiops-entry-label="true">{entryLabels[entry]}</span>
      <span>
        {status.model} ·{' '}
        {status.model_options.max_tokens
          ? `${status.model_options.max_tokens} token`
          : '输出上限：模型默认'}{' '}
        · {status.published ? `正式 V${status.version_no}` : '尚未发布'}
      </span>
      {entry === 'consumer' && (
        <span data-aiops-knowledge="true">
          对客知识库 {status.knowledge_base_ids?.length || 0} 个
        </span>
      )}
      {entry === 'consumer' && status.channel !== 'connected' && (
        <span data-aiops-channel="pending">
          {status.channel_note || 'AI 已准备，渠道待接入'}
        </span>
      )}
      <button
        type="button"
        className="rounded-lg bg-blue-600 px-3 py-2 text-white disabled:opacity-50"
        disabled={busy}
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
      </button>
      <details>
        <summary className="cursor-pointer">版本操作</summary>
        <button type="button" disabled={busy || !status.published} onClick={() => change()}>
          撤回当前正式版本
        </button>
        <button
          type="button"
          onClick={async () => {
            try {
              setVersions((await manage('versions', { app_id: status.app_id })).versions)
            } catch (e) {
              setMessage(e instanceof Error ? e.message : '操作未完成')
            }
          }}
        >
          查看历史
        </button>
        <select aria-label="历史版本" value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="">选择版本</option>
          {versions.map((v) => (
            <option key={v} value={v}>
              V{v}
            </option>
          ))}
        </select>
        <button type="button" disabled={busy || !selected} onClick={() => change(Number(selected))}>
          恢复所选版本
        </button>
      </details>
      <span role="status">{message}</span>
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
          for (const item of entryList(body))
            if (!item.sources.length) next[item.entry] = false
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
      <p role="alert" className="px-6 py-2 text-amber-700">
        {error}
      </p>
    )
  if (!catalogue?.platform) return null
  return (
    <div className="px-6 py-3">
      <button
        type="button"
        className="rounded-lg bg-blue-600 px-4 py-2 text-white"
        onClick={() => setOpen(!open)}
      >
        {open ? '收起客户 AI 服务' : '复制并开通客户 AI 服务'}
      </button>
      <div className="mt-3 flex items-center gap-2">
        <select
          aria-label="已开通客户"
          value={selectedWorkspace}
          onChange={(e) => setSelectedWorkspace(e.target.value)}
        >
          <option value="">选择已开通客户</option>
          {(catalogue.workspaces || []).map((w) => (
            <option key={w.workspace_id} value={w.workspace_id}>
              {w.name}
            </option>
          ))}
        </select>
        <button type="button" disabled={busy || !selectedWorkspace} onClick={() => enter()}>
          进入工作区
        </button>
        <button type="button" disabled={busy || !selectedWorkspace} onClick={() => enter(true)}>
          进入知识库
        </button>
      </div>
      {!open && error && <p role="alert">{error}</p>}
      {open && (
        <section aria-label="开通客户 AI 服务" className="mt-3 rounded-xl border p-4">
          <fieldset className="mb-3 flex flex-wrap items-center gap-4">
            <legend className="text-sm text-gray-600">开通范围</legend>
            {entries.map((item) => (
              <label key={item.entry} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  aria-label={item.label}
                  checked={Boolean(chosen[item.entry])}
                  disabled={!item.sources.length}
                  onChange={(e) => setChosen({ ...chosen, [item.entry]: e.target.checked })}
                />
                <span>{item.label}</span>
                {!item.sources.length && (
                  <span className="text-xs text-gray-500">（暂无已发布来源，可稍后补开）</span>
                )}
              </label>
            ))}
          </fieldset>
          <div className="flex flex-wrap gap-3">
            {entries
              .filter((item) => chosen[item.entry])
              .map((item) => (
                <label key={item.entry}>
                  {item.label}来源
                  <select
                    aria-label={`${item.label}来源`}
                    className="mx-2 rounded border p-2"
                    value={sources[item.entry] || ''}
                    onChange={(e) => setSources({ ...sources, [item.entry]: e.target.value })}
                  >
                    <option value="">选择已发布{item.label}</option>
                    {item.sources.map((s) => (
                      <option key={s.app_id} value={s.app_id}>
                        {s.name} · V{s.version_no}
                      </option>
                    ))}
                  </select>
                </label>
              ))}
            <label>
              客户
              <select
                aria-label="客户"
                className="mx-2 rounded border p-2"
                value={tenant}
                onChange={(e) => {
                  setTenant(e.target.value)
                  const tenantName = catalogue.tenants.find((t) => t.id === e.target.value)?.name || ''
                  setName(defaultName(tenantName, picked.length ? picked : ['operator']))
                }}
              >
                <option value="">选择客户</option>
                {catalogue.tenants.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              应用名称
              <input
                aria-label="应用名称"
                className="mx-2 rounded border p-2"
                maxLength={128}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-50"
              disabled={busy || !ready || !tenant || !name.trim()}
              onClick={async () => {
                setBusy(true)
                setError('')
                try {
                  const payload: Record<string, unknown> = {}
                  for (const key of picked) {
                    const appId = sources[key]
                    const version = byEntry(key)?.sources.find((s) => s.app_id === appId)?.version_no
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
            </button>
          </div>
          <p role="alert" className="my-2 text-red-600">
            {error}
          </p>
          {catalogue.operations.map((op) => {
            const progress = op.entries || {}
            return (
              <div key={op.id} className="mt-3 flex flex-wrap items-center gap-3 rounded border p-3">
                <strong>
                  {op.tenant_name} · {op.name}
                </strong>
                <span>{steps[op.step] || '准备中'}</span>
                {op.state === 'partial' && (
                  <span role="status" data-aiops-partial="true">
                    {messages.SETUP_PARTIAL}
                  </span>
                )}
                {Object.entries(progress).map(([key, value]) => (
                  <span key={key} data-aiops-progress={key}>
                    {entryLabels[key as EntryKey] || key}：{entrySummary(value)}
                  </span>
                ))}
                {op.state === 'failed' && (
                  <>
                    <span>{messages[op.error_code || ''] || '当前步骤未完成'}</span>
                    <button
                      type="button"
                      className="underline"
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
                    </button>
                  </>
                )}
                {op.state === 'completed' && <span>客户正常登录后可在自己的工作区查看</span>}
              </div>
            )
          })}
        </section>
      )}
    </div>
  )
}
