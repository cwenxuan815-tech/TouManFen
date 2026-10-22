/** F15：启动时由网关确定的数据来源状态（不触发模型推理）。 */
import { request } from './client'
import type { DemoSource } from '../types'

export interface DemoWorkerSource {
  model_id: string
  worker_id: string
  ready: boolean
  metadata_available: boolean
  data_source: DemoSource
  display_name?: string | null
}

export interface DemoState {
  request_id?: string
  data_source: DemoSource
  ready_models: number
  total_models: number
  mixed: boolean
  reason: string
  source_counts: Record<string, number>
  worker_sources: DemoWorkerSource[]
  display_name?: string | null
  generated_at?: string | null
}

export const DEMO_STATE_READY = true

export function getDemoState(): Promise<DemoState> {
  return request<DemoState>('/demo/state', { method: 'GET' }, { timeoutProfile: 'classify' })
}
