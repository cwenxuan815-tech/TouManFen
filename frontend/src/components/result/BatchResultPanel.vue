<script setup lang="ts">
/** 阶段 2B：多条标题的进度、逐项结果、失败重试与取消。 */
import { computed } from 'vue'
import { categoryInfo } from '../shared/meta'
import type { BatchPhase, BatchRowState } from '../../composables'

const props = defineProps<{
  rows: BatchRowState[]
  phase: BatchPhase
  progressDone: number
  progressTotal: number
  okCount: number
  errorCount: number
  batchId: string
  requestId: string
  lastError: string
  modelLabel: string
}>()

const emit = defineEmits<{
  retry: []
  cancel: []
}>()

const running = computed(() => props.phase === 'running')
const skippedCount = computed(
  () => props.rows.filter((row) => row.status === 'skipped').length,
)
const canRetry = computed(
  () => !running.value && props.rows.some((row) => row.status === 'error'),
)
const progressPercent = computed(() => {
  if (props.progressTotal <= 0) return 0
  return Math.min(100, Math.round((props.progressDone / props.progressTotal) * 100))
})

const heading = computed(() => {
  if (props.phase === 'running') {
    return `正在逐条分类… ${props.progressDone} / ${props.progressTotal}`
  }
  if (props.phase === 'cancelled') {
    return '已取消，保留已完成结果。'
  }
  if (props.phase === 'error') {
    return '批次没有完成。'
  }
  if (props.phase === 'partial') {
    if (props.errorCount > 0) {
      return `部分完成：${props.okCount} 成功 / ${props.errorCount} 失败。`
    }
    return `部分完成：${props.okCount} 成功 · ${skippedCount.value} 条未提交。`
  }
  if (props.phase === 'done') {
    return `${props.okCount} 条标题分类完成。`
  }
  return '多条标题分类'
})

function rowStatusText(status: BatchRowState['status']): string {
  if (status === 'ok') return '完成'
  if (status === 'error') return '失败'
  if (status === 'cancelled') return '已取消'
  if (status === 'skipped') return '未提交'
  return '等待中'
}

function rowCategory(row: BatchRowState): string {
  return row.predClass ? categoryInfo(row.predClass).zh : '—'
}

function rowError(row: BatchRowState): string {
  if (row.status !== 'error' && row.status !== 'cancelled' && row.status !== 'skipped') {
    return ''
  }
  const code = row.errorCode ? `[${row.errorCode}] ` : ''
  return `${code}${row.errorMessage || '未完成'}`
}
</script>

<template>
  <section
    class="batch-result"
    data-batch-result
    :data-batch-phase="phase"
    :data-batch-id="batchId"
    :data-request-id="requestId"
    aria-labelledby="batch-result-title"
  >
    <div class="batch-result-head">
      <div>
        <div class="eyebrow"><span class="eyebrow-line"></span> BATCH CLASSIFICATION</div>
        <h3 id="batch-result-title">{{ heading }}</h3>
        <p class="batch-result-meta">
          模型：{{ modelLabel }}
          <span v-if="batchId"> · BATCH · {{ batchId }}</span>
          <span v-if="requestId"> · REQUEST · {{ requestId.slice(0, 8) }}</span>
        </p>
      </div>
      <div class="batch-result-actions">
        <button
          v-if="running"
          type="button"
          class="batch-action batch-cancel"
          data-batch-cancel
          @click="emit('cancel')"
        >
          取消后续分片
        </button>
        <button
          v-if="canRetry"
          type="button"
          class="batch-action batch-retry"
          data-batch-retry
          @click="emit('retry')"
        >
          重试失败项
        </button>
      </div>
    </div>

    <div
      class="batch-progress"
      role="status"
      aria-live="polite"
      data-batch-progress
      :data-progress-done="progressDone"
      :data-progress-total="progressTotal"
    >
      <div class="batch-progress-track" aria-hidden="true">
        <i :style="{ width: `${progressPercent}%` }"></i>
      </div>
      <span>已处理 {{ progressDone }} / {{ progressTotal }} 条</span>
      <span v-if="okCount || errorCount || skippedCount" data-batch-summary>
        {{ okCount }} 成功 · {{ errorCount }} 失败
        <template v-if="skippedCount"> · {{ skippedCount }} 未提交</template>
      </span>
    </div>

    <p v-if="lastError" class="batch-global-error" role="alert" data-batch-error>
      {{ lastError }}
    </p>
    <p v-if="phase === 'cancelled'" class="batch-cancel-note" role="status">
      已停止发送尚未开始的分片；已发给服务端的同步推理不保证立即中止。
    </p>

    <ul class="batch-result-list" aria-label="多条分类结果">
      <li
        v-for="row in rows"
        :key="row.itemId"
        class="batch-result-row"
        :class="`is-${row.status}`"
        data-batch-result-row
        :data-item-id="row.itemId"
        :data-status="row.status"
        :data-line-no="row.lineNo"
        :data-record-no="row.recordNo ?? ''"
      >
        <span
          class="batch-result-line"
          :title="row.recordNo !== undefined ? `记录 ${row.recordNo} · 第 ${row.lineNo} 行` : `第 ${row.lineNo} 行`"
        >{{ row.lineNo }}</span>
        <div class="batch-result-main">
          <span class="batch-result-title" :title="row.title">{{ row.title }}</span>
          <span v-if="rowError(row)" class="batch-result-error" role="alert">
            {{ rowError(row) }}
          </span>
        </div>
        <span class="batch-result-status">{{ rowStatusText(row.status) }}</span>
        <span class="batch-result-category">{{ rowCategory(row) }}</span>
        <span class="batch-result-latency">
          {{ row.status === 'ok' ? `${row.latencyMs} ms` : '—' }}
        </span>
        <span v-if="row.dataSource" class="batch-result-source">{{ row.dataSource }}</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.batch-result {
  margin-top: 45px;
  padding: var(--space-24);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-5xl);
  background: var(--surface-card);
  box-shadow: 0 16px 48px #2a3f6010;
}

.batch-result-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: var(--space-14);
}

.batch-result-head h3 {
  margin: var(--space-10) 0 0;
  font-size: 24px;
  letter-spacing: -0.04em;
}

.batch-result-meta {
  margin: var(--space-8) 0 0;
  color: var(--text-quiet);
  font-size: 10px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.batch-result-actions {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-8);
  justify-content: flex-end;
}

.batch-action {
  min-height: 32px;
  padding: 0 var(--space-12);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: var(--surface-card);
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}

.batch-action:hover {
  border-color: var(--brand-light);
  color: var(--brand);
}

.batch-cancel {
  color: var(--danger-text);
}

.batch-progress {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-10);
  margin-top: var(--space-16);
  color: var(--text-secondary);
  font-size: 11px;
}

.batch-progress-track {
  flex: 1 1 180px;
  height: 6px;
  overflow: hidden;
  border-radius: var(--radius-pill);
  background: var(--surface-page);
}

.batch-progress-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--brand);
  transition: width var(--duration-fast);
}

.batch-global-error {
  margin: var(--space-12) 0 0;
  padding: var(--space-10) var(--space-12);
  border: 1px solid var(--danger-border);
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger-text);
  font-size: 11px;
  line-height: 1.6;
}

.batch-cancel-note {
  margin: var(--space-10) 0 0;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.6;
}

.batch-result-list {
  margin: var(--space-16) 0 0;
  padding: 0;
  border-top: 1px solid var(--border);
  list-style: none;
}

.batch-result-row {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) 64px 72px 72px auto;
  gap: var(--space-8);
  align-items: center;
  padding: var(--space-10) 0;
  border-bottom: 1px solid var(--border);
  font-size: 11px;
}

.batch-result-row:last-child {
  border-bottom: 0;
}

.batch-result-row.is-error {
  background: color-mix(in srgb, var(--danger-soft) 45%, transparent);
}

.batch-result-line {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.batch-result-main {
  min-width: 0;
}

.batch-result-title {
  display: block;
  overflow: hidden;
  color: var(--text-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.batch-result-error {
  display: block;
  margin-top: 2px;
  color: var(--danger-text);
  font-size: 10px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.batch-result-status {
  color: var(--text-quiet);
  font-weight: 700;
}

.batch-result-row.is-ok .batch-result-status {
  color: var(--success-text);
}

.batch-result-row.is-error .batch-result-status {
  color: var(--danger-text);
}

.batch-result-category {
  color: var(--text-primary);
  font-weight: 700;
}

.batch-result-latency,
.batch-result-source {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.batch-result-source {
  justify-self: end;
}

@media (max-width: 760px) {
  .batch-result {
    padding: var(--space-17) var(--space-15);
    border-radius: 19px;
  }

  .batch-result-head {
    display: block;
  }

  .batch-result-actions {
    justify-content: flex-start;
    margin-top: var(--space-10);
  }

  .batch-result-row {
    grid-template-columns: 28px minmax(0, 1fr) auto;
    row-gap: 3px;
  }

  .batch-result-category,
  .batch-result-latency,
  .batch-result-source {
    grid-column: 2 / -1;
  }

  .batch-result-status {
    grid-column: 3;
    grid-row: 1;
    justify-self: end;
  }
}
</style>
