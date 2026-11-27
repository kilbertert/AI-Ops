'use client'

import { Button } from '@langgenius/dify-ui/button'
import { useEffect, useState } from 'react'
import { manage } from '@/service/aiops-management'

type IngressDiff = {
  changed: boolean
  added_routes: string[]
  removed_routes: string[]
}

type ReleaseCatalogEntry = {
  tag: string
  version: string
  revision: string
  short_sha: string
  full_version: string
  released_at: string
  summary: string
  ingress_routes: string[]
  ingress_diff: IngressDiff
  pool_python_minors: string[]
}

type RollbackTarget = {
  version: string
  revision: string
  release_name: string
  recorded_at: number | null
  has_nginx_backup: boolean
}

type ReleaseJobStep = {
  step: string
  state: 'pending' | 'running' | 'passed' | 'failed' | 'skipped'
  reason?: string
}

type ReleaseJob = {
  job_id?: string
  environment?: string
  action?: string
  state?: 'queued' | 'running' | 'completed' | 'failed'
  passed?: boolean
  failed_step?: string
  error_code?: string
  steps?: ReleaseJobStep[]
  from_version?: string
  to_version?: string
  target_tag?: string
  nginx_changed?: boolean
  untouched_config_verified?: boolean
  rolled_back_automatically?: boolean
  smoke_passed?: boolean
  current_version?: string
  business_mutations?: string
  updated_at?: number
  result?: Record<string, unknown>
}

type ReleaseOverview = {
  schema: string
  environment: string
  is_control: boolean
  control_version: string
  enrolled: boolean
  gateway_url: string
  current_version: string
  current_revision: string
  python_minor: string
  business_mutations: string
  health_ok: boolean | null
  last_probed_at: number | null
  current_ingress_routes: string[]
  rollback_target: RollbackTarget | null
  catalog_updated_at: number | null
  available_releases: ReleaseCatalogEntry[]
  latest_release: ReleaseCatalogEntry | null
  update_available: boolean
  history: ReleaseJob[]
}

const STEP_TITLES: Record<string, string> = {
  probe_target: '探测线上健康与入口',
  resolve_artifact: '校验官方标签与离线制品',
  install_release: '隔离目录离线安装 venv',
  sync_nginx_ingress: '对比同步受控 Nginx 入口并 nginx -t',
  switch_and_verify: '原子切换软链、重启核验并检测配置零改动',
  public_smoke: '公网 HTTPS 401/200 冒烟验证',
  verify_previous_release: '核对上一版本目录与备份完整性',
}

const ACTION_TITLES: Record<string, string> = {
  release_probe: '实时版本探测',
  environment_upgrade: '无损滚动升级',
  environment_rollback: '一键安全回滚',
}

function formatEpoch(sec: number | null | undefined): string {
  if (!sec || !Number.isFinite(sec)) return '—'
  try {
    return new Date(sec * 1000).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return String(sec)
  }
}

export default function EnvironmentReleasesPanel({
  environment,
  environmentName,
  isControl,
  onOpenWizard,
}: {
  environment: string
  environmentName: string
  isControl: boolean
  onOpenWizard?: () => void
}) {
  const [overview, setOverview] = useState<ReleaseOverview | null>(null)
  const [selectedTag, setSelectedTag] = useState('')
  const [activeJob, setActiveJob] = useState<ReleaseJob | null>(null)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [confirmUpgrade, setConfirmUpgrade] = useState(false)
  const [confirmRollback, setConfirmRollback] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  async function loadOverview(envId: string) {
    if (!envId) return
    setLoading(true)
    setError('')
    try {
      const res = (await manage('environment-wizard', {
        wizard_action: 'release_overview',
        wizard_payload: { environment: envId },
      })) as ReleaseOverview
      setOverview(res)
      setSelectedTag((prev) => {
        if (prev && res.available_releases?.some((item) => item.tag === prev)) return prev
        return res.latest_release?.tag || res.available_releases?.[0]?.tag || ''
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取版本与发版总览失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    setConfirmUpgrade(false)
    setConfirmRollback(false)
    setActiveJob(null)
    setNotice('')
    setError('')
    void loadOverview(environment)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [environment])

  async function pollReleaseJob(
    jobAction: 'release_probe' | 'environment_upgrade' | 'environment_rollback',
    initial: ReleaseJob,
  ): Promise<ReleaseJob | null> {
    if (initial?.job_id || (Array.isArray(initial?.steps) && initial.steps.length > 0)) {
      setActiveJob(initial)
    }
    if (!initial?.job_id || initial.state === 'completed' || initial.state === 'failed') {
      return initial
    }
    let latest = initial
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 2000))
      const polled = (await manage('environment-wizard', {
        wizard_action: 'infra_status',
        wizard_payload: {
          environment,
          action: jobAction,
          job_id: initial.job_id,
        },
      })) as ReleaseJob
      latest = polled
      setActiveJob(polled)
      if (polled.state === 'completed' || polled.state === 'failed') {
        return polled
      }
    }
    return latest
  }

  async function handleProbeLive() {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const initial = (await manage('environment-wizard', {
        wizard_action: 'release_probe',
        wizard_payload: { environment },
      })) as ReleaseJob
      const done = await pollReleaseJob('release_probe', initial)
      await loadOverview(environment)
      if (done?.passed) {
        setNotice(`已同步 ${environmentName || environment} 最新运行版本与 Nginx 入口状态`)
      } else {
        setError(`实时版本探测失败：${done?.error_code || '请检查目标主机连通性'}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '发起实时版本探测失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleUpgrade() {
    if (!selectedTag) {
      setError('请先选择目标官方正式版本标签')
      return
    }
    setConfirmUpgrade(false)
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const initial = (await manage('environment-wizard', {
        wizard_action: 'environment_upgrade',
        wizard_payload: { environment, target_tag: selectedTag },
      })) as ReleaseJob
      const done = await pollReleaseJob('environment_upgrade', initial)
      await loadOverview(environment)
      if (done?.passed) {
        setNotice(
          `升级完成：${done.to_version || selectedTag} 已就绪且公网冒烟通过（客户配置零触碰核验通过）`,
        )
      } else {
        const rbTip = done?.rolled_back_automatically ? '；系统已自动切回原版本兜底' : ''
        setError(`升级阻断（${done?.failed_step || done?.error_code || '未知错误'}）${rbTip}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '提交无损滚动升级失败')
    } finally {
      setBusy(false)
    }
  }

  async function handleRollback() {
    setConfirmRollback(false)
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const initial = (await manage('environment-wizard', {
        wizard_action: 'environment_rollback',
        wizard_payload: { environment, confirm_rollback: true },
      })) as ReleaseJob
      const done = await pollReleaseJob('environment_rollback', initial)
      await loadOverview(environment)
      if (done?.passed) {
        setNotice(`已安全回滚至上一版本 ${done.to_version || ''} 并完成 /health 与公网冒烟校验`)
      } else {
        setError(`回滚失败：${done?.failed_step || done?.error_code || '请检查主机状态'}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : '发起一键回滚失败')
    } finally {
      setBusy(false)
    }
  }

  const releases = overview?.available_releases || []
  const selectedRelease = releases.find((item) => item.tag === selectedTag) || releases[0] || null
  const envIsControl = Boolean(isControl || overview?.is_control)
  const sameAsCurrent = Boolean(
    selectedRelease &&
    overview?.current_version &&
    selectedRelease.full_version === overview.current_version,
  )

  return (
    <div
      id="aiops-panel-releases"
      role="tabpanel"
      aria-labelledby="aiops-tab-releases"
      className="space-y-5"
    >
      {error && (
        <div
          role="alert"
          className="flex items-center gap-2.5 rounded-xl border border-state-destructive-border bg-state-destructive-hover px-4 py-3 system-xs-medium text-text-destructive"
        >
          <span aria-hidden="true" className="i-ri-error-warning-fill size-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div
          role="status"
          className="flex items-center gap-2.5 rounded-xl border border-state-success-active/60 bg-state-success-hover px-4 py-3 system-xs-medium text-text-success"
        >
          <span aria-hidden="true" className="i-ri-checkbox-circle-fill size-4 shrink-0" />
          <span>{notice}</span>
        </div>
      )}

      {/* 当前运行版本英雄卡 */}
      <section className="overflow-hidden rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-divider-subtle bg-background-section-burn/40 px-6 py-4">
          <div className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-blue-soft text-text-accent"
            >
              <span className="i-ri-git-branch-line size-4" />
            </span>
            <div className="space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <h3 className="system-md-semibold text-text-primary">
                  当前运行版本 · {environmentName || environment}（{environment}）
                </h3>
                <span className="inline-flex items-center gap-1 rounded-full border border-divider-accent/40 bg-components-badge-bg-blue-light-soft px-2.5 py-0.5 text-[11px] font-medium text-text-accent">
                  <span aria-hidden="true" className="i-ri-shield-check-line size-3" />
                  <span>business_mutations: {overview?.business_mutations || 'disabled'}</span>
                </span>
                {overview?.update_available && !envIsControl && (
                  <span className="inline-flex items-center gap-1 rounded-full border border-state-warning-active/40 bg-components-badge-bg-orange-soft px-2.5 py-0.5 text-[11px] font-medium text-text-warning">
                    <span aria-hidden="true" className="i-ri-arrow-up-circle-line size-3" />
                    <span>有新正式版可升级</span>
                  </span>
                )}
              </div>
              <p className="text-[11px] text-text-tertiary tabular-nums">
                最近探测：{formatEpoch(overview?.last_probed_at)} · 官方版本目录更新：
                {formatEpoch(overview?.catalog_updated_at)}
              </p>
            </div>
          </div>
          <Button
            type="button"
            variant="secondary"
            size="small"
            disabled={busy || loading}
            onClick={() => void handleProbeLive()}
          >
            <span
              aria-hidden="true"
              className={`mr-1 i-ri-refresh-line size-3.5 ${busy || loading ? 'animate-spin' : ''}`}
            />
            {busy ? '处理中…' : '刷新实时版本'}
          </Button>
        </div>

        <div className="grid gap-3 p-6 sm:grid-cols-3">
          <div className="rounded-xl border border-divider-subtle bg-background-section-burn/55 px-4 py-3">
            <div className="text-[11px] text-text-tertiary">线上当前版本</div>
            <div className="mt-1 font-mono system-sm-semibold break-all text-text-primary">
              {overview?.current_version || (loading ? '加载中…' : '尚未探测（请点击刷新）')}
            </div>
          </div>
          <div className="rounded-xl border border-divider-subtle bg-background-section-burn/55 px-4 py-3">
            <div className="text-[11px] text-text-tertiary">运行时与健康状态</div>
            <div className="mt-1 flex items-center gap-2 system-sm-medium text-text-primary">
              <span
                aria-hidden="true"
                className={`size-2 rounded-full ${
                  overview?.health_ok === true
                    ? 'bg-state-success-solid'
                    : overview?.health_ok === false
                      ? 'bg-state-destructive-solid'
                      : 'bg-text-quaternary'
                }`}
              />
              <span>
                {overview?.python_minor ? `Python ${overview.python_minor}` : '待探测'}
                {overview?.health_ok === true
                  ? ' · 健康正常'
                  : overview?.health_ok === false
                    ? ' · 健康异常'
                    : ''}
              </span>
            </div>
          </div>
          <div className="rounded-xl border border-divider-subtle bg-background-section-burn/55 px-4 py-3">
            <div className="text-[11px] text-text-tertiary">公网 HTTPS 入口</div>
            <div className="mt-1 truncate font-mono text-xs text-text-secondary">
              {overview?.gateway_url || '—'}
            </div>
          </div>
        </div>
      </section>

      {/* 控制面 41 或未引导主机说明 */}
      {envIsControl ? (
        <section className="flex items-start gap-3.5 rounded-2xl border border-divider-accent/40 bg-components-badge-bg-blue-light-soft/40 p-5">
          <span
            aria-hidden="true"
            className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-blue-soft text-text-accent"
          >
            <span className="i-ri-shield-keyhole-line size-5" />
          </span>
          <div>
            <h4 className="system-sm-semibold text-text-primary">控制面 41 发布说明</h4>
            <p className="mt-1 system-xs-regular text-text-secondary">
              41 为主控制面节点，按平台开发共识仅经受保护 GitHub CD
              工作流发布与回滚，不在当前管理页自升级；此处仅展示控制面版本与官方已发版标签目录。
            </p>
          </div>
        </section>
      ) : !overview?.enrolled ? (
        <section className="flex flex-wrap items-center justify-between gap-4 rounded-2xl border border-dashed border-divider-regular bg-components-panel-bg p-5 shadow-xs">
          <div className="flex items-start gap-3">
            <span
              aria-hidden="true"
              className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-orange-soft text-text-warning"
            >
              <span className="i-ri-rocket-line size-5" />
            </span>
            <div className="space-y-1">
              <h4 className="system-sm-semibold text-text-primary">该客户主机尚未完成自助接入</h4>
              <p className="system-xs-regular text-text-secondary">
                Day-2 无损升级与回滚复用主机钉扎指纹 SSH 通道；请先在「5
                步自助开通向导」完成主机引导与冷装。
              </p>
            </div>
          </div>
          {onOpenWizard && (
            <Button type="button" variant="primary" size="small" onClick={onOpenWizard}>
              打开 5 步自助开通向导
            </Button>
          )}
        </section>
      ) : (
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
          {/* 无损滚动升级面板 */}
          <section className="flex flex-col justify-between gap-4 rounded-2xl border border-components-panel-border bg-components-panel-bg p-6 shadow-xs">
            <div className="space-y-4">
              <div className="flex items-start gap-3">
                <span
                  aria-hidden="true"
                  className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-blue-soft text-text-accent"
                >
                  <span className="i-ri-arrow-up-circle-line size-5" />
                </span>
                <div className="space-y-1">
                  <h4 className="system-md-semibold text-text-primary">无损原地滚动升级</h4>
                  <p className="system-xs-regular text-text-tertiary">
                    仅可选 <code className="font-mono">main</code> 上带不可变注释标签（
                    <code className="font-mono">vX.Y.Z</code>
                    ）的官方正式版；自动匹配离线包、同步受控 Nginx 入口并校验配置零触碰。
                  </p>
                </div>
              </div>

              <div className="space-y-3">
                <label className="block space-y-1.5">
                  <span className="system-xs-medium text-text-secondary">目标官方正式版本</span>
                  <div className="relative">
                    <select
                      aria-label="目标官方正式版本"
                      className="h-10 w-full appearance-none rounded-xl border border-divider-regular bg-components-input-bg-normal pr-9 pl-3 text-xs text-text-primary focus:border-state-accent-solid focus:outline-hidden"
                      disabled={busy || releases.length === 0}
                      value={selectedTag}
                      onChange={(e) => {
                        setSelectedTag(e.target.value)
                        setConfirmUpgrade(false)
                      }}
                    >
                      {releases.length === 0 && <option value="">等待同步官方标签目录…</option>}
                      {releases.map((item) => (
                        <option key={item.tag} value={item.tag}>
                          {item.tag}（{item.full_version}）— {item.summary || '正式发布版'}
                        </option>
                      ))}
                    </select>
                    <span
                      aria-hidden="true"
                      className="pointer-events-none absolute top-1/2 right-3 i-ri-arrow-down-s-line size-4 -translate-y-1/2 text-text-tertiary"
                    />
                  </div>
                </label>

                {selectedRelease && (
                  <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/55 p-4 text-xs">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <span className="font-medium text-text-primary">
                        Nginx 用户入口变更检测（自动同步 · nginx -t 兜底）
                      </span>
                      <span
                        className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                          selectedRelease.ingress_diff.changed
                            ? 'bg-components-badge-bg-orange-soft text-text-warning'
                            : 'bg-components-badge-bg-green-soft text-text-success'
                        }`}
                      >
                        {selectedRelease.ingress_diff.changed
                          ? '含受控 Nginx 入口更新'
                          : 'Nginx 入口无变化'}
                      </span>
                    </div>

                    {selectedRelease.ingress_diff.added_routes.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-text-secondary">
                        <span>新增放行路由：</span>
                        {selectedRelease.ingress_diff.added_routes.map((r) => (
                          <code
                            key={r}
                            className="rounded-md bg-components-badge-bg-green-soft px-2 py-0.5 font-mono text-text-success"
                          >
                            {r}
                          </code>
                        ))}
                      </div>
                    )}

                    {selectedRelease.ingress_diff.removed_routes.length > 0 && (
                      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-text-secondary">
                        <span>移除路由：</span>
                        {selectedRelease.ingress_diff.removed_routes.map((r) => (
                          <code
                            key={r}
                            className="rounded-md bg-components-badge-bg-red-soft px-2 py-0.5 font-mono text-text-destructive"
                          >
                            {r}
                          </code>
                        ))}
                      </div>
                    )}

                    <div className="border-t border-divider-subtle pt-2.5 text-[11px] text-text-tertiary">
                      <div className="mb-1.5 flex items-center gap-1 font-medium text-text-secondary">
                        <span
                          aria-hidden="true"
                          className="i-ri-lock-line size-3 text-text-success"
                        />
                        <span>零触碰保护（升级前后 SHA-256 核对）：</span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {[
                          'production.env',
                          'registry/environment.json',
                          'runtime/',
                          'gateway.db',
                          'nginx-user-ingress-auth.conf',
                        ].map((file) => (
                          <code
                            key={file}
                            className="rounded bg-components-panel-bg px-1.5 py-0.5 font-mono text-[10px] text-text-secondary"
                          >
                            {file}
                          </code>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>

            {!confirmUpgrade ? (
              <div className="flex flex-wrap items-center gap-2 pt-1">
                <Button
                  type="button"
                  variant="primary"
                  disabled={busy || !selectedRelease || sameAsCurrent}
                  onClick={() => setConfirmUpgrade(true)}
                >
                  <span aria-hidden="true" className="mr-1.5 i-ri-rocket-2-line size-4" />
                  {sameAsCurrent ? '当前已是该版本' : '一键无损滚动升级'}
                </Button>
              </div>
            ) : (
              <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-state-accent-solid/45 bg-components-badge-bg-blue-light-soft p-3.5 text-xs">
                <span className="text-text-primary">
                  确认将 <strong>{environment}</strong> 无损升级至{' '}
                  <strong>{selectedRelease?.full_version}</strong>
                  ？（不动客户配置与数据，失败自动回退）
                </span>
                <div className="flex gap-2">
                  <Button
                    type="button"
                    variant="secondary"
                    size="small"
                    onClick={() => setConfirmUpgrade(false)}
                  >
                    取消
                  </Button>
                  <Button
                    type="button"
                    variant="primary"
                    size="small"
                    onClick={() => void handleUpgrade()}
                  >
                    确认执行升级
                  </Button>
                </div>
              </div>
            )}
          </section>

          {/* 一键安全回滚面板 */}
          <section className="flex flex-col justify-between gap-4 rounded-2xl border border-components-panel-border bg-components-panel-bg p-6 shadow-xs">
            <div className="space-y-4">
              <div className="flex items-start gap-3">
                <span
                  aria-hidden="true"
                  className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-components-icon-bg-orange-soft text-text-warning"
                >
                  <span className="i-ri-arrow-go-back-line size-5" />
                </span>
                <div className="space-y-1">
                  <h4 className="system-md-semibold text-text-primary">一键安全回滚</h4>
                  <p className="system-xs-regular text-text-tertiary">
                    切换 <code className="font-mono">current</code>{' '}
                    软链回退至上一隔离发布目录并还原对应 Nginx 备份，不影响业务库与租户路由。
                  </p>
                </div>
              </div>

              {overview?.rollback_target ? (
                <div className="space-y-3 rounded-xl border border-divider-subtle bg-background-section-burn/55 p-4 text-xs">
                  <div>
                    <div className="text-[11px] text-text-tertiary">可回滚目标版本</div>
                    <strong className="mt-0.5 block font-mono text-sm text-text-primary">
                      {overview.rollback_target.version}
                    </strong>
                  </div>
                  <div className="text-[11px] text-text-tertiary">
                    记录时间：{formatEpoch(overview.rollback_target.recorded_at)} · Nginx 备份：
                    {overview.rollback_target.has_nginx_backup
                      ? '含备份可联动恢复'
                      : '无需回退 Nginx'}
                  </div>
                </div>
              ) : (
                <div className="rounded-xl border border-dashed border-divider-regular bg-background-section-burn/40 p-4 text-xs text-text-tertiary">
                  暂无可回滚的上一发布版本（仅在通过本页面完成至少一次升级后保留上一隔离版本目录）。
                </div>
              )}
            </div>

            {overview?.rollback_target &&
              (!confirmRollback ? (
                <div>
                  <Button
                    type="button"
                    variant="secondary"
                    size="small"
                    disabled={busy}
                    onClick={() => setConfirmRollback(true)}
                  >
                    <span aria-hidden="true" className="mr-1 i-ri-arrow-go-back-line size-3.5" />
                    一键回滚至上一版本
                  </Button>
                </div>
              ) : (
                <div className="space-y-2.5 rounded-xl border border-state-warning-active/45 bg-components-badge-bg-orange-soft p-3.5 text-xs">
                  <div className="font-medium text-text-primary">
                    确认立即回滚至 <strong>{overview.rollback_target.version}</strong> 并重启验证？
                  </div>
                  <div className="flex gap-2">
                    <Button
                      type="button"
                      variant="secondary"
                      size="small"
                      onClick={() => setConfirmRollback(false)}
                    >
                      取消
                    </Button>
                    <Button
                      type="button"
                      variant="primary"
                      size="small"
                      onClick={() => void handleRollback()}
                    >
                      确认执行回滚
                    </Button>
                  </div>
                </div>
              ))}
          </section>
        </div>
      )}

      {/* 实时流水线与回执 */}
      {activeJob && (
        <section className="space-y-4 rounded-2xl border border-divider-accent/50 bg-components-panel-bg p-6 shadow-xs">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-divider-subtle pb-3">
            <div className="flex items-center gap-2">
              <span
                aria-hidden="true"
                className={`size-4 ${
                  activeJob.state === 'completed' && activeJob.passed
                    ? 'i-ri-checkbox-circle-fill text-text-success'
                    : activeJob.state === 'failed'
                      ? 'i-ri-close-circle-fill text-text-destructive'
                      : 'i-ri-loader-2-line animate-spin text-text-accent'
                }`}
              />
              <h4 className="system-sm-semibold text-text-primary">
                {ACTION_TITLES[activeJob.action || ''] || activeJob.action}进度 ·{' '}
                {activeJob.job_id || '实时'}
              </h4>
            </div>
            <span className="rounded-full bg-background-section-burn px-2.5 py-0.5 text-xs font-medium text-text-secondary">
              状态：{activeJob.state || (activeJob.passed ? 'completed' : 'running')}
            </span>
          </div>
          {Array.isArray(activeJob.steps) && activeJob.steps.length > 0 && (
            <div className="space-y-2">
              {activeJob.steps.map((s) => (
                <div
                  key={s.step}
                  className={`flex items-center justify-between rounded-xl border px-3.5 py-2 text-xs ${
                    s.state === 'passed'
                      ? 'border-state-success-active/35 bg-components-badge-bg-green-soft/50'
                      : s.state === 'failed'
                        ? 'border-state-destructive-border bg-components-badge-bg-red-soft/60'
                        : s.state === 'running'
                          ? 'border-divider-accent bg-components-badge-bg-blue-light-soft/60'
                          : 'border-divider-subtle bg-background-section-burn/50'
                  }`}
                >
                  <span className="flex items-center gap-2 text-text-primary">
                    <span
                      aria-hidden="true"
                      className={`size-3.5 ${
                        s.state === 'passed'
                          ? 'i-ri-checkbox-circle-fill text-text-success'
                          : s.state === 'failed'
                            ? 'i-ri-close-circle-fill text-text-destructive'
                            : s.state === 'running'
                              ? 'i-ri-loader-2-line animate-spin text-text-accent'
                              : 'i-ri-time-line text-text-tertiary'
                      }`}
                    />
                    <span>{STEP_TITLES[s.step] || s.step}</span>
                  </span>
                  <span
                    className={`font-medium ${
                      s.state === 'passed'
                        ? 'text-text-success'
                        : s.state === 'failed'
                          ? 'text-text-destructive'
                          : 'text-text-tertiary'
                    }`}
                  >
                    {s.state}
                    {s.reason ? ` (${s.reason})` : ''}
                  </span>
                </div>
              ))}
            </div>
          )}
          {activeJob.state === 'completed' && activeJob.passed && (
            <div className="space-y-2 rounded-xl border border-state-success-active/40 bg-components-badge-bg-green-soft/45 p-4 text-xs text-text-secondary">
              {activeJob.from_version && activeJob.to_version && (
                <div className="font-medium text-text-primary">
                  版本变更：<code className="font-mono">{activeJob.from_version}</code> →{' '}
                  <code className="font-mono">{activeJob.to_version}</code>
                </div>
              )}
              {typeof activeJob.untouched_config_verified === 'boolean' && (
                <div>
                  配置与数据零触碰核验：
                  {activeJob.untouched_config_verified ? '通过（SHA-256 一致）' : '未确认'} · 受控
                  Nginx 更新：{activeJob.nginx_changed ? '已同步重载' : '无变更'} · 公网冒烟：
                  {activeJob.smoke_passed ? '通过' : '未确认'}
                </div>
              )}
              <div className="text-text-tertiary">
                诚实边界：业务语义准确率当前为 <strong>unconfirmed</strong>
                （自动化只证明进程健康、只读契约与公网 401/200 路由，不替代真实故障签收）。
              </div>
            </div>
          )}
        </section>
      )}

      {/* 最近发版与回滚审计历史 */}
      <section className="overflow-hidden rounded-2xl border border-components-panel-border bg-components-panel-bg shadow-xs">
        <div className="flex items-center gap-2 border-b border-divider-subtle px-6 py-4">
          <span
            aria-hidden="true"
            className="flex size-7 items-center justify-center rounded-lg bg-components-icon-bg-indigo-soft text-text-accent"
          >
            <span className="i-ri-history-line size-4" />
          </span>
          <h4 className="system-sm-semibold text-text-primary">发版、探测与回滚记录</h4>
        </div>
        {(overview?.history || []).length === 0 ? (
          <p className="px-6 py-6 text-center text-xs text-text-tertiary">暂无记录。</p>
        ) : (
          <div className="divide-y divide-divider-subtle">
            {(overview?.history || []).map((item) => (
              <div
                key={item.job_id || `${item.action}-${item.updated_at}`}
                className="flex flex-wrap items-center justify-between gap-2 px-6 py-3 text-xs transition-colors hover:bg-background-section-burn/40"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    aria-hidden="true"
                    className={`size-3.5 ${
                      item.passed
                        ? 'i-ri-checkbox-circle-fill text-text-success'
                        : item.state === 'failed'
                          ? 'i-ri-close-circle-fill text-text-destructive'
                          : 'i-ri-time-line text-text-tertiary'
                    }`}
                  />
                  <span className="font-medium text-text-primary">
                    {ACTION_TITLES[item.action || ''] || item.action}
                  </span>
                  {item.target_tag && (
                    <span className="rounded bg-background-section-burn px-1.5 py-0.5 font-mono text-[11px] text-text-secondary">
                      {item.target_tag}
                    </span>
                  )}
                  {item.from_version && item.to_version && (
                    <span className="font-mono text-[11px] text-text-secondary">
                      ({item.from_version} → {item.to_version})
                    </span>
                  )}
                </div>
                <div className="flex items-center gap-3 text-text-tertiary tabular-nums">
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      item.passed
                        ? 'bg-components-badge-bg-green-soft text-text-success'
                        : item.state === 'failed'
                          ? 'bg-components-badge-bg-red-soft text-text-destructive'
                          : 'bg-components-badge-bg-gray-soft text-text-tertiary'
                    }`}
                  >
                    {item.passed
                      ? '通过'
                      : item.state === 'failed'
                        ? `失败(${item.error_code})`
                        : item.state}
                  </span>
                  <span>{formatEpoch(item.updated_at)}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}
