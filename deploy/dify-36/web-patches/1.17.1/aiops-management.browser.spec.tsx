import { afterEach, expect, it, vi } from 'vitest'
import { ManagedPublish } from '@/app/components/aiops-management'
import { publishManaged } from '@/service/aiops-management'
import { render } from './__tests__/render'

const status = {
  published_prompt: 'SOP',
  published_tools: [],
  app_id: 'app',
  managed: true,
  platform: true,
  model: 'actual-java-model',
  profile: '标准',
  model_options: { max_tokens: 4096 },
  version_no: 2,
  route_revision: 3,
  published: true,
}

afterEach(() => vi.restoreAllMocks())

it('shows save failure and preserves the displayed formal version', async () => {
  const screen = await render(
    <ManagedPublish
      status={status}
      save={async () => {
        throw new Error('保存失败')
      }}
    />,
  )
  await expect.element(screen.getByText('actual-java-model', { exact: false })).toBeVisible()
  await screen.getByRole('button', { name: '发布到管家' }).click()
  await expect.element(screen.getByRole('status')).toHaveTextContent('保存失败')
  await expect.element(screen.getByText('正式 V2', { exact: false })).toBeVisible()
})

it('labels the consumer end and shows the pending channel beside it', async () => {
  const screen = await render(
    <ManagedPublish
      status={{
        ...status,
        entry: 'consumer',
        knowledge_base_ids: ['kb-1', 'kb-2'],
        channel: 'pending',
        model: 'deepseek-v4-flash',
      }}
      save={async () => true}
    />,
  )
  await expect.element(screen.getByText('客户端客服', { exact: true })).toBeVisible()
  await expect.element(screen.getByText('AI 已准备，渠道待接入')).toBeVisible()
  await screen.getByRole('button', { name: '发布到客户端客服' }).click()
  await expect.element(screen.getByRole('status')).toHaveTextContent('已发布到客户端客服')
})

it('shows remote environment badge and confirms before publishing to remote host', async () => {
  const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
  const save = vi.fn(async () => true)
  const screen = await render(
    <ManagedPublish
      status={{
        ...status,
        environment: 'jiejia',
        environment_name: '捷佳四轮国内充电',
        tenant_name: '捷佳',
        remote_managed: true,
      }}
      save={save}
    />,
  )
  await expect.element(screen.getByText('捷佳四轮国内充电 · 捷佳')).toBeVisible()
  await screen.getByRole('button', { name: '发布到管家' }).click()
  expect(confirmSpy).toHaveBeenCalledTimes(1)
  expect(save).toHaveBeenCalledTimes(1)
  await expect
    .element(screen.getByRole('status'))
    .toHaveTextContent('已发布到捷佳四轮国内充电 · 捷佳 · 管家')
})

it('publishes with the loaded route revision and reports concurrent publication conflict', async () => {
  const calls: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      calls.push(JSON.parse(options.body))
      return Response.json({ error: { code: 'SETUP_ROUTE_CHANGED' } }, { status: 409 })
    }),
  )
  await expect(
    publishManaged('app', { pre_prompt: 'SOP', agent_mode: { tools: [] } }, 3),
  ).rejects.toThrow('正式版本已变化')
  expect(calls).toHaveLength(1)
  expect(calls[0]).toMatchObject({ action: 'publish', app_id: 'app', revision: 3 })
  expect(calls[0]?.draft).toMatch(/^[a-f0-9]{64}$/)
})

it('translates the retracted-version refusal instead of blaming the session', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json(
        {
          error: {
            code: 'ROUTE_BINDING_RETRACTED',
            message: '该版本已撤回；要重新上线请用「恢复历史版本」',
          },
        },
        { status: 409 },
      ),
    ),
  )
  await expect(publishManaged('app', { pre_prompt: 'SOP' }, 3)).rejects.toThrow('该版本已撤回')
})

it('shows the backend message with its code for codes without a translation', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      Response.json(
        { error: { code: 'DIFY_FUTURE_CODE', message: 'upstream refused' } },
        { status: 409 },
      ),
    ),
  )
  await expect(publishManaged('app', { pre_prompt: 'SOP' }, 3)).rejects.toThrow(
    'upstream refused（DIFY_FUTURE_CODE）',
  )
})

it('falls back to the bare code rather than a misleading relogin hint', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => Response.json({ error: { code: 'SETUP_FUTURE_CODE' } }, { status: 403 })),
  )
  const error = await publishManaged('app', { pre_prompt: 'SOP' }, 3).catch((e) => e)
  expect(error).toBeInstanceOf(Error)
  expect(error.message).toContain('SETUP_FUTURE_CODE')
  expect(error.message).not.toContain('重新登录')
})

it('describes a non-JSON upstream failure instead of a parse exception', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('<html>bad gateway</html>', { status: 502 })),
  )
  await expect(publishManaged('app', { pre_prompt: 'SOP' }, 3)).rejects.toThrow('502')
})

it('shows the model default when Java has no explicit output cap', async () => {
  const screen = await render(
    <ManagedPublish status={{ ...status, model_options: {} }} save={async () => true} />,
  )
  await expect.element(screen.getByText('输出上限：模型默认', { exact: false })).toBeVisible()
})
