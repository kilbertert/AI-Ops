'use client'

import { Button } from '@langgenius/dify-ui/button'
import { Checkbox } from '@langgenius/dify-ui/checkbox'
import { Input } from '@langgenius/dify-ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectItemIndicator,
  SelectItemText,
  SelectTrigger,
  SelectValue,
} from '@langgenius/dify-ui/select'
import { useEffect, useState } from 'react'
import { manage } from '@/service/aiops-management'

type Tenant = { id: string; name: string; enabled: boolean }
type Environment = Tenant & {
  tenants: Tenant[]
  company_gateway_configured: boolean
  acceptance: 'unverified'
}
type Catalogue = { revision: number; environment: string; environments: Environment[] }

export default function EnvironmentManagement() {
  const [data, setData] = useState<Catalogue | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState('')
  const [tenant, setTenant] = useState('')
  const [name, setName] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const environment = data?.environments.find((item) => item.id === selected)
  const target = tenant ? environment?.tenants.find((item) => item.id === tenant) : environment
  const load = async () => {
    setBusy(true)
    setError('')
    try {
      const result: Catalogue = await manage('environments')
      setData(result)
      setTenant('')
      setSelected((current) =>
        result.environments.some((item) => item.id === current)
          ? current
          : result.environments[0]?.id || '',
      )
    } catch (e) {
      setData(null)
      setError(e instanceof Error ? e.message : '读取环境失败')
    } finally {
      setBusy(false)
    }
  }
  useEffect(() => {
    setName(target?.name || '')
    setEnabled(target?.enabled || false)
  }, [target])
  const save = async () => {
    if (!data || !target) return
    setBusy(true)
    setError('')
    try {
      await manage('environment-update', {
        environment: selected,
        revision: data.revision,
        name,
        enabled,
        ...(tenant ? { tenant_id: tenant } : {}),
      })
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存未完成，请刷新核对')
    } finally {
      setBusy(false)
    }
  }
  return (
    <section aria-label="环境与租户管理" className="px-6 py-3">
      <Button
        variant="secondary"
        disabled={busy}
        onClick={() => {
          setLoaded(true)
          void load()
        }}
      >
        {loaded ? '刷新环境目录' : '环境与租户管理'}
      </Button>
      {error && (
        <p role="alert" className="mt-2 text-text-destructive">
          {error}
        </p>
      )}
      {data && (
        <div className="mt-3 flex flex-wrap items-end gap-3 rounded-xl border p-4">
          <div>
            业务环境
            <Select
              disabled={busy}
              value={selected}
              onValueChange={(value) => {
                setSelected(value || '')
                setTenant('')
                setError('')
              }}
            >
              <SelectTrigger aria-label="业务环境">
                <SelectValue placeholder="选择环境" />
              </SelectTrigger>
              <SelectContent>
                {data.environments.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    <SelectItemText>
                      {item.name}（{item.id}）
                    </SelectItemText>
                    <SelectItemIndicator />
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            管理对象
            <Select
              disabled={busy}
              value={tenant || '/'}
              onValueChange={(value) => {
                setTenant(value === '/' ? '' : value || '')
                setError('')
              }}
            >
              <SelectTrigger aria-label="管理对象">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="/">
                  <SelectItemText>当前环境</SelectItemText>
                  <SelectItemIndicator />
                </SelectItem>
                {environment?.tenants.map((item) => (
                  <SelectItem key={item.id} value={item.id}>
                    <SelectItemText>
                      {item.name}（{item.id}）
                    </SelectItemText>
                    <SelectItemIndicator />
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <label>
            显示名称
            <Input
              aria-label="显示名称"
              value={name}
              disabled={busy || !target}
              maxLength={128}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            <Checkbox
              aria-label="允许业务准入"
              checked={enabled}
              disabled={busy || !target}
              onCheckedChange={(checked) => setEnabled(checked === true)}
            />
            允许业务准入
          </label>
          <Button disabled={busy || !target || !name.trim()} onClick={() => void save()}>
            保存准入设置
          </Button>
          <p role="status" className="w-full text-text-secondary">
            {environment?.name} / {tenant ? target?.name : '环境'} ·
            {environment?.company_gateway_configured ? '公司网关已登记' : '公司网关待登记'} ·
            接入待验收
          </p>
          <p className="w-full text-text-tertiary">
            准入设置不代表已发布或业务已验收。租户应用仍在其所属环境的管理入口开通和编辑。
          </p>
        </div>
      )}
    </section>
  )
}
