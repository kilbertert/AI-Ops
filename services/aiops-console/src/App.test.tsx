import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

const mockCatalogue = {
  registry_revision: 30,
  can_create: true,
  effective: [
    {
      id: 'jiejia',
      name: '捷佳四轮国内充电',
      enabled: true,
      gateway_url: 'https://api.example.test',
      cpms_base_url: 'https://cpms.example.test',
      company_gateway_url: '',
      tenants: [{ id: '2001', name: '捷佳', enabled: true }],
    },
  ],
  drafts: [
    {
      environment: 'jiejia',
      revision: 1,
      registry_changed: false,
      draft: {
        id: 'jiejia',
        name: '捷佳四轮国内充电',
        enabled: true,
        gateway_url: 'https://api.example.test',
        cpms_base_url: 'https://cpms.example.test',
        company_gateway_url: '',
        deployment_target: 'jiejia',
        credential_ref: 'jiejia-env',
        timezone: 'Asia/Shanghai',
        tenants: [{ id: '2001', name: '捷佳', enabled: true }],
      },
    },
  ],
  overview: {
    schema: '1',
    control_environment: '41',
    control_execution_environment: '41',
    control_version: '1.43.23',
    generated_at: 1795690000,
    registry_revision: 30,
    environments: [
      {
        id: 'jiejia',
        name: '捷佳四轮国内充电',
        is_control: false,
        status: 'active',
        enabled: true,
        draft_revision: 1,
        registry_changed: false,
        tenant_count: 1,
        enabled_tenant_count: 1,
        active_route_count: 1,
        nodes: [],
        tenants: [],
        latest_task: null,
      },
    ],
  },
}

const mockReleases = {
  schema: '1',
  environment: 'jiejia',
  is_control: false,
  control_version: '1.43.23',
  enrolled: true,
  gateway_url: 'https://api.example.test',
  current_version: '1.43.23+c4dfba594c69',
  current_revision: 'c4dfba594c69',
  python_minor: '3.12',
  runtime_mode: 'docker',
  current_image_digest: 'sha256:c7c4fa2248130000000000000000000000000000000000000000000000000000',
  current_image_ref: 'qushi-registry.cn-hangzhou.cr.aliyuncs.com/release/aiops-gateway@sha256:c7c4fa224813',
  docker_available: true,
  business_mutations: 'disabled',
  health_ok: true,
  last_probed_at: 1795690000,
  current_ingress_routes: [],
  rollback_target: null,
  catalog_updated_at: 1795690000,
  available_releases: [
    {
      tag: 'v1.43.23',
      version: '1.43.23',
      revision: 'c4dfba594c69',
      short_sha: 'c4dfba594c69',
      full_version: '1.43.23+c4dfba594c69',
      released_at: '2026-11-28T00:00:00Z',
      summary: 'Docker OCI release',
      ingress_routes: [],
      ingress_diff: { changed: false, added_routes: [], removed_routes: [] },
      pool_python_minors: ['3.12'],
    },
  ],
  latest_release: null,
  update_available: false,
  history: [],
}

describe('AI-Ops Standalone Console', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init?: RequestInit) => {
        const body = init?.body ? JSON.parse(String(init.body)) : {}
        if (body.action === 'environment-drafts') {
          return new Response(JSON.stringify(mockCatalogue), { status: 200 })
        }
        if (body.action === 'environment-wizard' && body.wizard_action === 'release_overview') {
          return new Response(JSON.stringify(mockReleases), { status: 200 })
        }
        return new Response(
          JSON.stringify({ tasks: [], sources: [], plans: [] }),
          { status: 200 },
        )
      }),
    )
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders standalone header, environments list, and release panel without Dify dependencies', async () => {
    render(<App />)

    expect(screen.getByText('AI-Ops 管理工作台')).toBeTruthy()
    const backLink = screen.getByRole('link', { name: '返回 Dify 控制台' })
    expect(backLink.getAttribute('href')).toBe('/apps')

    await waitFor(() => {
      expect(screen.getAllByText(/捷佳四轮国内充电/).length).toBeGreaterThan(0)
    })

    fireEvent.click(screen.getByText('版本与发版'))
    await waitFor(() => {
      expect(screen.getAllByText(/1\.43\.23\+c4dfba594c69/).length).toBeGreaterThan(0)
    })
  })
})
