'use client'

import type { Status } from '@/service/aiops-management'
import { useEffect, useState } from 'react'
import { manage, messages, steps } from '@/service/aiops-management'

type Item = { id: string; name: string }
type Source = { app_id: string; name: string; version_no: number }
type Operation = {
  id: string
  name: string
  tenant_name: string
  state: string
  step: string
  app_id?: string
  published: boolean
  error_code?: string
}
export function ManagedPublish({ status, save }: { status: Status; save: () => Promise<unknown> }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [versions, setVersions] = useState<number[]>([])
  const [selected, setSelected] = useState('')
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
      data-aiops-revision={status.route_revision}
      className="flex items-center gap-2 text-sm"
    >
      <span>
        {status.model} ·{' '}
        {status.model_options.max_tokens
          ? `${status.model_options.max_tokens} token`
          : '输出上限：模型默认'}{' '}
        · {status.published ? `正式 V${status.version_no}` : '尚未发布'}
      </span>
      <button
        type="button"
        className="rounded-lg bg-blue-600 px-3 py-2 text-white disabled:opacity-50"
        disabled={busy}
        onClick={async () => {
          setBusy(true)
          setMessage('')
          try {
            await save()
            setMessage('已发布到管家')
          } catch (e) {
            setMessage(e instanceof Error ? e.message : '操作未完成')
          } finally {
            setBusy(false)
          }
        }}
      >
        {busy ? '发布中…' : '发布到管家'}
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
        <select
          aria-label="历史版本"
          value={selected}
          onChange={(e) => setSelected(e.target.value)}
        >
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
    operations: Operation[]
    workspaces?: { workspace_id: string; tenant_id: string; name: string }[]
  } | null>(null)
  const [open, setOpen] = useState(false)
  const [selectedWorkspace, setSelectedWorkspace] = useState('')
  const [tenant, setTenant] = useState('')
  const [source, setSource] = useState('')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
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
      .then(setCatalogue)
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
        {open ? '收起客户开通' : '复制并开通客户管家'}
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
        <section aria-label="开通客户管家" className="mt-3 rounded-xl border p-4">
          <div className="flex flex-wrap gap-3">
            <label>
              来源版本
              <select
                aria-label="来源版本"
                className="mx-2 rounded border p-2"
                value={source}
                onChange={(e) => setSource(e.target.value)}
              >
                <option value="">选择已发布管家</option>
                {catalogue.sources.map((s) => (
                  <option key={s.app_id} value={s.app_id}>
                    {s.name} · V{s.version_no}
                  </option>
                ))}
              </select>
            </label>
            <label>
              客户
              <select
                aria-label="客户"
                className="mx-2 rounded border p-2"
                value={tenant}
                onChange={(e) => {
                  setTenant(e.target.value)
                  setName(
                    `${catalogue.tenants.find((t) => t.id === e.target.value)?.name || ''}－订单管家`,
                  )
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
              智能体名称
              <input
                aria-label="智能体名称"
                className="mx-2 rounded border p-2"
                maxLength={128}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </label>
            <button
              type="button"
              className="rounded bg-blue-600 px-3 py-2 text-white disabled:opacity-50"
              disabled={busy || !source || !tenant || !name.trim()}
              onClick={async () => {
                setBusy(true)
                setError('')
                try {
                  const sourceVersion = catalogue.sources.find(
                    (s) => s.app_id === source,
                  )?.version_no
                  const fingerprint = `${source}:${sourceVersion}:${tenant}:${name.trim()}`
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
                    source_app_id: source,
                    source_version: sourceVersion,
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
          {catalogue.operations.map((op) => (
            <div key={op.id} className="mt-3 flex flex-wrap items-center gap-3 rounded border p-3">
              <strong>
                {op.tenant_name} · {op.name}
              </strong>
              <span>
                {steps[op.step] || '准备中'}
                {op.state === 'completed'
                  ? op.published
                    ? ' · 已启用'
                    : ' · 草稿（原管家不变）'
                  : ''}
              </span>
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
          ))}
        </section>
      )}
    </div>
  )
}
