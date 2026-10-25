<script setup lang="ts">
/** 阶段 3B：文件导入后的只读标题列表；清除后恢复手动文本草稿。 */
import type { ImportedBatchItem, ImportedFileMeta } from '../../composables'
import { BATCH_ITEM_MAX_CHARS } from '../../utils/batchTitles'

defineProps<{
  items: ImportedBatchItem[]
  meta: ImportedFileMeta | null
  disabled?: boolean
}>()

const emit = defineEmits<{
  clear: []
}>()

function itemSource(item: ImportedBatchItem): string {
  if (item.recordNo !== undefined) {
    return `记录 ${item.recordNo} · 第 ${item.lineNo} 行`
  }
  return `第 ${item.lineNo} 行`
}

function encodingLabel(meta: ImportedFileMeta | null): string {
  if (!meta) return '未知编码'
  return `${meta.encoding === 'utf-8' ? 'UTF-8' : 'GB18030'}${meta.hadBom ? ' BOM' : ''}`
}
</script>

<template>
  <section class="imported-titles" data-imported-titles aria-labelledby="imported-titles-heading">
    <div class="imported-head">
      <div>
        <div class="imported-kicker">FILE IMPORT · READ ONLY</div>
        <h4 id="imported-titles-heading">已导入 {{ items.length }} 条标题</h4>
        <p v-if="meta" class="imported-meta" data-imported-meta>
          {{ meta.fileName }} · {{ meta.format === 'csv' ? 'CSV' : 'TXT' }} · {{ encodingLabel(meta) }}
          <span v-if="meta.skippedErrorCount"> · 已跳过 {{ meta.skippedErrorCount }} 条坏记录</span>
        </p>
      </div>
      <button
        type="button"
        class="imported-clear"
        data-imported-clear
        :disabled="disabled"
        @click="emit('clear')"
      >
        清除导入，恢复手动草稿
      </button>
    </div>

    <p v-if="meta" class="imported-stats">
      <span v-if="meta.format === 'txt'">总行数 {{ meta.recordCount + meta.blankRecordCount }}</span>
      <span v-else>数据记录 {{ meta.recordCount }}</span>
      <span>空白 {{ meta.blankRecordCount }}</span>
      <span v-if="meta.format === 'csv'">空标题 {{ meta.emptyTitleCount }}</span>
      <span v-if="meta.duplicateExtraCount">重复 {{ meta.duplicateExtraCount }} 条 / {{ meta.duplicateGroupCount }} 组</span>
      <span>只读；修改标题请清除导入后返回文本模式</span>
    </p>

    <ul class="imported-list">
      <li
        v-for="item in items"
        :key="item.itemId"
        class="imported-item"
        data-imported-item
        :data-item-id="item.itemId"
        :data-line-no="item.lineNo"
        :data-record-no="item.recordNo ?? ''"
      >
        <span class="imported-source">{{ itemSource(item) }}</span>
        <span class="imported-title" :title="item.title">{{ item.title }}</span>
        <span class="imported-length">{{ item.charCount }}/{{ BATCH_ITEM_MAX_CHARS }}</span>
        <span class="imported-duplicate">{{ item.duplicateCount > 1 ? `重复 ×${item.duplicateCount}` : '' }}</span>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.imported-titles {
  margin-top: var(--space-12);
  padding: var(--space-14);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
}

.imported-head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-10);
}

.imported-kicker {
  color: var(--text-faint);
  font-size: 9px;
  letter-spacing: 0.08em;
}

.imported-titles h4 {
  margin: var(--space-4) 0 0;
  color: var(--text-primary);
  font-size: 16px;
}

.imported-meta {
  margin: var(--space-4) 0 0;
  color: var(--text-secondary);
  font-size: 10px;
  overflow-wrap: anywhere;
}

.imported-clear {
  min-height: 32px;
  padding: 0 var(--space-12);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  font-size: 10px;
  cursor: pointer;
}

.imported-clear:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.imported-stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6) var(--space-10);
  margin: var(--space-10) 0 0;
  color: var(--text-secondary);
  font-size: 10px;
}

.imported-list {
  max-height: 270px;
  margin: var(--space-10) 0 0;
  padding: 0;
  overflow: auto;
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  list-style: none;
}

.imported-item {
  display: grid;
  grid-template-columns: minmax(108px, 0.9fr) minmax(0, 2fr) 52px 62px;
  gap: var(--space-8);
  align-items: center;
  padding: var(--space-6) var(--space-8);
  border-bottom: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 10px;
}

.imported-item:last-child {
  border-bottom: 0;
}

.imported-source {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.imported-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.imported-length,
.imported-duplicate {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.imported-duplicate {
  color: var(--warning-text);
}

@media (max-width: 760px) {
  .imported-titles {
    padding: var(--space-10);
  }

  .imported-item {
    grid-template-columns: minmax(84px, 0.9fr) minmax(0, 1.8fr) 46px;
  }

  .imported-duplicate {
    display: none;
  }
}
</style>
