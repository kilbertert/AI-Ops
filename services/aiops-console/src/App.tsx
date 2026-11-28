import EnvironmentManagement from './components/aiops-environments'
import { LiquidGlassDefs } from './components/liquid-glass'

export default function App() {
  return (
    <div className="aiops-ambient-shell flex min-h-screen flex-col">
      <LiquidGlassDefs />
      <header className="aiops-glass-toolbar sticky top-0 z-20 border-x-0 border-t-0">
        <div className="mx-auto flex max-w-[1320px] items-center justify-between gap-4 px-6 py-3">
          <div className="flex items-center gap-3">
            <span className="inline-flex size-7 items-center justify-center rounded-lg bg-state-accent-solid system-xs-semibold text-white shadow-xs">
              A
            </span>
            <span className="system-md-semibold text-text-primary">AI-Ops 管理工作台</span>
            <span
              aria-current="page"
              className="rounded-full bg-state-accent-hover px-3 py-1 system-xs-semibold text-text-accent"
            >
              环境与租户
            </span>
          </div>
          <nav aria-label="控制台导航" className="flex items-center gap-3">
            <a
              href="/datasets"
              className="rounded-full px-3 py-1.5 system-sm-medium text-text-secondary transition-colors hover:bg-components-panel-on-panel-item-bg-hover hover:text-text-primary"
            >
              知识库
            </a>
            <a
              href="/apps"
              className="rounded-full border border-divider-deep bg-components-panel-bg px-3.5 py-1.5 system-sm-medium text-text-primary transition-colors hover:bg-components-panel-on-panel-item-bg-hover"
            >
              返回 Dify 控制台
            </a>
          </nav>
        </div>
      </header>
      <main className="relative z-10 flex-1">
        <EnvironmentManagement />
      </main>
    </div>
  )
}
