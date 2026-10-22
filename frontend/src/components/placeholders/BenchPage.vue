<script setup lang="ts">
/**
 * F11：PK 榜单页（离线基准，只读展示）。
 *
 * 数据来源：`GET /api/v1/bench/pk`（B8 离线产物，含 §6.8 meta）。
 * 红线：
 * - 所有数值来自接口返回的离线产物，页面不内置/估算任何指标；
 * - 缺失模型（如 LLM 未授权）只标注「未纳入」，不画条、不参与归一化；
 * - 顶部 meta 来源条逐项展示 dataset/split/sample_size/generated_at/code_version/metric_def；
 * - 不称「实时准确率/线上准确率」，不与竞技场 Elo 混用。
 */
import { computed, onMounted, onScopeDispose, ref, watch } from 'vue'
import { friendlyErrorMessage, getBenchPk, isAbortError } from '../../api'
import type {
  BenchDerived,
  BenchMetricKey,
  BenchMetrics,
  BenchModel,
  BenchPkResponse,
  MetricMeta,
} from '../../types'
import AsyncState from '../common/AsyncState.vue'

type PageState = 'loading' | 'ready' | 'empty' | 'error'
type RowKind = 'model' | 'derived'

interface MetricOption {
  key: BenchMetricKey
  label: string
  shortLabel: string
  direction: string
  unit: string
}

const METRICS: MetricOption[] = [
  {
    key: 'macro_f1',
    label: 'Macro-F1',
    shortLabel: 'F1',
    direction: '越大越好',
    unit: '无量纲（0–1）',
  },
  {
    key: 'avg_latency_ms',
    label: '平均延迟',
    shortLabel: '延迟',
    direction: '越小越好',
    unit: '毫秒（ms）',
  },
  {
    key: 'size_bytes',
    label: '模型体积',
    shortLabel: '体积',
    direction: '越小越好',
    unit: '字节（自动格式化为 B / KiB / MiB）',
  },
]

interface RowSource {
  key: string
  kind: RowKind
  modelId: string | null
  derivedKind: string | null
  displayName: string
  status: string
  metrics: BenchMetrics | null
  reason: string | null
  note: string | null
  weight: number | null
  isEnsemble: boolean
  isOracle: boolean
}

interface BenchRow extends RowSource {
  rawValue: number | null
  showBar: boolean
  barWidth: number
  valueText: string
  exactText: string
  missingText: string
  reasonText: string
  tag: string
}

const state = ref<PageState>('loading')
const data = ref<BenchPkResponse | null>(null)
const error = ref('')
const requestId = ref('')
const metric = ref<BenchMetricKey>('macro_f1')

let controller: AbortController | null = null
let loadSeq = 0

const meta = computed<MetricMeta | null>(() => data.value?.meta ?? null)
const currentMetric = computed<MetricOption>(
  () => METRICS.find((option) => option.key === metric.value) ?? METRICS[0]!,
)
const isEmpty = computed(
  () => Boolean(data.value) && data.value!.models.length === 0 && data.value!.derived.length === 0,
)

const metaItems = computed(() => {
  const current = meta.value
  if (!current) return []
  const metricDef = Object.entries(current.metric_def)
  return [
    { key: 'dataset', label: '数据集', raw: current.dataset, display: current.dataset },
    { key: 'split', label: '切分', raw: current.split, display: current.split },
    {
      key: 'sample_size',
      label: '样本量',
      raw: String(current.sample_size),
      display: `n = ${current.sample_size}`,
    },
    {
      key: 'generated_at',
      label: '生成时间',
      raw: current.generated_at,
      display: current.generated_at,
    },
    {
      key: 'code_version',
      label: '代码版本',
      raw: current.code_version,
      display: current.code_version,
    },
    {
      key: 'metric_def',
      label: '指标口径',
      raw: JSON.stringify(current.metric_def),
      display: metricDef.length
        ? metricDef.map(([key, value]) => `${key}=${value}`).join(' · ')
        : '未提供',
    },
    {
      key: 'data_kind',
      label: '数据性质',
      raw: current.data_kind,
      display: current.data_kind === 'offline' ? '离线评估（测试集）' : '演示数据（demo）',
    },
    {
      key: 'model_versions',
      label: '模型版本',
      raw: JSON.stringify(current.model_versions),
      display:
        Object.entries(current.model_versions)
          .map(([key, value]) => `${key}=${value}`)
          .join(' · ') || '未提供',
    },
  ]
})

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function metricValue(metrics: BenchMetrics | null, key: BenchMetricKey): number | null {
  if (!metrics) return null
  const value = metrics[key]
  return isFiniteNumber(value) && value >= 0 ? value : null
}

function toModelSource(model: BenchModel): RowSource {
  return {
    key: `model:${model.model_id}`,
    kind: 'model',
    modelId: model.model_id,
    derivedKind: null,
    displayName: model.display_name || model.model_id,
    status: model.status,
    metrics: model.metrics,
    reason: model.reason ?? null,
    note: model.note ?? null,
    weight: model.weight ?? null,
    isEnsemble: false,
    isOracle: false,
  }
}

function toDerivedSource(item: BenchDerived): RowSource {
  return {
    key: `derived:${item.kind}`,
    kind: 'derived',
    modelId: null,
    derivedKind: item.kind,
    displayName: item.display_name || item.kind,
    status: item.status,
    metrics: item.metrics,
    reason: item.reason ?? null,
    note: item.note ?? null,
    weight: null,
    isEnsemble: item.kind === 'weighted_vote',
    isOracle: item.kind === 'oracle',
  }
}

const sourceRows = computed<RowSource[]>(() => {
  const current = data.value
  if (!current) return []
  return [
    ...current.models.map(toModelSource),
    ...current.derived.map(toDerivedSource),
  ]
})

/** 归一化基准：只统计状态 ok 且该指标存在的条目，缺失项不参与。 */
const maxValue = computed(() => {
  let max = 0
  for (const row of sourceRows.value) {
    if (row.status !== 'ok') continue
    const value = metricValue(row.metrics, metric.value)
    if (value !== null && value > max) max = value
  }
  return max
})

const oracleSource = computed<RowSource | null>(
  () => sourceRows.value.find((row) => row.isOracle) ?? null,
)
const oracleRawValue = computed<number | null>(() => {
  const row = oracleSource.value
  if (!row || row.status !== 'ok') return null
  return metricValue(row.metrics, metric.value)
})
const oraclePercent = computed<number | null>(() => {
  const value = oracleRawValue.value
  if (value === null || maxValue.value <= 0) return null
  return Math.min(100, Math.max(0, (value / maxValue.value) * 100))
})
const oracleText = computed(() =>
  oracleRawValue.value === null
    ? '未提供'
    : formatMetricValue(metric.value, oracleRawValue.value),
)

function reasonTextOf(reason: string | null): string {
  if (!reason) return ''
  if (reason === 'external_provider_not_authorized') return '外部 Provider 未授权'
  return reason
}

function tagOf(row: RowSource): string {
  if (row.kind !== 'derived') return ''
  if (row.isOracle) return '参考上界，非独立模型'
  if (row.isEnsemble) return '非独立模型（加权投票集成）'
  return '派生指标，非独立模型'
}

const rows = computed<BenchRow[]>(() => {
  const max = maxValue.value
  return sourceRows.value.map((row) => {
    const rawValue = metricValue(row.metrics, metric.value)
    const statusOk = row.status === 'ok'
    const showBar = statusOk && rawValue !== null && max > 0
    const missingText = statusOk
      ? rawValue === null
        ? '该指标未提供'
        : ''
      : '未纳入'
    return {
      ...row,
      rawValue,
      showBar,
      barWidth: showBar && rawValue !== null ? (rawValue / max) * 100 : 0,
      valueText: showBar && rawValue !== null ? formatMetricValue(metric.value, rawValue) : '—',
      exactText: showBar && rawValue !== null ? exactTextOf(metric.value, rawValue) : '',
      missingText,
      reasonText: reasonTextOf(row.reason),
      tag: tagOf(row),
    }
  })
})

const hasEnsemble = computed(() => rows.value.some((row) => row.isEnsemble))
const hasOracle = computed(() => rows.value.some((row) => row.isOracle))

function formatMetricValue(key: BenchMetricKey, value: number): string {
  if (key === 'macro_f1') return value.toFixed(4)
  if (key === 'avg_latency_ms') return `${Math.round(value)} ms`
  return formatBytes(value)
}

function exactTextOf(key: BenchMetricKey, value: number): string {
  if (key === 'macro_f1') return `原始值 ${value}`
  if (key === 'avg_latency_ms') return `${value} ms`
  return `${Math.round(value).toLocaleString('zh-CN')} bytes`
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—'
  if (bytes < 1024) return `${Math.round(bytes)} B`
  const units = ['KiB', 'MiB', 'GiB', 'TiB']
  let value = bytes / 1024
  let unitIndex = 0
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024
    unitIndex += 1
  }
  const digits = value >= 100 ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(digits)} ${units[unitIndex]}`
}

function barWidthStyle(width: number): Record<string, string> {
  return { width: `${width.toFixed(2)}%` }
}

function oracleLineStyle(percent: number): Record<string, string> {
  // oracle 上界通常就是当前指标最大值：此时参考线贴在条形轨道右端（100%），
  // 用 right:0 避免被 overflow 裁剪；其余情况按位置画竖线。
  if (percent >= 99.99) return { right: '0px' }
  return { left: `${percent.toFixed(2)}%` }
}

/** F14：指标切换只播报一句摘要（不再让整个榜单 aria-live 重复朗读全部行）。 */
const liveMessage = ref('')
watch(metric, () => {
  const meta = data.value?.meta
  const source = meta ? `离线评估（${meta.split}，n=${meta.sample_size}）` : '离线基准产物'
  liveMessage.value = `已切换指标：${currentMetric.value.label}，${currentMetric.value.direction}；数据来自${source}。`
})

async function load(): Promise<void> {
  const seq = ++loadSeq
  controller?.abort()
  const next = new AbortController()
  controller = next
  state.value = 'loading'
  error.value = ''
  try {
    const response = await getBenchPk(next.signal)
    if (seq !== loadSeq) return
    data.value = response
    requestId.value = response.request_id ?? ''
    state.value =
      response.models.length === 0 && response.derived.length === 0 ? 'empty' : 'ready'
  } catch (reason) {
    if (seq !== loadSeq || isAbortError(reason)) return
    data.value = null
    requestId.value = ''
    error.value = friendlyErrorMessage(reason)
    state.value = 'error'
  } finally {
    if (controller === next) controller = null
  }
}

onMounted(() => {
  void load()
})

onScopeDispose(() => {
  controller?.abort()
  controller = null
  loadSeq += 1
})

/** 供测试 harness / 调试重新读取；不改变页面行为。 */
defineExpose({ reload: load })
</script>

<template>
  <section id="bench" class="bench-section" aria-labelledby="bench-title" data-page="bench">
    <div class="section-heading bench-heading">
      <div>
        <div class="eyebrow"><span class="eyebrow-line"></span> OFFLINE BENCHMARK · PK</div>
        <h2 id="bench-title">离线测试集上的模型对比。</h2>
      </div>
      <p>
        数据来自离线基准产物，<br class="desktop-break" />不是实时线上指标；LLM 缺失时单独标注。
      </p>
    </div>

    <div class="bench-body" :data-state="state">
      <AsyncState
        v-if="state === 'loading'"
        state="loading"
        loading-text="正在读取离线基准产物…"
      />

      <AsyncState
        v-else-if="state === 'error'"
        state="error"
        title="基准产物读取失败"
        :message="error"
        retry-label="重新读取"
        :request-id="requestId"
        @retry="load"
      />

      <AsyncState
        v-else-if="state === 'empty' || isEmpty"
        state="empty"
        title="暂无基准产物"
        message="后端尚未生成离线基准 JSON，或产物中没有任何模型结果。此处不会展示任何占位数值。"
      />

      <template v-else-if="data && meta">
        <!-- 顶部 meta 来源条：每个字段都可回溯到产物 JSON -->
        <div class="bench-meta" data-role="bench-source-bar">
          <div class="meta-lead">
            <span class="meta-badge" :class="`meta-badge-${meta.data_kind}`">
              {{ meta.data_kind === 'offline' ? '离线评估' : '演示数据（demo）' }}
            </span>
            <span class="meta-title">数据来源 · 离线基准产物</span>
            <span class="meta-request" v-if="requestId">REQUEST · {{ requestId.slice(0, 8) }}</span>
          </div>
          <dl class="meta-grid">
            <div
              v-for="item in metaItems"
              :key="item.key"
              class="meta-item"
              :data-meta-field="item.key"
              :data-meta-value="item.raw"
            >
              <dt>{{ item.label }}</dt>
              <dd>{{ item.display }}</dd>
            </div>
          </dl>
          <div class="meta-artifact" v-if="data.artifact_path">
            产物路径：{{ data.artifact_path }}
          </div>
        </div>

        <!-- 指标切换 -->
        <div class="bench-controls">
          <div class="metric-switch" role="group" aria-label="切换展示指标">
            <button
              v-for="option in METRICS"
              :key="option.key"
              type="button"
              class="metric-button"
              :class="{ active: metric === option.key }"
              :data-metric="option.key"
              :aria-pressed="metric === option.key"
              @click="metric = option.key"
            >
              {{ option.label }}
            </button>
          </div>
          <p class="metric-hint" data-role="metric-hint">
            <b>{{ currentMetric.label }}</b>：{{ currentMetric.direction }}，单位
            {{ currentMetric.unit }}；条形长度按当前指标相对最大值归一化。
          </p>
        </div>

        <!-- 横向条形榜 -->
        <p class="sr-only" role="status" aria-live="polite" aria-atomic="true" data-live="bench">
          {{ liveMessage }}
        </p>
        <div class="bench-board" role="list" data-role="bench-board">
          <div class="board-head" aria-hidden="true">
            <span>模型 / 派生项</span>
            <span>条形（{{ currentMetric.shortLabel }}）</span>
            <span>{{ currentMetric.shortLabel }}</span>
          </div>

          <div
            v-for="(row, index) in rows"
            :key="row.key"
            class="bench-row"
            :class="[
              `bench-row-${row.kind}`,
              {
                'bench-row-missing': row.status !== 'ok',
                'bench-row-oracle': row.isOracle,
                'bench-row-ensemble': row.isEnsemble,
              },
            ]"
            role="listitem"
            :data-bench-key="row.key"
            :data-bench-model="row.modelId || ''"
            :data-bench-kind="row.kind"
            :data-bench-derived="row.derivedKind || ''"
            :data-bench-status="row.status"
            :data-metric="metric"
            :data-metric-value="row.showBar && row.rawValue !== null ? String(row.rawValue) : ''"
          >
            <div class="row-name">
              <span class="row-index" aria-hidden="true">{{ String(index + 1).padStart(2, '0') }}</span>
              <div class="row-title">
                <b>{{ row.displayName }}</b>
                <small v-if="row.kind === 'model'">
                  {{ row.modelId }}
                  <template v-if="row.weight !== null"> · 权重 {{ row.weight }}</template>
                </small>
                <small v-else class="row-derived-tag">{{ row.tag }}</small>
              </div>
            </div>

            <div class="row-track">
              <span
                v-if="row.showBar"
                class="row-bar"
                :style="barWidthStyle(row.barWidth)"
                :data-bar-width="row.barWidth.toFixed(2)"
                :title="`${row.displayName}：${row.valueText}`"
              ></span>
              <span
                v-if="oraclePercent !== null"
                class="oracle-line"
                :class="{ 'oracle-line-edge': oraclePercent >= 99.99 }"
                :style="oracleLineStyle(oraclePercent)"
                :title="`oracle 参考上界：${oracleText}`"
                data-role="oracle-line"
                :data-oracle-percent="oraclePercent.toFixed(2)"
              ></span>
              <span
                v-if="row.missingText"
                class="row-not-included"
                data-role="missing-label"
              >
                {{ row.missingText }}<template v-if="row.reasonText"> · {{ row.reasonText }}</template>
              </span>
            </div>

            <div class="row-value">
              <b data-role="metric-value">{{ row.valueText }}</b>
              <small v-if="row.exactText" class="row-exact">{{ row.exactText }}</small>
              <small v-if="row.note && row.kind === 'derived'" class="row-note">{{ row.note }}</small>
            </div>
          </div>
        </div>

        <div class="bench-legend">
          <span class="legend-item"><i class="legend-mark legend-model"></i>本地模型</span>
          <span v-if="hasEnsemble" class="legend-item">
            <i class="legend-mark legend-ensemble"></i>加权投票集成（非独立模型）
          </span>
          <span v-if="hasOracle" class="legend-item">
            <i class="legend-mark legend-oracle"></i>oracle 参考上界（非独立模型）
          </span>
          <span v-if="oraclePercent !== null" class="legend-item">
            <i class="legend-mark legend-line"></i>虚线 = oracle 参考上界（{{ oracleText }}）
          </span>
        </div>

        <p class="bench-footnote">
          口径：以上均为离线测试集（<code>{{ meta.split }}</code>，n={{ meta.sample_size }}）评估结果，
          <b>不代表实时或线上表现</b>；派生项与 oracle 仅为参考，不与竞技场 Elo 混用。
          缺失模型只标注「未纳入」，不画条形，也不参与当前指标的归一化基准。
        </p>
      </template>
    </div>
  </section>
</template>

<style scoped>
  .bench-section {
    max-width: var(--layout-content);
    margin: 0 auto;
    padding: 72px 6.3% 90px;
  }

  .bench-heading h2 {
    font-size: 36px;
  }

  .bench-body {
    margin-top: var(--space-8);
  }

  .bench-meta {
    position: sticky;
    top: var(--space-8);
    z-index: 2;
    padding: var(--space-14) var(--space-17);
    border: 1px solid var(--border-card);
    border-radius: var(--radius-lg);
    background: var(--surface-card);
    box-shadow: 0 10px 30px #2a3f600d;
  }

  .meta-lead {
    display: flex;
    align-items: center;
    gap: var(--space-10);
    flex-wrap: wrap;
  }

  .meta-badge {
    padding: 2px var(--space-9);
    border-radius: var(--radius-pill);
    font-size: 10px;
    font-weight: 700;
  }

  .meta-badge-offline {
    background: var(--success-soft);
    color: var(--success-text);
  }

  .meta-badge-demo {
    background: var(--warning-soft);
    color: var(--warning-text);
  }

  .meta-title {
    font-size: 11px;
    font-weight: 700;
    color: var(--text-secondary);
  }

  .meta-request {
    margin-left: auto;
    color: var(--text-faint);
    font-size: 9px;
    letter-spacing: .1em;
  }

  .meta-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: var(--space-10) var(--space-17);
    margin: var(--space-12) 0 0;
    padding: 0;
  }

  .meta-item dt {
    color: var(--text-faint);
    font-size: 9px;
    letter-spacing: .08em;
    text-transform: uppercase;
  }

  .meta-item dd {
    margin: 2px 0 0;
    color: var(--text-primary);
    font-size: 11px;
    line-height: 1.5;
    word-break: break-all;
  }

  .meta-artifact {
    margin-top: var(--space-10);
    padding-top: var(--space-8);
    border-top: 1px solid var(--border);
    color: var(--text-subtle);
    font-size: 9px;
    word-break: break-all;
  }

  .bench-controls {
    display: flex;
    align-items: center;
    gap: var(--space-14);
    flex-wrap: wrap;
    margin: var(--space-17) 0 var(--space-12);
  }

  .metric-switch {
    display: inline-flex;
    padding: 3px;
    border: 1px solid var(--border-card);
    border-radius: var(--radius-pill);
    background: var(--surface-card);
  }

  .metric-button {
    border: 0;
    background: transparent;
    color: var(--text-quiet);
    font-size: 11px;
    padding: var(--space-6) var(--space-14);
    border-radius: var(--radius-pill);
    transition: background var(--duration-fast), color var(--duration-fast);
  }

  .metric-button.active {
    background: var(--brand-tint);
    color: var(--brand-strong);
    font-weight: 700;
  }

  .metric-hint {
    margin: 0;
    color: var(--text-quiet);
    font-size: 10px;
    line-height: 1.7;
    flex: 1 1 320px;
  }

  .metric-hint b {
    color: var(--text-secondary);
  }

  .bench-board {
    padding: var(--space-12) var(--space-17) var(--space-8);
    border: 1px solid var(--border-card);
    border-radius: var(--radius-xl);
    background: var(--surface-card);
  }

  .board-head,
  .bench-row {
    display: grid;
    grid-template-columns: minmax(170px, 220px) minmax(0, 1fr) minmax(110px, 150px);
    align-items: center;
    gap: var(--space-14);
  }

  .board-head {
    padding: 0 0 var(--space-8);
    border-bottom: 1px solid var(--border);
    color: var(--text-faint);
    font-size: 9px;
    letter-spacing: .08em;
    text-transform: uppercase;
  }

  .board-head span:last-child {
    text-align: right;
  }

  .bench-row {
    padding: var(--space-10) 0;
    border-bottom: 1px solid var(--border);
  }

  .bench-row:last-child {
    border-bottom: 0;
  }

  .row-name {
    display: flex;
    align-items: flex-start;
    gap: var(--space-10);
    min-width: 0;
  }

  .row-index {
    color: var(--text-subtle);
    font-size: 9px;
    letter-spacing: .08em;
    padding-top: 2px;
  }

  .row-title {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }

  .row-title b {
    font-size: 12px;
    color: var(--text-primary);
    word-break: break-word;
  }

  .row-title small {
    color: var(--text-faint);
    font-size: 9px;
    line-height: 1.5;
  }

  .row-derived-tag {
    color: var(--warning-text) !important;
    font-weight: 700;
  }

  .row-track {
    position: relative;
    height: 18px;
    display: flex;
    align-items: center;
    border-radius: var(--radius-pill);
    background: var(--surface-page);
    overflow: hidden;
  }

  .row-bar {
    display: block;
    height: 100%;
    border-radius: var(--radius-pill);
    background: linear-gradient(90deg, var(--brand-strong), var(--brand));
    transition: width var(--duration-medium) ease;
    min-width: 2px;
  }

  .bench-row-ensemble .row-bar {
    background: linear-gradient(90deg, #2f9e6a, #58c18f);
  }

  .bench-row-oracle .row-bar {
    background: repeating-linear-gradient(
      135deg,
      var(--warning-strong) 0 6px,
      #f0d9a8 6px 12px
    );
  }

  .oracle-line {
    position: absolute;
    top: -3px;
    bottom: -3px;
    width: 0;
    border-left: 1px dashed var(--text-muted);
    pointer-events: none;
  }

  .oracle-line-edge {
    left: auto !important;
    border-left: 0;
    border-right: 1px dashed var(--warning-strong);
  }

  .row-not-included {
    position: relative;
    z-index: 1;
    padding: 0 var(--space-10);
    color: var(--text-quiet);
    font-size: 10px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .bench-row-missing .row-track {
    background: transparent;
    border: 1px dashed var(--border-card);
  }

  .bench-row-missing .row-title b {
    color: var(--text-quiet);
  }

  .row-value {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 1px;
    text-align: right;
  }

  .row-value b {
    color: var(--text-primary);
    font-size: 13px;
    font-variant-numeric: tabular-nums;
  }

  .row-exact,
  .row-note {
    color: var(--text-faint);
    font-size: 9px;
    line-height: 1.4;
  }

  .row-note {
    max-width: 170px;
  }

  .bench-legend {
    display: flex;
    align-items: center;
    gap: var(--space-15);
    flex-wrap: wrap;
    margin-top: var(--space-12);
    color: var(--text-quiet);
    font-size: 10px;
  }

  .legend-item {
    display: inline-flex;
    align-items: center;
    gap: var(--space-6);
  }

  .legend-mark {
    width: 18px;
    height: 8px;
    border-radius: var(--radius-pill);
    background: var(--brand);
  }

  .legend-ensemble {
    background: #58c18f;
  }

  .legend-oracle {
    background: repeating-linear-gradient(
      135deg,
      var(--warning-strong) 0 5px,
      #f0d9a8 5px 10px
    );
  }

  .legend-line {
    width: 0;
    height: 12px;
    border-left: 1px dashed var(--text-faint);
    border-radius: 0;
    background: transparent;
  }

  .bench-footnote {
    margin: var(--space-14) 0 0;
    padding: var(--space-12) var(--space-14);
    border: 1px solid var(--border-card);
    border-left: 3px solid var(--warning-strong);
    border-radius: var(--radius-md);
    background: var(--warning-soft);
    color: var(--warning-text);
    font-size: 10px;
    line-height: 1.8;
  }

  .bench-footnote b {
    font-weight: 700;
  }

  .bench-footnote code {
    font-family: inherit;
    font-weight: 700;
  }

  @media (prefers-reduced-motion: reduce) {
    .row-bar {
      transition: none;
    }
  }

  @media (max-width: 860px) {
    .board-head,
    .bench-row {
      grid-template-columns: minmax(120px, 150px) minmax(0, 1fr) minmax(88px, 110px);
      gap: var(--space-10);
    }

    .row-title b {
      font-size: 11px;
    }

    .row-value b {
      font-size: 12px;
    }
  }

  @media (max-width: 640px) {
    .bench-section {
      padding: 56px 5% 64px;
    }

    .bench-heading h2 {
      font-size: 28px;
    }

    .board-head {
      display: none;
    }

    .bench-row {
      grid-template-columns: minmax(0, 1fr) minmax(84px, 110px);
      grid-template-areas:
        'name value'
        'track track';
      row-gap: var(--space-6);
    }

    .row-name {
      grid-area: name;
    }

    .row-track {
      grid-area: track;
    }

    .row-value {
      grid-area: value;
    }

    .meta-request {
      margin-left: 0;
    }
  }
</style>
