import { afterEach, expect, it, vi } from 'vite-plus/test'
import { render } from 'vitest-browser-react'
import EnvironmentManagement from './aiops-environments'

const data = {
  revision: 4,
  environment: 'a',
  environments: [
    {
      id: 'a',
      name: '环境甲',
      enabled: true,
      company_gateway_configured: false,
      acceptance: 'unverified',
      tenants: [{ id: 'same', name: '甲租户', enabled: true }],
    },
    {
      id: 'b',
      name: '环境乙',
      enabled: false,
      company_gateway_configured: true,
      acceptance: 'unverified',
      tenants: [{ id: 'same', name: '乙租户', enabled: false }],
    },
  ],
}

afterEach(() => vi.unstubAllGlobals())

it('keeps environment and tenant edits separate and reports conflicts honestly', async () => {
  const writes: Record<string, unknown>[] = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (_url, options) => {
      const body = JSON.parse(options.body)
      if (body.action === 'environments') return Response.json(data)
      writes.push(body)
      return Response.json(
        { error: { code: 'ENVIRONMENT_REGISTRY_REVISION_CONFLICT' } },
        { status: 409 },
      )
    }),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '环境与租户管理', exact: true }).click()
  await screen.getByRole('combobox', { name: '管理对象' }).click()
  await screen.getByRole('option', { name: '甲租户（same）' }).click()
  await screen.getByRole('textbox', { name: '显示名称' }).fill('甲租户新名称')
  await screen.getByRole('button', { name: '保存准入设置' }).click()
  expect(writes[0]).toEqual({
    action: 'environment-update',
    environment: 'a',
    tenant_id: 'same',
    revision: 4,
    name: '甲租户新名称',
    enabled: true,
  })
  await expect.element(screen.getByRole('alert')).toHaveTextContent('目录已被更新')
  await screen.getByRole('combobox', { name: '业务环境' }).click()
  await screen.getByRole('option', { name: '环境乙（b）' }).click()
  await expect.element(screen.getByRole('textbox', { name: '显示名称' })).toHaveValue('环境乙')
  await expect.element(screen.getByRole('checkbox', { name: '允许业务准入' })).not.toBeChecked()
  await expect.element(screen.getByRole('status')).toHaveTextContent('接入待验收')
  expect(screen.getByRole('alert').elements()).toHaveLength(0)
})

it('removes previously visible catalogue when access is revoked', async () => {
  let reads = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async () =>
      ++reads === 1
        ? Response.json(data)
        : Response.json({ error: { code: 'ENVIRONMENT_MANAGEMENT_FORBIDDEN' } }, { status: 403 }),
    ),
  )
  const screen = await render(<EnvironmentManagement />)
  await screen.getByRole('button', { name: '环境与租户管理', exact: true }).click()
  await expect.element(screen.getByRole('textbox', { name: '显示名称' })).toHaveValue('环境甲')
  await screen.getByRole('button', { name: '刷新环境目录' }).click()
  await expect.element(screen.getByRole('alert')).toHaveTextContent('没有环境管理授权')
  expect(screen.getByRole('textbox', { name: '显示名称' }).elements()).toHaveLength(0)
})
