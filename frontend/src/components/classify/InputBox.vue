<script setup lang="ts">
withDefaults(
  defineProps<{
    modelValue: string
    examples: readonly string[]
    maxChars?: number
  }>(),
  { maxChars: 200 },
)

const emit = defineEmits<{
  'update:modelValue': [value: string]
  'select-example': [value: string]
  'composition-start': []
  'composition-end': [value: string]
}>()

function onInput(event: Event): void {
  const target = event.target as HTMLTextAreaElement
  emit('update:modelValue', target.value)
}

function onCompositionEnd(event: CompositionEvent): void {
  const target = event.target as HTMLTextAreaElement
  emit('composition-end', target.value)
}
</script>

<template>
  <label class="field-label" for="news-title"><span>新闻标题</span><span class="field-hint">粘贴或输入一条中文标题</span></label>
  <div class="textarea-wrap" :class="{ 'textarea-error': modelValue.length > maxChars }">
    <textarea
      id="news-title"
      :value="modelValue"
      :maxlength="maxChars"
      placeholder="例如：北京二手房成交量创年内新高"
      rows="4"
      @input="onInput"
      @compositionstart="emit('composition-start')"
      @compositionend="onCompositionEnd"
    ></textarea>
    <div class="textarea-footer">
      <span class="input-tip"><span class="sparkle">✦</span> 让模型读懂标题背后的主题</span>
      <span class="char-count" :class="{ 'count-warn': modelValue.length > maxChars }">{{ modelValue.length }} <i>/</i> {{ maxChars }}</span>
    </div>
  </div>

  <div class="examples-row">
    <span class="examples-label">试试这些</span>
    <button v-for="example in examples" :key="example" type="button" class="example-chip" @click="emit('select-example', example)">{{ example }}</button>
  </div>
</template>

<style scoped>
  .textarea-wrap {
    border: 1px solid var(--border-control);
    border-radius: var(--radius-lg);
    background: #fbfcfe;
    transition: border var(--duration-fast),box-shadow var(--duration-fast);
  }

  .textarea-wrap:focus-within {
    border-color: var(--brand-light);
    box-shadow: 0 0 0 4px var(--brand-tint);
  }

  .textarea-wrap.textarea-error {
    border-color: #ed817f;
  }

  .textarea-wrap textarea {
    width: 100%;
    resize: vertical;
    min-height: 106px;
    padding: var(--space-17) 18px var(--space-9);
    border: 0;
    outline: 0;
    background: transparent;
    color: #222b39;
    font-size: 16px;
    line-height: 1.8;
  }

  .textarea-wrap textarea::placeholder {
    color: #b1b8c3;
  }

  .textarea-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-8) var(--space-15) var(--space-12);
    color: var(--text-subtle);
    font-size: 10px;
  }

  .input-tip {
    display: flex;
    align-items: center;
    gap: var(--space-6);
  }

  .sparkle {
    color: #5589f0;
  }

  .char-count {
    font-variant-numeric: tabular-nums;
  }

  .char-count i {
    font-style: normal;
    color: #c5cbd4;
    padding: 0 2px;
  }

  .char-count.count-warn {
    color: #d9534f;
  }

  .examples-row {
    display: flex;
    align-items: center;
    gap: var(--space-7);
    flex-wrap: wrap;
    margin: 13px 0 20px;
  }

  .examples-label {
    font-size: 10px;
    color: #9aa2ae;
    margin-right: 2px;
  }

  .example-chip {
    border: 1px solid #e9edf3;
    background: var(--surface-card);
    border-radius: var(--radius-pill);
    padding: var(--space-7) var(--space-10);
    color: #737e8f;
    font-size: 10px;
    transition: all var(--duration-fast);
  }

  .example-chip:hover {
    border-color: #a9c7ff;
    color: var(--brand);
    background: #f7faff;
  }

  @media(max-width:760px) {
    .textarea-wrap textarea {
      min-height: 100px;
      font-size: 13px;
    }
    .examples-row {
      gap: var(--space-6);
    }
    .example-chip {
      font-size: 9px;
      padding: var(--space-6) var(--space-8);
    }
  }
</style>
