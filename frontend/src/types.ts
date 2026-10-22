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

/**
 * 未校准原始分数的来源标识。
 * 不是校准概率，也不得跨模型比较分数绝对值。
 */
export type ScoreKind = 'raw_proba' | 'raw_fasttext' | 'raw_softmax'

/** top-k 的一个类别/分数配对（分数降序，并列按类别名升序）。 */
export interface TopKEntry {
  class_id: string
  score: number
}

/** F15：结果数据来源。unknown 只用于“尚未得到真实来源”，不得推断为 live。 */
export type DemoSource = 'live' | 'mock' | 'cached' | 'unknown'

/** B4：相似人工反馈引用（待审核，不得替换模型预测）。 */
export interface HumanFeedbackHint {
  corrected_class: string
  status: 'pending'
}

/** B3：拒识阈值来源元数据（必须标明合成负样本、未校准、适用范围有限）。 */
export interface ThresholdMeta {
  source: string
  calibrated: boolean
  method?: string
  max_score_threshold?: number | null
  entropy_threshold?: number | null
  scope_note?: string
  sample_size?: number | null
}

export interface ClassifyResponse {
  request_id: string
  model_id: string
  model_version: string | null
  pred_class: string
  latency_ms: number
  /** 未校准原始分数（按类别 ID 配对）；无分数或校验失败时缺省 / null。 */
  scores?: Record<string, number> | null
  /** 分数来源；无分数时缺省 / null。 */
  score_kind?: ScoreKind | null
  /** 前 k 类（默认 3）；无分数时缺省 / null。 */
  top_k?: TopKEntry[] | null
  /** B4：相似反馈参考（只读提示，不改变 pred_class）。 */
  human_feedback_hint?: HumanFeedbackHint | null
  /** B3：软拒识结果；原 pred_class 仍保留。 */
  rejected?: boolean
  reject_reason?: string | null
  threshold_meta?: ThresholdMeta | null
  /** B11 后由服务端提供；未提供时不得推断为 live。 */
  data_source?: DemoSource | null
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
  /** B11 后由服务端提供；未提供时不得推断为 live。 */
  data_source?: DemoSource | null
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

// ---------------------------------------------------------------------------
// B6/F9：GET /api/v1/compare/stream（SSE）事件契约
//
// 统一契约（B6 后端与 F9 前端一致）：
//   event: model   → CompareStreamModelEvent（每个模型最多一次，按完成顺序）
//   event: summary → CompareStreamSummaryEvent
//   event: done    → CompareStreamDoneEvent（紧接 summary，之后关闭流）
//   event: error   → CompareStreamErrorEvent（流级致命错误）
//   心跳是 `: heartbeat` 注释行，不是事件；EventSource 不会触发 message。
// ---------------------------------------------------------------------------

/** summary/done 的整体状态：全部成功 / 部分 / 全失败。 */
export type CompareStreamStatus = 'ok' | 'partial' | 'all_failed'

/** 单模型完成事件（status=error 时 model_version/pred_class 为 null）。 */
export interface CompareStreamModelEvent {
  request_id: string
  model_id: string
  status: 'ok' | 'error'
  latency_ms: number
  model_version: string | null
  pred_class: string | null
  error: ApiError | null
  data_source?: DemoSource | null
}

export interface CompareStreamSummaryEvent {
  request_id: string
  status: CompareStreamStatus
  total: number
  ok: number
  failed: number
  elapsed_ms: number
  total_timeout_ms: number
  partial: boolean
}

export interface CompareStreamDoneEvent {
  request_id: string
  status: CompareStreamStatus
  total: number
  ok: number
  failed: number
}

/** 流级致命错误（HTTP 200 + `event: error`，随后关闭流）。 */
export interface CompareStreamErrorEvent {
  request_id: string
  code: string
  message: string
}

export type CompareStreamEventName = 'model' | 'summary' | 'done' | 'error'

// ---------------------------------------------------------------------------
// B13：离线指标产物 meta 契约（与 tools/validate_meta.py 保持一致）
// ---------------------------------------------------------------------------
export type MetaDataKind = 'offline' | 'demo'

export interface MetricMeta {
  dataset: string
  split: string
  sample_size: number
  generated_at: string
  code_version: string
  model_versions: Record<string, string>
  metric_def: Record<string, string>
  data_kind: MetaDataKind
}

export interface MetricArtifact<T = unknown> {
  meta: MetricMeta
  models: T[]
}

// ---------------------------------------------------------------------------
// B8/F11：离线基准 PK 契约（GET /api/v1/bench/pk）
//
// 数值全部来自后端返回的离线产物（artifacts/bench/pk_*.json），前端只做展示，
// 不内置/推算任何指标；缺失值一律为 null，由 UI 显示「未提供」。
// ---------------------------------------------------------------------------

export type BenchMetricKey = 'macro_f1' | 'accuracy' | 'avg_latency_ms' | 'size_bytes'

/** 单个模型的离线指标；字段缺失/非法时为 null（不补 0、不估算）。 */
export interface BenchMetrics {
  macro_f1: number | null
  accuracy: number | null
  avg_latency_ms: number | null
  size_bytes: number | null
}

/** 5 个本地模型（LLM 可能 status=missing / reason=external_provider_not_authorized）。 */
export interface BenchModel {
  model_id: string
  display_name: string
  status: 'ok' | 'missing' | string
  metrics: BenchMetrics | null
  confusion_matrix?: number[][] | null
  weight?: number | null
  reason?: string | null
  note?: string | null
}

/** 派生项：加权投票集成 / oracle 上界（均非独立模型）。 */
export interface BenchDerived {
  kind: 'weighted_vote' | 'oracle' | string
  display_name: string
  status: 'ok' | 'missing' | string
  metrics: BenchMetrics | null
  note?: string | null
  reason?: string | null
}

export interface BenchPkResponse {
  /** 包了一层的后端响应才带；raw artifact 时为 undefined。 */
  request_id?: string
  artifact_path?: string
  meta: MetricMeta
  models: BenchModel[]
  derived: BenchDerived[]
  missing_models: string[]
  reproduction?: Record<string, unknown> | null
}

// ---------------------------------------------------------------------------
// B7：竞技场匿名投票契约（F3 API 边界；F10 才接入页面）
// ---------------------------------------------------------------------------
export interface ArenaCandidate {
  anon_id: string
  pred_class: string
  latency_ms?: number
}

export interface ArenaNextResponse {
  request_id: string
  item_id: string
  title: string
  candidates: ArenaCandidate[]
  partial: boolean
}

export interface ArenaVoteRequest {
  client_id: string
  item_id: string
  anon_id: string
}

export interface ArenaSelectedModel {
  model_id: string
  display_name: string
  pred_class: string
}

export interface ArenaVoteResponse {
  request_id: string
  selected: ArenaSelectedModel
  elo_after: number
  games_applied: number
}

export interface ArenaLeaderboardEntry {
  model_id: string
  display_name: string
  elo: number
  votes: number
  sample_size?: number | null
  wins: number
  losses: number
  win_rate: number | null
  wilson_lower: number
  wilson_upper: number
  warming_up: boolean
  rank: number | null
}

export interface ArenaLeaderboardResponse {
  request_id: string
  status: 'warming_up' | 'ready'
  data_kind: 'vote'
  note: string
  min_votes_for_ranking: number
  entries: ArenaLeaderboardEntry[]
}
