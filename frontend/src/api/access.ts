/** 阶段 5C：访问管理 API 封装。 */
import { ACCESS_TICKET_HEADER } from './accessTicket'
import { request } from './client'

export type AccessWireState =
  | 'active'
  | 'ready'
  | 'queued'
  | 'cooling'
  | 'expired'
  | 'cancelled'
  | 'queue_full'

export interface AccessStateResponse {
  request_id: string
  state: AccessWireState
  ticket?: string | null
  position?: number | null
  ahead?: number | null
  remaining_seconds?: number | null
}

const ACCESS_OPTIONS = {
  timeoutProfile: 'access' as const,
  includeAccessTicket: false,
}

function ticketHeader(ticket?: string | null): Record<string, string> {
  const value = (ticket || '').trim()
  return value ? { [ACCESS_TICKET_HEADER]: value } : {}
}

export function enterAccess(ticket?: string | null): Promise<AccessStateResponse> {
  return request<AccessStateResponse>(
    '/access/enter',
    {
      method: 'POST',
      headers: ticketHeader(ticket),
    },
    ACCESS_OPTIONS,
  )
}

export function getAccessStatus(ticket: string): Promise<AccessStateResponse> {
  return request<AccessStateResponse>(
    '/access/status',
    {
      method: 'GET',
      headers: ticketHeader(ticket),
    },
    ACCESS_OPTIONS,
  )
}

export function claimAccess(ticket: string): Promise<AccessStateResponse> {
  return request<AccessStateResponse>(
    '/access/claim',
    {
      method: 'POST',
      headers: ticketHeader(ticket),
    },
    ACCESS_OPTIONS,
  )
}

export function leaveAccess(ticket: string): Promise<AccessStateResponse> {
  return request<AccessStateResponse>(
    '/access/leave',
    {
      method: 'POST',
      headers: ticketHeader(ticket),
    },
    ACCESS_OPTIONS,
  )
}

export function cancelAccess(ticket: string): Promise<AccessStateResponse> {
  return request<AccessStateResponse>(
    '/access/cancel',
    {
      method: 'POST',
      headers: ticketHeader(ticket),
    },
    ACCESS_OPTIONS,
  )
}
