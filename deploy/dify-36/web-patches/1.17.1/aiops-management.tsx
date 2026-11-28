'use client'

import type { EntryKey, Status } from '@/service/aiops-management'
import { Button } from '@langgenius/dify-ui/button'
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
import { useState } from 'react'
import { entryLabels, manage } from '@/service/aiops-management'

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
  const envBadge = status.environment_name
    ? status.tenant_name && status.tenant_name !== status.environment_name
      ? `${status.environment_name} · ${status.tenant_name}`
      : status.environment_name
    : ''
  const targetLabel = envBadge ? `${envBadge} · ${entryLabels[entry]}` : entryLabels[entry]

  const change = async (version?: number) => {
    if (
      status.remote_managed &&
      typeof window !== 'undefined' &&
      !window.confirm(
        version
          ? `确认将【${targetLabel}】生产版本恢复为 V${version}？`
          : `确认撤回【${targetLabel}】当前正式版本？`,
      )
    ) {
      return
    }
    setBusy(true)
    try {
      await manage('version', {
        app_id: status.app_id,
        revision: status.route_revision,
        ...(version ? { version_no: version } : {}),
      })
      window.dispatchEvent(new CustomEvent('aiops-published'))
      setMessage(version ? `历史版本 V${version} 已恢复至 ${targetLabel}` : `正式版本已从 ${targetLabel} 撤回`)
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
      data-aiops-environment={status.environment || ''}
      data-aiops-revision={status.route_revision}
      className="flex flex-wrap items-center gap-2"
    >
      {envBadge && (
        <span
          data-aiops-env-badge="true"
          className={
            status.remote_managed
              ? 'inline-flex items-center rounded-full border border-state-warning-active/40 bg-components-badge-bg-orange-soft px-2 py-0.5 system-xs-medium text-text-warning'
              : 'inline-flex items-center rounded-full border border-divider-subtle bg-background-section-burn px-2 py-0.5 system-xs-medium text-text-secondary'
          }
        >
          {envBadge}
        </span>
      )}
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
      {entry === 'consumer' && !status.published && status.live_app_id && (
        <span className="system-xs-regular text-text-warning">
          当前客户端仍使用另一应用的正式 V{status.live_version_no}；发布成功后才切换到本应用。
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
          if (
            status.remote_managed &&
            typeof window !== 'undefined' &&
            !window.confirm(`确认将当前提示词与配置发布到【${targetLabel}】生产环境？`)
          ) {
            return
          }
          setBusy(true)
          setMessage('')
          try {
            await save()
            setMessage(`已发布到${targetLabel}，本次提示词与配置已生效`)
          } catch (e) {
            setMessage(
              `${e instanceof Error ? e.message : '操作未完成'}；发布未确认成功，请刷新核对正式版本`,
            )
          } finally {
            setBusy(false)
          }
        }}
      >
        {busy ? '发布中…' : `发布到${entryLabels[entry]}`}
      </Button>
      <Popover>
        <PopoverTrigger
          render={
            <Button variant="secondary" size="small">
              版本操作
            </Button>
          }
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
              disabled={busy}
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
