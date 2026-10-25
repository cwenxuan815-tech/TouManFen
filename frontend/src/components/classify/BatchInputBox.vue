<script setup lang="ts">
/** 阶段 2B：多条标题输入（一行一条；不把整段 200 字上限套在 textarea 上）。 */
import { computed } from 'vue'
import {
  BATCH_ITEM_MAX_CHARS,
  BATCH_MAX_ITEMS,
  BATCH_MAX_TOTAL_CHARS,
  type ParsedBatchItem,
} from '../../utils/batchTitles'

const props = defineProps<{
  modelValue: string
  items: ParsedBatchItem[]
  totalChars: number
  emptyLineCount: number
  duplicateExtraCount: number
  duplicateGroupCount: number
  tooLongCount: number
  disabled?: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const overItemLimit = computed(() => props.items.length > BATCH_MAX_ITEMS)
const overTotalChars = computed(() => props.totalChars > BATCH_MAX_TOTAL_CHARS)

function onInput(event: Event): void {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
}
</script>

<template>
  <div class="batch-input" data-batch-input-wrap>
    <label class="field-label" for="batch-news-title">
      <span>多条新闻标题</span>
      <span class="field-hint">每行一条 · 空行忽略 · 重复保留</span>
    </label>
    <div class="batch-textarea-wrap">
      <textarea
        id="batch-news-title"
        :value="modelValue"
        data-batch-input
        rows="8"
        placeholder="每行输入一条新闻标题，例如：&#10;北京二手房成交量创年内新高&#10;Zacks调升苹果目标股价至200美元"
        :disabled="disabled"
        @input="onInput"
      ></textarea>
      <div class="batch-textarea-footer">
        <span>{{ items.length }} 条 · {{ totalChars }} / {{ BATCH_MAX_TOTAL_CHARS }} 字符</span>
        <span v-if="emptyLineCount">已忽略 {{ emptyLineCount }} 个空行</span>
        <span>单条上限 {{ BATCH_ITEM_MAX_CHARS }} 字符</span>
      </div>
    </div>

    <div class="batch-meta-row" aria-live="polite">
      <span v-if="items.length" data-batch-count>{{ items.length }} 条待分类</span>
      <span v-else>还没有有效标题。</span>
      <span v-if="duplicateExtraCount" class="batch-warn" data-batch-duplicate>
        重复标题 {{ duplicateExtraCount }} 条（{{ duplicateGroupCount }} 组），将分别分类
      </span>
      <span v-if="overItemLimit" class="batch-error" data-batch-too-many role="alert">
        最多 {{ BATCH_MAX_ITEMS }} 条，当前 {{ items.length }} 条
      </span>
      <span v-if="overTotalChars" class="batch-error" data-batch-total-error role="alert">
        总字符超过 {{ BATCH_MAX_TOTAL_CHARS }}
      </span>
    </div>

    <p v-if="tooLongCount" class="batch-error batch-alert" data-batch-too-long role="alert">
      有 {{ tooLongCount }} 条超过 {{ BATCH_ITEM_MAX_CHARS }} 个字符，请修改对应行后再提交。
    </p>

    <ul v-if="items.length" class="batch-preview" data-batch-preview>
      <li
        v-for="item in items"
        :key="item.itemId"
        class="batch-preview-row"
        :class="{ 'is-too-long': item.tooLong }"
        data-batch-preview-row
        :data-item-id="item.itemId"
        :data-line-no="item.lineNo"
      >
        <span class="batch-line-no">{{ item.lineNo }}</span>
        <span class="batch-preview-title" :title="item.title">{{ item.title }}</span>
        <span
          class="batch-char-count"
          :class="{ 'is-over': item.tooLong }"
          data-batch-char-count
        >{{ item.charCount }}/{{ BATCH_ITEM_MAX_CHARS }}</span>
        <span v-if="item.duplicateCount > 1" class="batch-duplicate-tag">
          重复 ×{{ item.duplicateCount }}
        </span>
      </li>
    </ul>

    <p v-else class="batch-empty" data-batch-empty>
      每行输入一条标题；空行和纯空白行不会被提交。
    </p>
  </div>
</template>

<style scoped>
.batch-input {
  min-width: 0;
}

.batch-textarea-wrap {
  border: 1px solid var(--border-control);
  border-radius: var(--radius-lg);
  background: var(--surface-page);
  transition: border var(--duration-fast), box-shadow var(--duration-fast);
}

.batch-textarea-wrap:focus-within {
  border-color: var(--brand-light);
  box-shadow: 0 0 0 4px var(--brand-tint);
}

.batch-textarea-wrap textarea {
  display: block;
  width: 100%;
  min-height: 172px;
  padding: var(--space-17) 18px var(--space-9);
  border: 0;
  outline: 0;
  resize: vertical;
  background: transparent;
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.8;
}

.batch-textarea-wrap textarea::placeholder {
  color: var(--text-subtle);
}

.batch-textarea-wrap textarea:disabled {
  cursor: not-allowed;
  opacity: 0.72;
}

.batch-textarea-footer {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: var(--space-6);
  padding: var(--space-8) var(--space-15) var(--space-12);
  color: var(--text-subtle);
  font-size: 10px;
}

.batch-meta-row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-10);
  align-items: center;
  margin-top: var(--space-10);
  color: var(--text-secondary);
  font-size: 11px;
}

.batch-warn {
  color: var(--warning-text);
}

.batch-error {
  color: var(--danger-text);
}

.batch-alert {
  margin: var(--space-10) 0 0;
  font-size: 11px;
  line-height: 1.6;
}

.batch-preview {
  max-height: 270px;
  overflow: auto;
  margin: var(--space-12) 0 0;
  padding: 0;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-md);
  background: var(--surface-card);
  list-style: none;
}

.batch-preview-row {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) auto auto;
  gap: var(--space-8);
  align-items: center;
  padding: var(--space-8) var(--space-10);
  border-bottom: 1px solid var(--border);
  font-size: 11px;
}

.batch-preview-row:last-child {
  border-bottom: 0;
}

.batch-preview-row.is-too-long {
  background: var(--danger-soft);
}

.batch-line-no {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.batch-preview-title {
  overflow: hidden;
  color: var(--text-secondary);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.batch-char-count {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.batch-char-count.is-over {
  color: var(--danger-text);
  font-weight: 700;
}

.batch-duplicate-tag {
  padding: 1px var(--space-6);
  border-radius: var(--radius-pill);
  background: var(--warning-soft);
  color: var(--warning-text);
  font-size: 9px;
  white-space: nowrap;
}

.batch-empty {
  margin: var(--space-12) 0 0;
  color: var(--text-faint);
  font-size: 11px;
  line-height: 1.7;
}

@media (max-width: 760px) {
  .batch-textarea-wrap textarea {
    min-height: 150px;
    font-size: 13px;
  }

  .batch-preview-row {
    grid-template-columns: 28px minmax(0, 1fr) auto;
  }

  .batch-duplicate-tag {
    grid-column: 2 / -1;
    justify-self: start;
  }
}
</style>
