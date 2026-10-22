/** F3：模型列表加载状态（14 个顶层 ref 迁移之一）。 */
import { onScopeDispose, ref } from 'vue'
import { friendlyErrorMessage, getModels, isAbortError } from '../api'
import type { ModelInfo } from '../types'

export function useModels() {
  const models = ref<ModelInfo[]>([])
  const defaultModel = ref('')
  const loading = ref(true)
  const error = ref('')
  let activeController: AbortController | null = null

  async function load(signal?: AbortSignal): Promise<void> {
    activeController?.abort()
    const controller = new AbortController()
    activeController = controller
    const requestSignal = signal ?? controller.signal
    loading.value = true
    error.value = ''
    try {
      const data = await getModels(requestSignal)
      models.value = data.models
      defaultModel.value = data.default_model
    } catch (reason) {
      if (isAbortError(reason) || signal?.aborted) return
      error.value = friendlyErrorMessage(reason)
    } finally {
      if (activeController === controller) {
        loading.value = false
        activeController = null
      }
    }
  }

  function cancel(): void {
    activeController?.abort()
  }

  onScopeDispose(cancel)

  return { models, defaultModel, loading, error, load, cancel }
}
