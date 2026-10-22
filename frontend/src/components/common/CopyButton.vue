<script setup lang="ts">
/**
 * F16：复制结果。
 *
 * 安全上下文才调用 navigator.clipboard.writeText；
 * 非安全上下文/失败时降级为“可聚焦只读文本框 + 手动选择”提示。
 * execCommand 只在手动降级时机会性尝试，失败绝不报告“已复制”。
 */
import { nextTick, ref } from 'vue'
import { useSecureContext } from '../../composables/useSecureContext'
import { pushToast } from './toast'

const props = withDefaults(
  defineProps<{
    text: string
    label?: string
  }>(),
  { label: '复制结果' },
)

const { isSecureContext } = useSecureContext()
const manualOpen = ref(false)
const manualNote = ref('')
const copied = ref(false)
const manualBox = ref<HTMLTextAreaElement | null>(null)

function openManual(note: string): void {
  manualOpen.value = true
  manualNote.value = note
  copied.value = false
}

async function copy(): Promise<void> {
  if (!isSecureContext.value) {
    openManual('当前是 LAN HTTP 非安全上下文，浏览器不开放剪贴板 API。请手动选择下方文本复制。')
    return
  }
  const clipboard = typeof navigator !== 'undefined' ? navigator.clipboard : undefined
  if (!clipboard || typeof clipboard.writeText !== 'function') {
    openManual('当前浏览器未提供剪贴板 API，请手动选择下方文本复制。')
    return
  }
  try {
    await clipboard.writeText(props.text)
    copied.value = true
    manualOpen.value = false
    pushToast({ kind: 'success', message: '已复制结果', requestId: '' })
  } catch {
    openManual('自动复制失败（可能是权限或浏览器限制），请手动选择下方文本复制。')
  }
}

async function selectManual(): Promise<void> {
  await nextTick()
  const box = manualBox.value
  if (!box) return
  box.focus()
  box.select()
}

async function tryExecCommand(): Promise<void> {
  await selectManual()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  if (ok) {
    copied.value = true
    pushToast({ kind: 'success', message: '已通过兼容方式复制结果', requestId: '' })
  } else {
    copied.value = false
    manualNote.value = '自动复制失败，请保持文本框选中并手动复制（Ctrl+C / ⌘+C）。'
  }
}
</script>

<template>
  <div class="copy-control">
    <button type="button" class="copy-button" @click="copy">
      {{ copied ? '已复制' : label }}
    </button>
    <div v-if="manualOpen" class="copy-manual" role="note">
      <p>{{ manualNote }}</p>
      <textarea
        ref="manualBox"
        class="copy-manual-text"
        :value="text"
        readonly
        rows="3"
        aria-label="待复制的只读文本"
        @focus="selectManual"
      ></textarea>
      <div class="copy-manual-actions">
        <button type="button" class="copy-manual-button" @click="selectManual">全选文本</button>
        <button type="button" class="copy-manual-button" @click="tryExecCommand">尝试兼容复制</button>
        <span>失败时请手动按 Ctrl+C / ⌘+C</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.copy-control {
  display: inline-flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 8px;
}
.copy-button {
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: var(--surface-card);
  color: var(--text-secondary);
  padding: 5px 11px;
  font: inherit;
  font-size: 10px;
  cursor: pointer;
}
.copy-button:hover {
  border-color: var(--brand-light);
  color: var(--brand);
}
.copy-manual {
  max-width: 540px;
  padding: 9px 11px;
  border: 1px solid var(--warning-border);
  border-radius: var(--radius-md);
  background: var(--warning-soft);
  color: var(--warning-text);
  font-size: 10px;
  line-height: 1.55;
}
.copy-manual p {
  margin: 0 0 6px;
}
.copy-manual-text {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  border: 1px solid var(--warning-border);
  border-radius: var(--radius-sm);
  background: #fffdf7;
  color: var(--text-primary);
  padding: 7px 8px;
  font: inherit;
  font-size: 10px;
}
.copy-manual-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 6px;
}
.copy-manual-actions span {
  color: var(--warning-text);
  font-size: 9px;
}
.copy-manual-button {
  border: 1px solid var(--warning-border);
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--warning-text);
  padding: 4px 9px;
  font: inherit;
  font-size: 9px;
  cursor: pointer;
}
</style>
