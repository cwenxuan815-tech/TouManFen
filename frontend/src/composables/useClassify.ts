/**
 * F3/F5：分类/对比状态与请求编排。
 *
 * F3 迁移状态与请求；F5 增加实时输入（250ms debounce、abort、单调序号、
 * IME composition、长度边界、静默失败）。不实现 F6 的概率/Top-k UI。
 */
import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import {
  ApiClientError,
  classify,
  classifyRealtime,
  compare,
  friendlyErrorMessage,
  isAbortError,
  supportsCompareStream,
} from '../api'
import { MAX_TITLE_CHARS } from '../components/shared/meta'
import { setObservedSource } from './useDemoState'
import type { ClassifyResponse, CompareResponse, ModelInfo } from '../types'

export type ClassifyMode = 'single' | 'compare'
export type RealtimeStatus = 'idle' | 'loading' | 'success'

/**
 * F9：流式对比的一次提交请求。
 * nonce 变化即新提交；ResultStage 监听它并启动/替换 SSE 连接。
 */
export interface CompareStreamRequest {
  title: string
  models: string[]
  nonce: number
}

/** F5 实时输入参数（与清单一致）。 */
export const REALTIME_DEBOUNCE_MS = 250
export const REALTIME_MIN_CHARS = 4
export const REALTIME_MODEL_ID = 'fasttext'

export interface UseClassifyOptions {
  models: Ref<ModelInfo[]>
  defaultModel: Ref<string>
  modelsError: Ref<string>
  maxTitleChars?: number
  /** access 启用时 EventSource 不能带 ticket；false 时 compare 走 POST fallback。 */
  compareStreamEnabled?: Ref<boolean>
}

export function useClassify(options: UseClassifyOptions) {
  const { models, defaultModel, modelsError } = options
  const maxTitleChars = options.maxTitleChars ?? MAX_TITLE_CHARS

  const title = ref('')
  const mode = ref<ClassifyMode>('single')
  const selectedModel = ref('')
  const selectedModels = ref<string[]>([])
  const includeLlm = ref(false)
  const submitting = ref(false)
  const pageError = ref('')
  const result = ref<ClassifyResponse | CompareResponse | null>(null)
  const lastTitle = ref('')
  const lastRequestId = ref('')
  const realtimeResult = ref<ClassifyResponse | null>(null)
  const realtimeStatus = ref<RealtimeStatus>('idle')
  const realtimeTitle = ref('')
  const composing = ref(false)
  let activeController: AbortController | null = null
  let realtimeController: AbortController | null = null
  let realtimeTimer: ReturnType<typeof setTimeout> | null = null
  let realtimeSeq = 0
  /** F9：流式对比请求（组件层 ResultStage 负责真正的 EventSource 生命周期）。 */
  const compareStreamRequest = ref<CompareStreamRequest | null>(null)
  let compareStreamNonce = 0
  const compareStreaming = computed(
    () => mode.value === 'compare' && compareStreamRequest.value !== null,
  )

  const trimmedTitle = computed(() => title.value.trim())
  const selectedModelNames = computed(() =>
    selectedModels.value
      .map((id) => models.value.find((model) => model.model_id === id)?.display_name)
      .filter((name): name is string => Boolean(name)),
  )
  const canSubmit = computed(() => {
    if (!trimmedTitle.value || title.value.length > maxTitleChars || submitting.value || modelsError.value) return false
    if (mode.value === 'single') return Boolean(selectedModel.value) && (selectedModel.value !== 'llm' || includeLlm.value)
    return selectedModels.value.length > 0 && (includeLlm.value || !selectedModels.value.includes('llm'))
  })
  const singleResult = computed(() =>
    result.value && 'pred_class' in result.value ? result.value : null,
  )
  const compareResult = computed(() =>
    result.value && 'results' in result.value ? result.value : null,
  )
  const pageHeading = computed(() =>
    mode.value === 'single' ? '把标题，交给模型理解。' : '让不同模型，给出各自的判断。',
  )

  function modelSelectionDefaults(): string[] {
    return models.value
      .filter((item) => item.enabled && item.model_id !== 'llm')
      .map((item) => item.model_id)
  }

  function initModelSelection(): void {
    if (!models.value.length) return
    if (defaultModel.value === 'llm') {
      selectedModel.value = modelSelectionDefaults()[0] || ''
    } else {
      selectedModel.value = defaultModel.value
    }
    selectedModels.value = modelSelectionDefaults()
  }

  watch([models, defaultModel], () => initModelSelection(), { immediate: true })
  watch(mode, (nextMode) => {
    pageError.value = ''
    result.value = null
    // F9：切换模式立即作废流式请求（ResultStage 收到 null 后 stop 旧连接）。
    compareStreamRequest.value = null
    if (nextMode === 'compare' && !selectedModels.value.length) {
      selectedModels.value = modelSelectionDefaults()
    }
  })

  function setMode(nextMode: ClassifyMode): void {
    if (nextMode === mode.value) {
      pageError.value = ''
      result.value = null
      compareStreamRequest.value = null
      return
    }
    mode.value = nextMode
  }

  function clearCompareStreamRequest(): void {
    compareStreamRequest.value = null
  }

  /**
   * F9：对比模式提交入口。返回 true 表示已交给流式面板（ResultStage 启动 SSE）；
   * 返回 false 表示当前环境不支持 EventSource，调用方应回退到旧 `submit()` POST 路径。
   */
  function submitCompareStream(): boolean {
    if (mode.value !== 'compare') return false
    if (!canSubmit.value) return false
    if (options.compareStreamEnabled && !options.compareStreamEnabled.value) return false
    if (!supportsCompareStream()) return false
    cancelRealtime()
    activeController?.abort()
    activeController = null
    result.value = null
    pageError.value = ''
    lastTitle.value = trimmedTitle.value
    lastRequestId.value = ''
    compareStreamNonce += 1
    compareStreamRequest.value = {
      title: trimmedTitle.value,
      models: [...selectedModels.value],
      nonce: compareStreamNonce,
    }
    return true
  }

  function clearRealtimeState(): void {
    realtimeResult.value = null
    realtimeStatus.value = 'idle'
    realtimeTitle.value = ''
  }

  function cancelRealtime(): void {
    if (realtimeTimer !== null) {
      clearTimeout(realtimeTimer)
      realtimeTimer = null
    }
    realtimeController?.abort()
    realtimeController = null
    // 使任何已发出的实时请求失效，即使 abort 尚未传播。
    realtimeSeq += 1
    clearRealtimeState()
  }

  function realtimeEligible(): boolean {
    const text = title.value.trim()
    return (
      !composing.value &&
      !submitting.value &&
      text.length >= REALTIME_MIN_CHARS &&
      text.length <= maxTitleChars
    )
  }

  function scheduleRealtime(): void {
    if (realtimeTimer !== null) {
      clearTimeout(realtimeTimer)
      realtimeTimer = null
    }
    if (!realtimeEligible()) {
      cancelRealtime()
      return
    }
    realtimeTimer = setTimeout(() => {
      realtimeTimer = null
      void runRealtime()
    }, REALTIME_DEBOUNCE_MS)
  }

  async function runRealtime(): Promise<void> {
    if (!realtimeEligible()) return
    const text = title.value.trim()
    if (!text) return

    // 新请求发出前先取消旧请求；序号保证迟到响应不更新页面。
    realtimeController?.abort()
    const controller = new AbortController()
    realtimeController = controller
    const requestSeq = ++realtimeSeq
    realtimeResult.value = null
    realtimeTitle.value = text
    realtimeStatus.value = 'loading'

    try {
      const response = await classifyRealtime(text, REALTIME_MODEL_ID, controller.signal)
      if (controller.signal.aborted || requestSeq !== realtimeSeq) return
      realtimeResult.value = response
      realtimeStatus.value = 'success'
    } catch (reason) {
      // F5：实时失败静默降级，不写 pageError、不弹 Toaster。
      if (controller.signal.aborted || requestSeq !== realtimeSeq) return
      if (isAbortError(reason)) return
      realtimeResult.value = null
      realtimeStatus.value = 'idle'
    } finally {
      if (realtimeController === controller) realtimeController = null
    }
  }

  function onTitleUpdate(value: string): void {
    title.value = value
    result.value = null
    pageError.value = ''
    compareStreamRequest.value = null
    // 每次有效输入立即取消旧实时请求，等待 debounce 后再发新请求。
    cancelRealtime()
    if (!composing.value) scheduleRealtime()
  }

  function onCompositionStart(): void {
    composing.value = true
    cancelRealtime()
  }

  function onCompositionEnd(value: string): void {
    title.value = value
    composing.value = false
    result.value = null
    pageError.value = ''
    compareStreamRequest.value = null
    cancelRealtime()
    scheduleRealtime()
  }

  function chooseExample(value: string): void {
    title.value = value
    result.value = null
    pageError.value = ''
    composing.value = false
    compareStreamRequest.value = null
    cancelRealtime()
    scheduleRealtime()
  }

  function toggleModel(modelId: string): void {
    if (modelId === 'llm' && !includeLlm.value) return
    selectedModels.value = selectedModels.value.includes(modelId)
      ? selectedModels.value.filter((id) => id !== modelId)
      : [...selectedModels.value, modelId]
  }

  function activateLlm(): void {
    // v-model has already changed includeLlm before this event handler runs.
    if (!includeLlm.value) {
      selectedModels.value = selectedModels.value.filter((id) => id !== 'llm')
      if (selectedModel.value === 'llm') selectedModel.value = defaultModel.value === 'llm' ? '' : defaultModel.value
    }
  }

  async function submit(signal?: AbortSignal): Promise<void> {
    if (!canSubmit.value) return
    // 显式提交开始后取消实时请求，避免两类请求互相覆盖页面状态。
    cancelRealtime()
    // 走 POST 路径时作废任何流式请求（ResultStage 会 stop 旧 SSE）。
    compareStreamRequest.value = null
    activeController?.abort()
    const controller = new AbortController()
    activeController = controller
    const requestSignal = signal ?? controller.signal
    submitting.value = true
    pageError.value = ''
    result.value = null
    lastTitle.value = trimmedTitle.value

    try {
      if (mode.value === 'single') {
        result.value = await classify(trimmedTitle.value, selectedModel.value, requestSignal)
      } else {
        result.value = await compare(trimmedTitle.value, selectedModels.value, requestSignal)
      }
      lastRequestId.value = result.value.request_id
      // F15：页面级来源只取服务端显式返回，不按可达性推断。
      if ('pred_class' in result.value) {
        setObservedSource(result.value.data_source)
      } else {
        const sources = Array.from(
          new Set(
            (result.value.results ?? [])
              .map((entry) => entry.data_source)
              .filter((source) => Boolean(source)),
          ),
        )
        setObservedSource(sources.length === 1 ? sources[0] : null)
      }
    } catch (reason) {
      if (isAbortError(reason) || signal?.aborted) return
      pageError.value = friendlyErrorMessage(reason)
      lastRequestId.value =
        reason instanceof ApiClientError ? (reason.requestId ?? '') : ''
    } finally {
      if (activeController === controller) {
        submitting.value = false
        activeController = null
      }
    }
  }

  function cancel(): void {
    activeController?.abort()
  }

  onScopeDispose(() => {
    cancel()
    cancelRealtime()
  })

  return {
    title,
    mode,
    selectedModel,
    selectedModels,
    includeLlm,
    submitting,
    pageError,
    result,
    lastTitle,
    lastRequestId,
    realtimeResult,
    realtimeStatus,
    realtimeTitle,
    composing,
    trimmedTitle,
    selectedModelNames,
    canSubmit,
    singleResult,
    compareResult,
    pageHeading,
    compareStreamRequest,
    compareStreaming,
    setMode,
    onTitleUpdate,
    onCompositionStart,
    onCompositionEnd,
    chooseExample,
    toggleModel,
    activateLlm,
    submit,
    submitCompareStream,
    clearCompareStreamRequest,
    cancel,
    cancelRealtime,
  }
}
