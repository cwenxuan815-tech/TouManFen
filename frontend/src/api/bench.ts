/**
 * F11/B8：离线基准 PK API（只读展示后端产物，不内置任何指标数值）。
 *
 * `GET /api/v1/bench/pk` 可能返回：
 * 1) raw artifact：`{ meta, models, derived, missing_models, ... }`
 * 2) 包装层：`{ request_id, artifact_path, artifact|data|pk|result: <raw artifact> }`
 *
 * 前端只接受包含 `meta` + `models` 的完整 artifact；若后端只给 `artifact_path`
 * 而不给数据，则抛错让页面显示「无法回溯」错误态，绝不伪造/估算数值。
 */
import { ApiClientError, request } from './client'
import type {
  BenchDerived,
  BenchMetrics,
  BenchModel,
  BenchPkResponse,
  MetricMeta,
} from '../types'

/** B8 是否就绪；F11 页面据此决定是否请求（当前已按契约实现，保持 true）。 */
export const BENCH_PK_READY = true

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asNonEmptyString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null
}

function asStringRecord(value: unknown): Record<string, string> | null {
  if (!isRecord(value)) return null
  const output: Record<string, string> = {}
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === 'string') output[key] = entry
    else if (typeof entry === 'number' && Number.isFinite(entry)) output[key] = String(entry)
  }
  return output
}

export function normalizeBenchMetrics(value: unknown): BenchMetrics | null {
  if (!isRecord(value)) return null
  return {
    macro_f1: asFiniteNumber(value.macro_f1),
    accuracy: asFiniteNumber(value.accuracy),
    avg_latency_ms: asFiniteNumber(value.avg_latency_ms),
    size_bytes: asFiniteNumber(value.size_bytes),
  }
}

/**
 * B13 §6.8 meta 校验：缺字段直接抛错（页面显示错误态），不展示不完整的「假来源条」。
 */
export function normalizeBenchMeta(value: unknown): MetricMeta {
  if (!isRecord(value)) {
    throw new ApiClientError(0, 'BENCH_META_INVALID', '基准产物缺少 meta，无法追溯数据来源。')
  }
  const dataset = asNonEmptyString(value.dataset)
  const split = asNonEmptyString(value.split)
  const sampleSize = asFiniteNumber(value.sample_size)
  const generatedAt = asNonEmptyString(value.generated_at)
  const codeVersion = asNonEmptyString(value.code_version)
  const modelVersions = asStringRecord(value.model_versions)
  const metricDef = asStringRecord(value.metric_def)
  const dataKind =
    value.data_kind === 'offline' || value.data_kind === 'demo' ? value.data_kind : null
  const missing: string[] = []
  if (!dataset) missing.push('dataset')
  if (!split) missing.push('split')
  if (sampleSize === null || sampleSize <= 0) missing.push('sample_size')
  if (!generatedAt) missing.push('generated_at')
  if (!codeVersion) missing.push('code_version')
  if (!modelVersions) missing.push('model_versions')
  if (!metricDef) missing.push('metric_def')
  if (!dataKind) missing.push('data_kind')
  if (missing.length) {
    throw new ApiClientError(
      0,
      'BENCH_META_INVALID',
      `基准产物 meta 缺少或非法字段：${missing.join('、')}。`,
    )
  }
  return {
    dataset: dataset as string,
    split: split as string,
    sample_size: sampleSize as number,
    generated_at: generatedAt as string,
    code_version: codeVersion as string,
    model_versions: modelVersions as Record<string, string>,
    metric_def: metricDef as Record<string, string>,
    data_kind: dataKind as MetricMeta['data_kind'],
  }
}

function normalizeBenchModel(value: unknown): BenchModel | null {
  if (!isRecord(value)) return null
  const modelId = asNonEmptyString(value.model_id)
  if (!modelId) return null
  const metrics = normalizeBenchMetrics(value.metrics)
  const status =
    value.status === 'ok' || value.status === 'missing'
      ? value.status
      : metrics
        ? 'ok'
        : 'missing'
  return {
    model_id: modelId,
    display_name: asNonEmptyString(value.display_name) || modelId,
    status,
    metrics,
    confusion_matrix: Array.isArray(value.confusion_matrix)
      ? (value.confusion_matrix as number[][])
      : null,
    weight: asFiniteNumber(value.weight),
    reason: asNonEmptyString(value.reason),
    note: asNonEmptyString(value.note),
  }
}

function normalizeBenchDerived(value: unknown): BenchDerived | null {
  if (!isRecord(value)) return null
  const kind = asNonEmptyString(value.kind)
  if (!kind) return null
  const metrics = normalizeBenchMetrics(value.metrics)
  const status =
    value.status === 'ok' || value.status === 'missing'
      ? value.status
      : metrics
        ? 'ok'
        : 'missing'
  return {
    kind,
    display_name: asNonEmptyString(value.display_name) || kind,
    status,
    metrics,
    note: asNonEmptyString(value.note),
    reason: asNonEmptyString(value.reason),
  }
}

const ARTIFACT_CANDIDATE_KEYS = ['artifact', 'data', 'pk', 'result'] as const

function looksLikeArtifact(value: unknown): value is Record<string, unknown> {
  return isRecord(value) && isRecord(value.meta) && Array.isArray(value.models)
}

/**
 * 兼容 raw artifact 与 `{request_id, artifact_path, artifact|data|...}` 包装层。
 * 导出以便测试对两种形态做断言。
 */
export function normalizeBenchPk(raw: unknown): BenchPkResponse {
  if (!isRecord(raw)) {
    throw new ApiClientError(0, 'BENCH_PAYLOAD_INVALID', '基准接口返回内容不是 JSON 对象。')
  }
  let artifact: Record<string, unknown> | null = looksLikeArtifact(raw) ? raw : null
  if (!artifact) {
    for (const key of ARTIFACT_CANDIDATE_KEYS) {
      if (looksLikeArtifact(raw[key])) {
        artifact = raw[key] as Record<string, unknown>
        break
      }
    }
  }
  if (!artifact) {
    const artifactPath = asNonEmptyString(raw.artifact_path)
    if (artifactPath) {
      throw new ApiClientError(
        0,
        'BENCH_ARTIFACT_PATH_ONLY',
        `基准接口仅返回产物路径（${artifactPath}），未返回指标数据；需后端返回完整 artifact。`,
      )
    }
    throw new ApiClientError(
      0,
      'BENCH_PAYLOAD_INVALID',
      '基准接口未返回可渲染的指标产物（缺 meta/models）。',
    )
  }

  const meta = normalizeBenchMeta(artifact.meta)
  const models = (artifact.models as unknown[])
    .map(normalizeBenchModel)
    .filter((item): item is BenchModel => item !== null)
  const derivedRaw = Array.isArray(artifact.derived) ? artifact.derived : []
  const derived = derivedRaw
    .map(normalizeBenchDerived)
    .filter((item): item is BenchDerived => item !== null)

  const declaredMissing = Array.isArray(artifact.missing_models)
    ? artifact.missing_models.filter((item): item is string => typeof item === 'string')
    : []
  const knownIds = new Set(models.map((item) => item.model_id))
  const derivedMissing = models
    .filter((item) => item.status !== 'ok')
    .map((item) => item.model_id)
  const missingModels = Array.from(new Set([...declaredMissing, ...derivedMissing]))
  // 声明为缺失但产物里没给模型对象的，也补一条「未纳入」行（仅来源为后端声明，不造指标）。
  for (const modelId of declaredMissing) {
    if (!knownIds.has(modelId)) {
      models.push({
        model_id: modelId,
        display_name: modelId,
        status: 'missing',
        metrics: null,
        confusion_matrix: null,
        weight: null,
        reason: null,
        note: null,
      })
      knownIds.add(modelId)
    }
  }

  return {
    request_id:
      asNonEmptyString(raw.request_id) || asNonEmptyString(artifact.request_id) || undefined,
    artifact_path:
      asNonEmptyString(raw.artifact_path) ||
      asNonEmptyString(artifact.artifact_path) ||
      undefined,
    meta,
    models,
    derived,
    missing_models: missingModels,
    reproduction: isRecord(artifact.reproduction) ? artifact.reproduction : null,
  }
}

/** 只读获取最新离线基准产物（GET /bench/pk）。 */
export async function getBenchPk(signal?: AbortSignal): Promise<BenchPkResponse> {
  const raw = await request<unknown>(
    '/bench/pk',
    { method: 'GET' },
    { timeoutProfile: 'compare', signal },
  )
  return normalizeBenchPk(raw)
}
