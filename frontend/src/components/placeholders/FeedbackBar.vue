<script setup lang="ts">
/**
 * F8：纠错反馈 UI。
 *
 * 只提交“纠正类别”给 B4；成功文案固定为“反馈已提交，待审核”，
 * 不承诺模型变化。网络失败提供一次手动重试。
 */
import { computed, nextTick, onMounted, ref, useId, watch } from 'vue'
import { categoryNames } from '../shared/meta'
import { useFeedback } from '../../composables/useFeedback'
import { pushToast } from '../common/toast'

const props = defineProps<{
  title: string
  modelId: string
  predClass: string
  score?: number | null
}>()

const feedback = useFeedback()
const open = ref(false)
const corrected = ref('')
const retryUsed = ref(false)
const failedPayload = ref<{ corrected: string } | null>(null)
/** F14：每个 FeedbackBar 实例拥有唯一面板 id，避免多结果卡时 id 冲突。 */
const panelId = `feedback-panel-${useId()}`
const toggleRef = ref<HTMLButtonElement | null>(null)
const panelRef = ref<HTMLElement | null>(null)

const categories = Object.entries(categoryNames).map(([id, meta]) => ({ id, ...meta }))
const submitting = computed(() => feedback.submitting.value)
const currentLabel = computed(() => categoryNames[props.predClass]?.zh || props.predClass)
const total = computed(() => feedback.stats.value?.total ?? null)
const canSubmit = computed(
  () => Boolean(corrected.value) && corrected.value !== props.predClass && !submitting.value,
)
const canRetry = computed(
  () => Boolean(failedPayload.value) && !retryUsed.value && !submitting.value,
)

function resetState(): void {
  open.value = false
  corrected.value = ''
  retryUsed.value = false
  failedPayload.value = null
  feedback.error.value = ''
  feedback.duplicate.value = false
  feedback.successMessage.value = ''
}

watch(() => [props.title, props.modelId, props.predClass], resetState)

/** 面板内当前可聚焦元素（与 DOM 顺序一致）。 */
function panelFocusables(): HTMLElement[] {
  if (!panelRef.value) return []
  return Array.from(
    panelRef.value.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ),
  )
}

/** F14：打开时把焦点送入面板第一项（第一个非禁用类别按钮）。 */
function openPanel(): void {
  open.value = true
  void nextTick(() => {
    const first = panelFocusables()[0]
    ;(first ?? panelRef.value)?.focus()
  })
}

/** F14：关闭时清理状态，并把焦点交还给触发按钮。 */
function closePanel(restoreFocus = true): void {
  open.value = false
  feedback.error.value = ''
  feedback.duplicate.value = false
  feedback.successMessage.value = ''
  failedPayload.value = null
  if (restoreFocus) {
    void nextTick(() => toggleRef.value?.focus())
  }
}

function togglePanel(): void {
  if (open.value) closePanel()
  else openPanel()
}

/**
 * F14：面板是内联展开的纠错表单，使用轻量焦点循环（trigger ↔ 面板内控件），
 * 保证 Tab 不会跑出上下文；Escape 关闭并归还焦点。
 */
function onKeydown(event: KeyboardEvent): void {
  if (!open.value) return
  if (event.key === 'Escape') {
    event.preventDefault()
    event.stopPropagation()
    closePanel()
    return
  }
  if (event.key !== 'Tab') return
  const list = [toggleRef.value, ...panelFocusables()].filter(
    (item): item is HTMLElement => Boolean(item),
  )
  if (!list.length) return
  const first = list[0]!
  const last = list[list.length - 1]!
  const active = document.activeElement as HTMLElement | null
  if (event.shiftKey) {
    if (!active || active === first || !list.includes(active)) {
      event.preventDefault()
      last.focus()
    }
    return
  }
  if (active === last) {
    event.preventDefault()
    first.focus()
  }
}

function selectCategory(id: string): void {
  if (id === props.predClass) return
  corrected.value = id
  feedback.error.value = ''
  feedback.duplicate.value = false
}

async function runSubmit(correctedClass: string): Promise<void> {
  const status = await feedback.submit({
    title: props.title,
    model_id: props.modelId,
    pred_class: props.predClass,
    corrected_class: correctedClass,
    score: props.score ?? null,
  })
  if (status === 'ok') {
    pushToast({
      kind: 'success',
      message: feedback.successMessage.value || '反馈已提交，待审核',
      requestId: '',
    })
    failedPayload.value = null
    retryUsed.value = false
    corrected.value = ''
    closePanel()
    return
  }
  if (status === 'duplicate') {
    pushToast({ kind: 'info', message: '已记录过，待审核', requestId: '' })
    failedPayload.value = null
    return
  }
  failedPayload.value = { corrected: correctedClass }
}

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  await runSubmit(corrected.value)
}

async function retry(): Promise<void> {
  if (!canRetry.value || !failedPayload.value) return
  retryUsed.value = true
  await runSubmit(failedPayload.value.corrected)
}

onMounted(() => {
  void feedback.loadStats()
})
</script>

<template>
  <section class="feedback-bar" aria-label="纠错反馈" @keydown="onKeydown">
    <div class="feedback-head">
      <div>
        <b>这个判断不对？</b>
        <span>可以提交纠正类别，反馈会进入待审核队列。</span>
      </div>
      <div class="feedback-head-right">
        <span v-if="total !== null" class="feedback-count">已记录 {{ total }} 条待审核</span>
        <button
          ref="toggleRef"
          type="button"
          class="feedback-toggle"
          :aria-expanded="open"
          :aria-controls="panelId"
          @click="togglePanel"
        >
          {{ open ? '收起' : '判错了' }}
        </button>
      </div>
    </div>

    <div
      v-if="open"
      :id="panelId"
      ref="panelRef"
      class="feedback-panel"
      role="group"
      aria-label="纠错表单"
      tabindex="-1"
      data-feedback-panel
    >
      <p class="feedback-prompt">
        当前模型判断：<b>{{ currentLabel }}</b>。请选择正确类别（不会改变本次预测结果）。
      </p>
      <div class="feedback-categories" role="group" aria-label="正确类别">
        <button
          v-for="category in categories"
          :key="category.id"
          type="button"
          class="feedback-chip"
          :class="[`cat-${category.color}`, { selected: corrected === category.id, current: category.id === predClass }]"
          :aria-pressed="corrected === category.id"
          :disabled="category.id === predClass"
          @click="selectCategory(category.id)"
        >
          {{ category.zh }}
          <small v-if="category.id === predClass">当前判断</small>
        </button>
      </div>

      <div class="feedback-actions">
        <button type="button" class="feedback-submit" :disabled="!canSubmit" @click="submit">
          {{ submitting ? '提交中…' : '提交反馈' }}
        </button>
        <button type="button" class="feedback-cancel" @click="closePanel()">取消</button>
        <span class="feedback-note">提交只表示“已记录，待审核”，不会自动改变模型。</span>
      </div>

      <div v-if="feedback.successMessage.value" class="feedback-status success" role="status">
        {{ feedback.successMessage.value }}（已记录 {{ total }} 条）
      </div>
      <div v-else-if="feedback.duplicate.value" class="feedback-status info" role="status">
        已记录过，待审核
      </div>
      <div v-else-if="feedback.error.value" class="feedback-status error" role="alert">
        {{ feedback.error.value }}
        <button
          v-if="canRetry"
          type="button"
          class="feedback-retry"
          @click="retry"
        >
          重试一次
        </button>
        <small v-else-if="retryUsed">重试仍未成功，请稍后再试。</small>
      </div>
    </div>
  </section>
</template>

<style scoped>
.feedback-bar {
  margin-top: 14px;
  padding: 14px 18px;
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
}
.feedback-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.feedback-head b {
  display: block;
  color: var(--text-primary);
  font-size: 11px;
  margin-bottom: 2px;
}
.feedback-head span {
  color: var(--text-quiet);
  font-size: 10px;
}
.feedback-head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: none;
}
.feedback-count {
  color: var(--text-quiet);
  font-size: 9px;
}
.feedback-toggle,
.feedback-submit,
.feedback-cancel,
.feedback-retry {
  border-radius: var(--radius-pill);
  font: inherit;
  cursor: pointer;
}
.feedback-toggle {
  border: 1px solid var(--border-strong);
  background: var(--surface-page);
  color: var(--text-secondary);
  padding: 6px 12px;
  font-size: 10px;
}
.feedback-toggle:hover {
  border-color: var(--brand-light);
  color: var(--brand);
}
.feedback-panel {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border-card);
}
.feedback-prompt {
  margin: 0 0 9px;
  color: var(--text-quiet);
  font-size: 10px;
}
.feedback-prompt b {
  color: var(--text-primary);
}
.feedback-categories {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.feedback-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 7px 11px;
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: var(--surface-page);
  color: var(--text-secondary);
  font: inherit;
  font-size: 10px;
  cursor: pointer;
}
.feedback-chip small {
  font-size: 8px;
  color: var(--text-faint);
}
.feedback-chip:hover:not(:disabled) {
  border-color: var(--brand-light);
  color: var(--brand);
}
.feedback-chip.selected {
  border-color: currentColor;
  background: var(--brand-tint);
  color: var(--brand);
}
.feedback-chip.current,
.feedback-chip:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.feedback-actions {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
  margin-top: 12px;
}
.feedback-submit {
  border: 0;
  background: var(--brand);
  color: #fff;
  padding: 8px 15px;
  font-size: 10px;
  font-weight: 600;
}
.feedback-submit:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.feedback-cancel {
  border: 1px solid var(--border-control);
  background: transparent;
  color: var(--text-quiet);
  padding: 7px 12px;
  font-size: 10px;
}
.feedback-note {
  color: var(--text-faint);
  font-size: 9px;
}
.feedback-status {
  margin-top: 10px;
  padding: 8px 10px;
  border-radius: var(--radius-sm);
  font-size: 10px;
  line-height: 1.5;
}
.feedback-status.success {
  background: var(--success-soft);
  color: var(--success-text);
}
.feedback-status.info {
  background: var(--surface-page);
  color: var(--text-quiet);
}
.feedback-status.error {
  background: var(--danger-soft);
  color: var(--danger-text);
}
.feedback-retry {
  margin-left: 8px;
  padding: 3px 8px;
  border: 1px solid currentColor;
  background: transparent;
  color: inherit;
  font-size: 9px;
}
.feedback-status small {
  display: block;
  margin-top: 4px;
}
@media (max-width: 760px) {
  .feedback-head {
    align-items: flex-start;
    flex-direction: column;
  }
  .feedback-head-right {
    width: 100%;
    justify-content: space-between;
  }
}
</style>
