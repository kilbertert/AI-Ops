import { render, screen } from '@testing-library/react'
import WorkflowHeader from '@/app/components/workflow-app/components/workflow-header'

const managed = vi.hoisted(() => ({ value: true }))
vi.mock('@/app/components/app/store', () => ({
  useStore: (selector: (s: unknown) => unknown) =>
    selector({
      appDetail: { id: 'app-c' },
      setCurrentLogItem: () => {},
      setShowMessageLogModal: () => {},
    }),
}))
vi.mock('@/service/aiops-management', () => ({
  useManagedApp: () => ({ status: { managed: managed.value } }),
}))
vi.mock('@/service/use-workflow', () => ({ useResetWorkflowVersionHistory: () => () => {} }))
vi.mock('@/app/components/workflow-app/hooks/use-is-chat-mode', () => ({
  useIsChatMode: () => true,
}))
vi.mock('@/app/components/workflow-app/components/workflow-header/chat-variable-trigger', () => ({
  default: () => null,
}))
vi.mock('@/app/components/workflow-app/components/workflow-header/features-trigger', () => ({
  default: () => null,
}))
vi.mock('@/app/components/workflow/header', () => ({
  default: ({ normal }: { normal: { runAndHistoryProps: { showPreviewButton: boolean } } }) => (
    <div>{normal.runAndHistoryProps.showPreviewButton ? 'native-preview' : 'managed-preview'}</div>
  ),
}))

it('keeps native preview for unmanaged apps and uses managed preview for customer apps', () => {
  const result = render(<WorkflowHeader />)
  expect(screen.getByText('managed-preview')).toBeInTheDocument()
  managed.value = false
  result.unmount()
  render(<WorkflowHeader />)
  expect(screen.getByText('native-preview')).toBeInTheDocument()
})
