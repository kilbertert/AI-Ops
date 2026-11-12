'use client'

import { Button } from '@langgenius/dify-ui/button'
import { useEffect, useState } from 'react'
import NativeLogs from '@/app/components/app/log-annotation'
import { useStore as useAppStore } from '@/app/components/app/store'
import { PageType } from '@/app/components/base/features/new-feature-panel/annotation-reply/type'
import { manage, useManagedApp } from '@/service/aiops-management'

type Event = {
  phase: string
  at: string
  elapsed_ms: number
  duration_ms?: number
  tool?: string
  status?: string
}
type Run = {
  run_id: string
  qa_id: string | null
  created_at: string
  state: string
  elapsed_ms: number | null
  elapsed_source: string
  result_expired: boolean
  delivery_mode?: 'diagnosis' | 'evidence_only' | null
  review_reason?: string | null
  trace: Event[]
  source: string
  agent_version: string | null
  gateway_version: string
  node_build: string | null
}
const labels: Record<string, string> = {
  issued: '已签发',
  running: '执行中',
  succeeded: '成功',
  failed: '失败',
  expired: '已过期',
  revoked: '已撤销',
  refused: '已拒绝',
  dispatch: '派发',
  tool: '工具',
  review: '事实复核',
  resample: '重新生成',
  completed: '完成',
  unsupported_currency: '答复添加了来源未提供的币种',
  unsupported_station: '答复使用了来源未提供的站名',
  unsupported_claim: '答复未通过事实一致性复核',
  review_uncertain: '答复事实一致性未能确认',
  review_unavailable: '事实复核服务未能完成',
  review_quota_exhausted: '事实复核模型额度不足',
  review_auth_rejected: '事实复核模型认证失败',
  review_not_accepted: '历史答复未通过复核，未留存具体原因',
  model_failed: '模型生成未完成',
  model_quota_exhausted: '生成模型额度不足',
  model_stream_incomplete: '模型回答传输不完整',
}
const stateLabel = (run: Run) =>
  run.state === 'succeeded' && run.delivery_mode === 'evidence_only'
    ? '仅交付证据（诊断未完成）'
    : labels[run.state] || run.state
const elapsed = (value: number | null) =>
  value === null ? '未采集' : `${(value / 1000).toFixed(2)} 秒`

export function OperatorRunLogs({ appId }: { appId: string }) {
  const [query, setQuery] = useState({ page: 1, run: '', refresh: 0 })
  const [result, setResult] = useState<{ items: Run[]; has_more: boolean } | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    manage(
      query.run ? 'run-log' : 'run-logs',
      query.run
        ? { app_id: appId, run_id: query.run }
        : { app_id: appId, page: query.page, limit: 20 },
    )
      .then((data) => {
        if (active) setResult(query.run ? { items: [data], has_more: false } : data)
      })
      .catch((e) => {
        if (active) setError(e instanceof Error ? e.message : '日志暂时不可读')
      })
    return () => {
      active = false
    }
  }, [appId, query])
  const change = (page: number, run = '') => {
    setResult(null)
    setError('')
    setQuery({ page, run, refresh: query.refresh + 1 })
  }
  return (
    <section aria-label="Java 运行日志" className="flex h-full flex-col gap-4 overflow-auto p-6">
      <h1 className="text-xl font-semibold">Java 运行日志</h1>
      <p>仅显示本人在当前应用、当前权限范围内的运行元数据。耗时截至最后已采集阶段。</p>
      <div className="flex gap-2">
        <Button onClick={() => change(query.page, query.run)}>刷新日志</Button>
        {query.run && <Button onClick={() => change(query.page)}>返回列表</Button>}
      </div>
      {error && <p role="alert">{error}</p>}
      {!result && !error && <p role="status">正在读取日志…</p>}
      {result && !query.run && (
        <>
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead>
                <tr>
                  <th>开始时间（UTC）</th>
                  <th>QA 引用</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>来源</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((run) => (
                  <tr key={run.run_id}>
                    <td>{run.created_at}</td>
                    <td>{run.qa_id || '未关联'}</td>
                    <td>
                      {stateLabel(run)}
                      {run.result_expired && '（结果已过期）'}
                    </td>
                    <td>{elapsed(run.elapsed_ms)}</td>
                    <td>Java</td>
                    <td>
                      <Button onClick={() => change(query.page, run.run_id)}>查看运行</Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!result.items.length && <p>当前没有可见运行记录</p>}
          <div className="flex items-center gap-2">
            <Button disabled={query.page === 1} onClick={() => change(query.page - 1)}>
              上一页
            </Button>
            <span>第 {query.page} 页</span>
            <Button
              disabled={!result.has_more || query.page >= 1000}
              onClick={() => change(query.page + 1)}
            >
              下一页
            </Button>
          </div>
        </>
      )}
      {result &&
        query.run &&
        result.items.map((run) => (
          <div key={run.run_id} className="flex flex-col gap-3">
            <p>
              QA 引用：{run.qa_id || '未关联'} · {stateLabel(run)} · {elapsed(run.elapsed_ms)}
            </p>
            <p>开始时间（UTC）：{run.created_at} · 来源：Java</p>
            {run.review_reason && run.review_reason !== 'allowed' && (
              <p>诊断未完成原因：{labels[run.review_reason] || '未能确认'}</p>
            )}
            <p>
              版本：{run.agent_version || '未核对'} · Gateway：{run.gateway_version} · 节点构建：
              {run.node_build || '未回报'}
            </p>
            {run.result_expired && <p>业务结果已过期，此处仅保留运行元数据。</p>}
            {!run.trace.length ? (
              <p>阶段耗时未采集</p>
            ) : (
              <ol>
                {run.trace.map((event) => (
                  <li
                    key={`${event.at}:${event.phase}:${event.tool}:${event.status}:${event.elapsed_ms}`}
                  >
                    {event.at} · {labels[event.phase] || event.phase} {event.tool} ·{' '}
                    {labels[event.status || ''] || event.status} · 累计 {elapsed(event.elapsed_ms)}
                    {event.phase === 'tool' && (
                      <> · 工具耗时 {elapsed(event.duration_ms ?? null)}</>
                    )}
                  </li>
                ))}
              </ol>
            )}
          </div>
        ))}
    </section>
  )
}

export default function AppLogs() {
  const appId = useAppStore((state) => state.appDetail?.id || '')
  const { status, error } = useManagedApp(appId)
  if (error) return <p role="alert">{error}</p>
  if (!status) return <p role="status">正在核对日志入口…</p>
  if (status.managed && status.entry === 'operator')
    return <OperatorRunLogs key={appId} appId={appId} />
  return <NativeLogs pageType={PageType.log} />
}
