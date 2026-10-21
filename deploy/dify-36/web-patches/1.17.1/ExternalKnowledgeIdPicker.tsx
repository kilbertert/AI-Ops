'use client'

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
import { useQuery } from '@tanstack/react-query'
import * as React from 'react'
import { useTranslation } from 'react-i18next'
import { fetchExternalKnowledgeList } from '@/service/datasets'

type Props = {
  /** The API template the operator picked — the list is scoped to it. */
  externalKnowledgeApiId: string
  value: string
  onChange: (value: string) => void
  inputId?: string
}

/**
 * The Knowledge ID field, with the endpoint's own list behind it.
 *
 * Upstream renders a bare text input here, so the operator has to already know
 * an opaque string — which is why "connect an external knowledge base" needed
 * an engineer. Our gateway serves `GET /v1/dify/knowledge` for exactly this
 * dialog, so the field becomes a picker.
 *
 * Typing stays available in every case. A stock external provider has no such
 * route (the list comes back empty), and an operator may also register a source
 * by id — either way the free-text field is what upstream had, unchanged.
 *
 * The list uses the SAME credential and endpoint as retrieval, so anything
 * offered here is something retrieval accepts. A picker built on anything else
 * would offer choices that 404.
 */
const ExternalKnowledgeIdPicker: React.FC<Props> = ({
  externalKnowledgeApiId,
  value,
  onChange,
  inputId,
}) => {
  const { t } = useTranslation()
  const placeholder = t(($) => $.externalKnowledgeIdPlaceholder, { ns: 'dataset' }) ?? ''
  const enabled = externalKnowledgeApiId !== ''
  const { data, isError } = useQuery({
    queryKey: ['external-knowledge-list', externalKnowledgeApiId],
    queryFn: () => fetchExternalKnowledgeList({ externalKnowledgeApiId }),
    enabled,
    retry: false,
  })
  const items = data?.knowledge_items ?? []

  if (!enabled || isError || items.length === 0) {
    return (
      <Input
        id={inputId}
        value={value}
        onValueChange={onChange}
        placeholder={placeholder}
      />
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <Select
        value={value}
        onValueChange={(next) => {
          if (next == null) return
          onChange(next)
        }}
      >
        <SelectTrigger aria-label={t(($) => $.externalKnowledgeId, { ns: 'dataset' })} className="w-full">
          <SelectValue placeholder={placeholder} />
        </SelectTrigger>
        <SelectContent placement="bottom-start" sideOffset={4} className="w-full">
          {items.map((item) => (
            <SelectItem key={item.knowledge_id} value={item.knowledge_id}>
              <SelectItemText>{item.name}</SelectItemText>
              <span className="ml-2 system-xs-regular text-text-tertiary">
                {item.knowledge_id}
              </span>
              <SelectItemIndicator />
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {/* Still typeable: a source registered by id has to be nameable here, and
          a provider without the list route must behave exactly as upstream. */}
      <Input
        id={inputId}
        value={value}
        onValueChange={onChange}
        placeholder={placeholder}
      />
    </div>
  )
}

export default React.memo(ExternalKnowledgeIdPicker)
