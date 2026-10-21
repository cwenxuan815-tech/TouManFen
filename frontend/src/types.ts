export interface ModelInfo {
  model_id: string
  display_name: string
  enabled: boolean
  worker_id?: string
  model_version: string | null
  status: 'up' | 'down' | 'unknown' | string
}

export interface ModelsResponse {
  request_id: string
  default_model: string
  classes: string[]
  models: ModelInfo[]
}

export interface ClassifyResponse {
  request_id: string
  model_id: string
  model_version: string | null
  pred_class: string
  latency_ms: number
}

export interface ApiError {
  code: string
  message: string
  details?: Record<string, unknown>
}

export interface ErrorResponse {
  request_id?: string
  error: ApiError
}

export interface CompareResult {
  model_id: string
  status: 'ok' | 'error'
  model_version?: string | null
  pred_class?: string
  latency_ms: number
  error?: ApiError
}

export interface CompareResponse {
  request_id: string
  summary: {
    total: number
    ok: number
    failed: number
    status: 'ok' | 'partial' | 'all_failed'
  }
  results: CompareResult[]
}

export type ClassifyResult = ClassifyResponse | CompareResponse
