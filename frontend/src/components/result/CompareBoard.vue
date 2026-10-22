<script setup lang="ts">
/**
 * F9：对比面板（流式 + 旧 POST 完整结果共用一个视图）。
 *
 * 数据来源二选一：
 * - `stream`：`useCompareStream` 的快照（逐条落位、等待中、回退标记、首事件耗时）；
 * - `data`：旧 `POST /compare` 的完整结果（显式 submit / 回退后的兜底展示）。
 *
 * 红线：不伪造事件/结果；等待中只显示骨架，不用前端定时器动画结果；
 * 单模型 error 只影响该卡片；分歧高亮只基于已到达的 ok 预测。
 */
import { computed, ref } from 'vue'
import type { CompareResponse, DemoSource, ModelInfo } from '../../types'
import type { CompareStreamSnapshot } from '../../composables/useCompareStream'
import { categoryInfo, modelLabel } from '../shared/meta'
import AsyncState from '../common/AsyncState.vue'
import DemoBadge from '../placeholders/DemoBadge.vue'

const props = defineProps<{
  models: ModelInfo[]
  title: string
  /** 旧 POST /compare 完整结果（显式 submit / 回退路径）。 */
  data?: CompareResponse | null
  /** F9 流式快照；status 非 idle 时进入流式展示。 */
  stream?: CompareStreamSnapshot | null
}>()

type SortMode = 'latency' | 'consensus'
type ItemStatus = 'ok' | 'error' | 'waiting' | 'missing'
type Consistency = 'match' | 'differ' | 'none'

interface RawItem {
  model_id: string
  status: ItemStatus
  pred_class: string | null
  latency_ms: number | null
  model_version: string | null
  data_source?: DemoSource | null
  error: { code: string; message: string } | null
  requestedIndex: number
}

interface BoardItem extends RawItem {
  consistency: Consistency
}

interface BoardSummary {
  status: 'ok' | 'partial' | 'all_failed'
  total: number
  ok: number
  failed: number
  elapsed_ms: number | null
  total_timeout_ms: number | null
  partial: boolean
  source: 'stream' | 'fallback' | 'post'
}

const sortMode = ref<SortMode>('latency')

const isStream = computed(() => Boolean(props.stream && props.stream.status !== 'idle'))
const streamStatus = computed(() => props.stream?.status ?? 'idle')
const streamActive = computed(
  () => isStream.value && (streamStatus.value === 'starting' || streamStatus.value === 'streaming'),
)

/** 流式/回退/POST 三种来源统一成同一套卡片数据。 */
const rawItems = computed<RawItem[]>(() => {
  const stream = props.stream
  if (isStream.value && stream) {
    const pendingStatus: ItemStatus = streamActive.value ? 'waiting' : 'missing'
    const orderedIds: string[] = [...stream.requestedModelIds]
    for (const modelId of stream.arrivalOrder) {
      if (!orderedIds.includes(modelId)) orderedIds.push(modelId)
    }
    return orderedIds.map((modelId, index) => {
      const event = stream.arrived[modelId]
      if (!event) {
        return {
          model_id: modelId,
          status: pendingStatus,
          pred_class: null,
          latency_ms: null,
          model_version: null,
          data_source: null,
          error: null,
          requestedIndex: index,
        }
      }
      return {
        model_id: modelId,
        status: event.status === 'ok' ? 'ok' : 'error',
        pred_class: event.pred_class,
        latency_ms: event.latency_ms,
        model_version: event.model_version,
        data_source: event.data_source ?? null,
        error: event.error,
        requestedIndex: index,
      }
    })
  }

  const data = props.data
  if (!data) return []
  return data.results.map((item, index) => ({
    model_id: item.model_id,
    status: item.status === 'ok' ? 'ok' : 'error',
    pred_class: item.pred_class ?? null,
    latency_ms: typeof item.latency_ms === 'number' ? item.latency_ms : null,
    model_version: item.model_version ?? null,
    data_source: item.data_source ?? null,
    error: item.error ?? null,
    requestedIndex: index,
  }))
})

const arrivedItems = computed(() =>
  rawItems.value.filter((item) => item.status === 'ok' || item.status === 'error'),
)
const okItems = computed(() =>
  arrivedItems.value.filter((item) => item.status === 'ok' && item.pred_class),
)

/**
 * 多数预测：出现次数严格最多的类别，且至少出现 2 次。
 * 无多数（相等并列 / 只有 1 个 ok）时返回 null，UI 显示「—/分歧」。
 */
const majorityClass = computed<string | null>(() => {
  const counts = new Map<string, number>()
  for (const item of okItems.value) {
    if (!item.pred_class) continue
    counts.set(item.pred_class, (counts.get(item.pred_class) ?? 0) + 1)
  }
  let topClass: string | null = null
  let topCount = 0
  let tie = false
  for (const [classId, count] of counts) {
    if (count > topCount) {
      topClass = classId
      topCount = count
      tie = false
    } else if (count === topCount) {
      tie = true
    }
  }
  if (!topClass || topCount < 2 || tie) return null
  // `tie` 只反映「最后更新的计数并列」；再显式确认没有第二个类别达到 topCount。
  for (const [classId, count] of counts) {
    if (classId !== topClass && count >= topCount) return null
  }
  return topClass
})

const distinctOkClasses = computed(() => {
  const set = new Set<string>()
  for (const item of okItems.value) {
    if (item.pred_class) set.add(item.pred_class)
  }
  return set.size
})

const items = computed<BoardItem[]>(() =>
  rawItems.value.map((item) => {
    let consistency: Consistency = 'none'
    if (item.status === 'ok' && item.pred_class && majorityClass.value) {
      consistency = item.pred_class === majorityClass.value ? 'match' : 'differ'
    }
    return { ...item, consistency }
  }),
)

const differCount = computed(
  () => items.value.filter((item) => item.consistency === 'differ').length,
)
const hasDivergence = computed(() => differCount.value > 0)
/** 已到达 2 个以上 ok、但没有多数类别 → 明确标「分歧」。 */
const noConsensus = computed(
  () => okItems.value.length >= 2 && !majorityClass.value && distinctOkClasses.value > 1,
)

/** 排序：按延迟（默认）/ 按一致度；等待与错误项统一后置，保持稳定顺序。 */
const sortedItems = computed<BoardItem[]>(() => {
  const list = [...items.value]
  if (sortMode.value === 'latency') {
    return list.sort((left, right) => {
      const leftPending = left.status === 'waiting' || left.status === 'missing'
      const rightPending = right.status === 'waiting' || right.status === 'missing'
      if (leftPending !== rightPending) return leftPending ? 1 : -1
      const leftLatency = left.latency_ms ?? Number.POSITIVE_INFINITY
      const rightLatency = right.latency_ms ?? Number.POSITIVE_INFINITY
      if (leftLatency !== rightLatency) return leftLatency - rightLatency
      return left.requestedIndex - right.requestedIndex
    })
  }
  const rank = (item: BoardItem): number => {
    if (item.status === 'waiting' || item.status === 'missing') return 4
    if (item.status === 'error') return 3
    if (item.consistency === 'match') return 0
    if (item.consistency === 'differ') return 1
    return 2
  }
  return list.sort((left, right) => {
    const delta = rank(left) - rank(right)
    if (delta !== 0) return delta
    return left.requestedIndex - right.requestedIndex
  })
})

/** 汇总：流式 summary/done 优先，其次旧 POST summary，都没有时是等待态。 */
const activeSummary = computed<BoardSummary | null>(() => {
  const stream = props.stream
  if (isStream.value && stream) {
    const source: BoardSummary['source'] = stream.fallback ? 'fallback' : 'stream'
    if (stream.summary) {
      return {
        status: stream.summary.status,
        total: stream.summary.total,
        ok: stream.summary.ok,
        failed: stream.summary.failed,
        elapsed_ms: stream.summary.elapsed_ms,
        total_timeout_ms: stream.summary.total_timeout_ms,
        partial: stream.summary.partial,
        source,
      }
    }
    if (stream.done) {
      return {
        status: stream.done.status,
        total: stream.done.total,
        ok: stream.done.ok,
        failed: stream.done.failed,
        elapsed_ms:
          stream.startedAt !== null && stream.finishedAt !== null
            ? Math.max(0, stream.finishedAt - stream.startedAt)
            : null,
        total_timeout_ms: null,
        partial: stream.done.status === 'partial',
        source,
      }
    }
    return null
  }

  if (props.data) {
    return {
      status: props.data.summary.status,
      total: props.data.summary.total,
      ok: props.data.summary.ok,
      failed: props.data.summary.failed,
      elapsed_ms: null,
      total_timeout_ms: null,
      partial: props.data.summary.status === 'partial',
      source: 'post',
    }
  }
  return null
})

const overviewState = computed<'waiting' | 'ok' | 'partial' | 'all_failed' | 'error'>(() => {
  if (props.stream?.status === 'error') return 'error'
  return activeSummary.value?.status ?? 'waiting'
})

const overviewTitle = computed(() => {
  if (overviewState.value === 'error') return '对比流中断'
  if (overviewState.value === 'waiting') return '正在等待模型返回…'
  if (overviewState.value === 'ok') return '全部模型已完成'
  if (overviewState.value === 'partial') return '部分结果已返回'
  return '模型暂时无法完成判断'
})

const overviewIcon = computed(() => {
  if (overviewState.value === 'error') return '!'
  if (overviewState.value === 'waiting') return '…'
  if (overviewState.value === 'ok') return '✓'
  if (overviewState.value === 'partial') return '◐'
  return '!'
})

const progressText = computed(() => {
  if (!isStream.value || !props.stream) return ''
  const total =
    props.stream.requestedModelIds.length ||
    props.stream.summary?.total ||
    activeSummary.value?.total ||
    rawItems.value.length
  return `已返回 ${arrivedItems.value.length} / ${total}`
})

const summaryDetail = computed(() => {
  const current = activeSummary.value
  if (!current) return ''
  const parts = [`${current.ok} 成功`, `${current.failed} 失败`]
  if (current.elapsed_ms !== null) parts.push(`用时 ${Math.round(current.elapsed_ms)} ms`)
  if (current.total_timeout_ms !== null && current.total_timeout_ms > 0) {
    parts.push(`总截止 ${current.total_timeout_ms} ms`)
  }
  return parts.join(' · ')
})

const sourceBadge = computed(() => {
  if (!isStream.value || !props.stream) {
    return { label: '完整请求（POST）', kind: 'post' as const }
  }
  if (props.stream.fallback) {
    return { label: '已回退完整请求（POST）', kind: 'fallback' as const }
  }
  return { label: '流式实时（SSE）', kind: 'stream' as const }
})

const firstEventText = computed(() => {
  const latency = props.stream?.firstEventLatencyMs
  if (!isStream.value || latency === null || latency === undefined) return ''
  return `首结果 ${Math.round(latency)} ms`
})

const requestIdText = computed(() => {
  const value = props.stream?.requestId || props.data?.request_id || ''
  return value ? `REQUEST · ${value.slice(0, 8)}` : ''
})

function itemLabel(modelId: string): string {
  return modelLabel(props.models, modelId)
}

function itemCategory(item: BoardItem) {
  return item.pred_class ? categoryInfo(item.pred_class) : null
}

function latencyText(item: BoardItem): string {
  if (item.latency_ms === null) return '—'
  return `${item.latency_ms} ms`
}

function consistencyLabel(item: BoardItem): string {
  if (item.status !== 'ok') return ''
  if (!majorityClass.value) return '—'
  return item.consistency === 'match' ? '与多数一致' : '与多数不同'
}

function itemStatusText(item: BoardItem): string {
  if (item.status === 'ok') return '分类完成'
  if (item.status === 'error') return item.error?.code || '调用失败'
  if (item.status === 'missing') return '未返回'
  return '等待中'
}
</script>

<template>
  <div
    class="compare-overview"
    :class="[`summary-${overviewState}`, { 'overview-fallback': stream?.fallback }]"
    aria-live="polite"
  >
    <div class="overview-icon" aria-hidden="true">{{ overviewIcon }}</div>
    <div class="overview-copy">
      <b>{{ overviewTitle }}</b>
      <span v-if="summaryDetail">{{ summaryDetail }}</span>
      <span v-else-if="progressText" class="overview-progress">{{ progressText }}</span>
    </div>
    <div class="overview-badges">
      <span class="source-badge" :class="`source-${sourceBadge.kind}`">{{ sourceBadge.label }}</span>
      <span v-if="firstEventText" class="timing-badge">{{ firstEventText }}</span>
      <span v-if="hasDivergence" class="divergence-badge" data-role="divergence-badge">
        模型间存在分歧（{{ differCount }} 个不同）
      </span>
      <span v-else-if="noConsensus" class="divergence-badge" data-role="divergence-badge">
        预测分歧，无多数
      </span>
    </div>
    <div class="overview-title" :title="title">{{ title }}</div>
  </div>

  <AsyncState
    v-if="overviewState === 'error' && (stream?.error || data)"
    class="compare-error"
    state="error"
    :message="stream?.error || '对比请求没有完成，请稍后重试。'"
    :request-id="stream?.requestId || ''"
  />

  <div v-if="isStream" class="compare-toolbar">
    <span class="toolbar-label">排序</span>
    <div class="sort-switch" role="group" aria-label="对比结果排序">
      <button
        type="button"
        data-sort="latency"
        :aria-pressed="sortMode === 'latency'"
        :class="{ active: sortMode === 'latency' }"
        @click="sortMode = 'latency'"
      >
        按延迟
      </button>
      <button
        type="button"
        data-sort="consensus"
        :aria-pressed="sortMode === 'consensus'"
        :class="{ active: sortMode === 'consensus' }"
        @click="sortMode = 'consensus'"
      >
        按一致度
      </button>
    </div>
    <span class="toolbar-note">
      {{ majorityClass ? '一致度 = 与多数预测相同' : '当前无多数预测，一致度显示 —' }}
    </span>
    <span v-if="requestIdText" class="toolbar-request">{{ requestIdText }}</span>
  </div>

  <div class="compare-grid" :class="{ 'compare-grid-waiting': streamActive }">
    <article
      v-for="item in sortedItems"
      :key="item.model_id"
      class="compare-card"
      :class="[
        `compare-card-${item.status}`,
        {
          'compare-card-error': item.status === 'error',
          'compare-card-waiting': item.status === 'waiting',
          'compare-card-missing': item.status === 'missing',
          'compare-card-differ': item.consistency === 'differ',
        },
      ]"
      :data-model-id="item.model_id"
      :data-status="item.status"
      :data-consistency="item.consistency"
    >
      <div class="compare-card-head">
        <span class="model-avatar" :class="`avatar-${item.model_id}`">
          {{ itemLabel(item.model_id).slice(0, 1) }}
        </span>
        <span
          class="model-status-dot"
          :class="item.status"
          :aria-label="itemStatusText(item)"
        ></span>
      </div>
      <div class="compare-model-name">{{ itemLabel(item.model_id) }}</div>
      <DemoBadge v-if="item.data_source" class="compare-source-badge" :source="item.data_source" />

      <template v-if="item.status === 'ok' && itemCategory(item)">
        <div class="compare-category" :class="`text-${itemCategory(item)!.color}`">
          {{ itemCategory(item)!.zh }}
        </div>
        <div class="compare-category-id">{{ item.pred_class }}</div>
        <div
          v-if="item.consistency !== 'none'"
          class="consensus-tag"
          :class="item.consistency === 'match' ? 'consensus-match' : 'consensus-differ'"
        >
          {{ consistencyLabel(item) }}
        </div>
      </template>

      <div v-else-if="item.status === 'error'" class="compare-failure">
        {{ item.error?.message || '模型暂不可用' }}
        <small v-if="item.error?.code">错误码 {{ item.error.code }}</small>
      </div>

      <div v-else-if="item.status === 'missing'" class="compare-failure compare-missing">
        未在总截止前返回结果
      </div>

      <div v-else class="compare-waiting" role="status">
        <span class="waiting-skeleton" aria-hidden="true"><i></i><i></i></span>
        <span class="waiting-text">{{ itemLabel(item.model_id) === 'LLM' ? 'LLM 等待中…' : '等待中…' }}</span>
      </div>

      <div class="compare-card-foot">
        <span>{{ itemStatusText(item) }}</span>
        <b :data-latency="item.latency_ms ?? ''">{{ latencyText(item) }}</b>
      </div>
    </article>

    <div v-if="!sortedItems.length && streamActive" class="compare-waiting-block" role="status">
      <span class="waiting-skeleton waiting-skeleton-lg" aria-hidden="true"><i></i><i></i><i></i></span>
      <span class="waiting-text">正在等待首个模型结果…</span>
    </div>
  </div>
</template>

<style scoped>
  .compare-overview {
    display: flex;
    align-items: center;
    gap: var(--space-12);
    padding: var(--space-14) var(--space-17);
    border: 1px solid #e6ebf2;
    border-radius: var(--radius-lg);
    background: var(--surface-card);
    flex-wrap: wrap;
  }

  .overview-icon {
    width: 29px;
    height: 29px;
    flex: none;
    display: grid;
    place-items: center;
    border-radius: var(--radius-xs);
    background: #e8f7ef;
    color: #2f9e6a;
    font-weight: 700;
  }

  .summary-waiting .overview-icon {
    background: var(--brand-tint);
    color: var(--brand-strong);
  }

  .summary-partial .overview-icon {
    background: #fff5dc;
    color: #bd8622;
  }

  .summary-all_failed .overview-icon,
  .summary-error .overview-icon {
    background: var(--danger-soft);
    color: #c15a53;
  }

  .overview-copy {
    display: flex;
    flex-direction: column;
    gap: 3px;
    min-width: 0;
  }

  .overview-copy b {
    font-size: 11px;
  }

  .overview-copy span {
    font-size: 10px;
    color: #9099a7;
  }

  .overview-progress {
    color: var(--brand-strong) !important;
  }

  .overview-badges {
    display: flex;
    align-items: center;
    gap: var(--space-6);
    flex-wrap: wrap;
  }

  .source-badge,
  .timing-badge,
  .divergence-badge {
    padding: 2px var(--space-8);
    border-radius: var(--radius-pill);
    font-size: 9px;
    line-height: 1.7;
    white-space: nowrap;
  }

  .source-stream {
    background: #e8f7ef;
    color: #2f8f63;
  }

  .source-fallback {
    background: #fff5dc;
    color: #9c6c16;
  }

  .source-post {
    background: #eef3ff;
    color: #4d73be;
  }

  .timing-badge {
    background: var(--surface-page);
    color: var(--text-quiet);
  }

  .divergence-badge {
    background: #fff1ef;
    color: #b4524b;
  }

  .overview-title {
    margin-left: auto;
    max-width: 100%;
    color: #727d8d;
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .compare-error {
    margin-top: 13px;
  }

  .compare-toolbar {
    display: flex;
    align-items: center;
    gap: var(--space-8);
    margin-top: 13px;
    flex-wrap: wrap;
  }

  .toolbar-label {
    color: var(--text-faint);
    font-size: 10px;
  }

  .sort-switch {
    display: inline-flex;
    border: 1px solid var(--border-card);
    border-radius: var(--radius-pill);
    padding: 2px;
    background: var(--surface-card);
  }

  .sort-switch button {
    border: 0;
    background: transparent;
    color: var(--text-quiet);
    font-size: 10px;
    padding: 4px 10px;
    border-radius: var(--radius-pill);
  }

  .sort-switch button.active {
    background: var(--brand-tint);
    color: var(--brand-strong);
    font-weight: 700;
  }

  .toolbar-note {
    color: var(--text-subtle);
    font-size: 9px;
  }

  .toolbar-request {
    margin-left: auto;
    color: #9ba4b2;
    font-size: 9px;
    letter-spacing: .1em;
  }

  .compare-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit,minmax(170px,1fr));
    gap: var(--space-12);
    margin-top: 13px;
  }

  .compare-card {
    position: relative;
    padding: var(--space-17) var(--space-17) 13px;
    background: var(--surface-card);
    border: 1px solid #e8ecf2;
    border-radius: var(--radius-xl);
    min-height: 160px;
  }

  .compare-card-differ {
    border-color: #f0c9c4;
    box-shadow: 0 0 0 2px #fff1ef inset;
  }

  .compare-card-waiting,
  .compare-card-missing {
    border-style: dashed;
    background: var(--surface-page);
  }

  .compare-card-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .model-avatar {
    width: 28px;
    height: 28px;
    display: grid;
    place-items: center;
    border-radius: var(--radius-xs);
    background: var(--brand-soft);
    color: #4d73be;
    font-size: 12px;
    font-weight: 700;
  }

  .avatar-fasttext {
    background: #e9f8f2;
    color: #2c9867;
  }

  .avatar-bert {
    background: #f1edff;
    color: #7556c5;
  }

  .avatar-llm {
    background: #fff4e4;
    color: #b47a21;
  }

  .avatar-distill {
    background: #eaf8fb;
    color: #32859c;
  }

  .model-status-dot {
    width: 7px;
    height: 7px;
    border-radius: var(--radius-circle);
    background: #36b278;
    box-shadow: 0 0 0 4px #e6f7ee;
  }

  .model-status-dot.error {
    background: #e4746d;
    box-shadow: 0 0 0 4px var(--danger-soft);
  }

  .model-status-dot.waiting,
  .model-status-dot.missing {
    background: #c3c9d3;
    box-shadow: 0 0 0 4px #f1f3f6;
  }

  .compare-model-name {
    margin-top: var(--space-12);
    font-size: 11px;
    color: var(--text-secondary);
  }

  .compare-category {
    margin-top: var(--space-6);
    font-size: 27px;
    line-height: 1.2;
    font-weight: 700;
    letter-spacing: -.04em;
  }

  .compare-category-id {
    margin-top: 2px;
    color: #9ba4b0;
    font-size: 9px;
  }

  .consensus-tag {
    display: inline-block;
    margin-top: var(--space-6);
    padding: 1px var(--space-7);
    border-radius: var(--radius-pill);
    font-size: 9px;
  }

  .consensus-match {
    background: #eef7f2;
    color: #3a8a63;
  }

  .consensus-differ {
    background: #fff1ef;
    color: #b4524b;
  }

  .text-blue { color: var(--cat-blue-text); }
  .text-violet { color: var(--cat-violet-text); }
  .text-cyan { color: var(--cat-cyan-text); }
  .text-amber { color: var(--cat-amber-text); }
  .text-indigo { color: var(--cat-indigo-text); }
  .text-green { color: var(--cat-green-text); }
  .text-red { color: var(--cat-red-text); }
  .text-orange { color: var(--cat-orange-text); }
  .text-pink { color: var(--cat-pink-text); }
  .text-purple { color: var(--cat-purple-text); }

  .compare-failure {
    min-height: 41px;
    margin-top: var(--space-8);
    color: #bc615b;
    font-size: 10px;
    line-height: 1.6;
  }

  .compare-failure small {
    display: block;
    margin-top: 2px;
    color: #c99a96;
    font-size: 9px;
  }

  .compare-missing {
    color: var(--text-quiet);
  }

  .compare-waiting {
    min-height: 41px;
    margin-top: var(--space-8);
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    color: var(--text-quiet);
    font-size: 10px;
  }

  .waiting-skeleton {
    display: flex;
    flex-direction: column;
    gap: var(--space-5);
  }

  .waiting-skeleton i {
    display: block;
    height: 10px;
    width: 72%;
    border-radius: var(--radius-pill);
    background: var(--brand-light);
    opacity: .45;
    animation: f9-skeleton-pulse 1.2s ease-in-out infinite;
  }

  .waiting-skeleton i:nth-child(2) {
    width: 48%;
  }

  .waiting-skeleton-lg {
    width: 180px;
  }

  .compare-waiting-block {
    grid-column: 1 / -1;
    min-height: 110px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-10);
    border: 1px dashed var(--border-card);
    border-radius: var(--radius-xl);
    background: var(--surface-page);
    color: var(--text-quiet);
    font-size: 10px;
  }

  .compare-card-foot {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-top: var(--space-10);
    margin-top: var(--space-10);
    border-top: 1px solid #f0f2f6;
    color: #a0a8b3;
    font-size: 9px;
    gap: var(--space-6);
  }

  .compare-card-foot b {
    color: #707c8d;
    font-size: 11px;
    font-weight: 600;
  }

  @keyframes f9-skeleton-pulse {
    50% { opacity: .18; }
  }

  @media(prefers-reduced-motion:reduce) {
    .waiting-skeleton i {
      animation: none;
    }
  }

  @media(max-width:760px) {
    .compare-overview {
      flex-wrap: wrap;
    }
    .overview-title {
      width: 100%;
      max-width: 100%;
      margin: 3px 0 0 41px;
    }
    .overview-badges {
      width: 100%;
      margin-left: 41px;
    }
    .compare-grid {
      grid-template-columns: repeat(2,minmax(0,1fr));
      gap: var(--space-8);
    }
    .compare-card {
      padding: 13px;
      min-height: 151px;
    }
    .compare-category {
      font-size: 24px;
    }
  }

  @media(max-width:390px) {
    .compare-grid {
      grid-template-columns: 1fr 1fr;
    }
    .toolbar-request {
      display: none;
    }
  }
</style>
