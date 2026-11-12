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
  detail?: string
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
  reason?: string | null
  question?: string | null
  order_no?: string | null
  language?: string | null
  environment?: string | null
  expected_node_id?: string | null
  actual_node_id?: string | null
  agent_version_observed?: string | null
  agent_version_gap?: string | null
  claimed_at?: string | null
  completed_at?: string | null
  expires_at?: string | null
  result_sha256?: string | null
  trial?: boolean
  history_run_count?: number
  evidence_tools?: string[]
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
  cancelled: '已取消',
  queued: '排队中',
  generating: '生成中',
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
  // 终态与拒绝原因码（账本存的是码不是原文）
  platform_unavailable: '运行平台不可用',
  authorization_unavailable: '业务授权服务不可用',
  order_not_visible: '订单不在当前权限范围',
  node_deactivated: '执行节点被停用',
  binding_changed: '节点绑定已变化',
  scope_unreadable: '权限范围读取失败',
  exec_config_drift: '节点执行配置漂移',
  unexpected_error: '未预期错误',
  startup_failed: '运行启动失败',
  binding_unavailable: '会话绑定不可用',
  consumer_release_mismatch: '发布版本不一致',
  snapshot_invalid: '运行快照无效',
  revoked_by_caller: '调用方撤销',
  trial_failed: '试用执行失败',
  dispatch_refused: '派发被节点拒绝',
  no_answer_accepted: '答复未通过放行复核',
  OPERATOR_ORDER_REQUIRED: '缺少订单号',
  OPERATOR_ORDER_NOT_REFERENCED: '订单未被用户提及或选定',
  OPERATOR_ORDER_CONFLICT: '单轮只能查询一笔订单',
  OPERATOR_TOOL_ARGUMENTS_INVALID: '工具参数未通过校验',
  OPERATOR_TOOL_ARGUMENTS_CHANGED: '同一工具参数被改变',
  OPERATOR_ANALYSIS_INVALID: '该工具不接受参数',
  OPERATOR_DETAIL_REQUIRED: '需先读取订单详情',
  OPERATOR_TARIFF_REQUIRED: '需先读取计费模板',
  OPERATOR_EVIDENCE_MISSING: '缺少依赖的订单证据',
  OPERATOR_SITE_SCOPE_REQUIRED: '当前账号没有可见站点',
  OPERATOR_OFFLINE_UNAVAILABLE: '离线资料暂不可用或范围过大',
  OPERATOR_SCOPE_CHANGED: '权限范围已变化',
  OPERATOR_PATH_REJECTED: '工具请求路径被拒绝',
  OPERATOR_GRANT_REJECTED: '运行授权校验失败',
  OPERATOR_RUN_INACTIVE: '运行已不处于活动状态',
  OPERATOR_TOOL_DISABLED: '工具被禁用',
  OPERATOR_DEPENDENCY_UNAVAILABLE: '依赖的节点能力不可用',
  OPERATOR_ANSWER_NOT_GROUNDED: '答复未能落到证据上',
  OPERATOR_MODEL_OUTPUT_UNSTABLE: '模型输出不稳定',
  OPERATOR_NODE_REJECTED: '节点拒绝了执行',
  OPERATOR_NODE_CHANGED: '执行节点已变化',
  OPERATOR_TARGET_MISSING: '执行目标缺失',
  OPERATOR_DIAGNOSIS_FAILED: '诊断未完成',
  OPERATOR_HISTORY_LIMIT: '历史上下文超限',
  OPERATOR_TRIAL_LOGIN_REQUIRED: '缺少试用登录凭据',
  CPMS_INVALID_RESPONSE: '上游返回数据无法识别',
  CPMS_SCOPE_MISMATCH: '订单不在可见范围或不存在',
  ttl_elapsed: '运行到期未推进',
  cancelled_before_dispatch: '派发前被取消',
  cancelled_by_caller: '调用方取消',
  node_trial_failed: '节点试用执行失败',
  operator_schedule_failed: '运行调度失败',
  qa_answer_mismatch: '答复与问题不匹配',
  qa_binding_failed: '问题绑定失败',
  retracted_by_operator: '被运维撤回',
  upstream_failed: '上游服务失败',
  operator_node_reported_failure: '节点上报执行失败',
  operator_node_failed: '节点执行失败',
  not_wired: '未启用执行清单',
  not_confirmed: '节点回报版本未核对一致',
}
const label = (code?: string | null) => {
  if (!code) return '未记录'
  // 组合码（operator_node_failed: tool_refused:x）先整串再退前缀查表。
  return labels[code] || labels[code.split(': ')[0]] || code
}
const stateLabel = (run: Run) =>
  run.state === 'succeeded' && run.delivery_mode === 'evidence_only'
    ? '仅交付证据（诊断未完成）'
    : labels[run.state] || run.state
const elapsed = (value: number | null) =>
  value === null ? '未采集' : `${(value / 1000).toFixed(2)} 秒`
const brief = (text: string | null | undefined, limit = 24) =>
  text ? (text.length > limit ? `${text.slice(0, limit)}…` : text) : '—'
const stamp = (name: string, value: string | null | undefined) =>
  value ? (
    <p>
      {name}：{value}
    </p>
  ) : null

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
      <p>仅显示本人在当前应用、当前权限范围内的运行记录。耗时截至最后已采集阶段。</p>
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
                  <th>问题</th>
                  <th>订单号</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>详情</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((run) => (
                  <tr key={run.run_id}>
                    <td>{run.created_at}</td>
                    <td title={run.question || ''}>{brief(run.question)}</td>
                    <td>{run.order_no || '未绑定'}</td>
                    <td>
                      {stateLabel(run)}
                      {run.result_expired && '（结果已过期）'}
                    </td>
                    <td>{elapsed(run.elapsed_ms)}</td>
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
        result.items.map((run) => {
          const resamples = run.trace.filter((e) => e.phase === 'resample').length
          return (
            <div key={run.run_id} className="flex flex-col gap-3">
              <h2 className="text-base font-semibold">运行详情</h2>
              <p>
                问题：{run.question || '未关联'}
              </p>
              <p>
                QA 引用：{run.qa_id || '未关联'} · 订单号：{run.order_no || '未绑定'} ·{' '}
                {stateLabel(run)} · {elapsed(run.elapsed_ms)}
              </p>
              {run.reason && <p>终态原因：{label(run.reason)}（{run.reason}）</p>}
              {run.review_reason && run.review_reason !== 'allowed' && (
                <p>诊断未完成原因：{label(run.review_reason)}</p>
              )}
              {run.result_expired && <p>业务结果已过期，此处仅保留运行记录。</p>}
              <p>
                执行节点：{run.expected_node_id || '未记录'} → 实际{' '}
                {run.actual_node_id || '未回报'}
                {run.expected_node_id &&
                  run.actual_node_id &&
                  run.expected_node_id !== run.actual_node_id &&
                  '（不一致）'}
                {` · 环境：${run.environment || '未记录'} · 语言：${run.language || '未记录'}`}
                {run.trial && ' · 试用'}
              </p>
              <p>
                版本：{run.agent_version || '未核对'} · 节点回报：
                {run.agent_version_observed || '未回报'} · 核对：
                {run.agent_version ? '一致' : label(run.agent_version_gap)} · Gateway：
                {run.gateway_version} · 节点构建：{run.node_build || '未回报'}
              </p>
              <div>
                {stamp('开始时间（UTC）', run.created_at)}
                {stamp('领取时间（UTC）', run.claimed_at)}
                {stamp('完成时间（UTC）', run.completed_at)}
                {stamp('过期时间（UTC）', run.expires_at)}
              </div>
              <p>
                结果指纹：{run.result_sha256 ? run.result_sha256.slice(0, 16) : '无'} · 历史上下文：
                {run.history_run_count ?? 0} 轮 · 重新生成：{resamples} 次
              </p>
              <p>
                证据工具：
                {run.evidence_tools?.length ? run.evidence_tools.join('、') : '无'}
              </p>
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
                      {event.phase === 'tool' && <> · 工具耗时 {elapsed(event.duration_ms ?? null)}</>}
                      {event.detail && <> · {label(event.detail)}</>}
                    </li>
                  ))}
                </ol>
              )}
            </div>
          )
        })}
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
