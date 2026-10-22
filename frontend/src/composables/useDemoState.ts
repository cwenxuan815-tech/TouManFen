/**
 * F3/F15：演示真实性状态与离线快照。
 *
 * 来源只来自服务端响应 data_source 或缓存快照，不按 URL/可达性猜 live。
 * 后端请求失败时，App 可调用 loadSnapshot() 读取构建产物 public/snapshot.json。
 */
import { computed, ref } from 'vue'
import {
  DEMO_STATE_READY,
  friendlyErrorMessage,
  getDemoState,
  type DemoState,
} from '../api'
import type { DemoSource } from '../types'

export type { DemoSource }

export const DEMO_LABELS: Record<DemoSource, string> = {
  live: '实时',
  mock: '演示数据（mock）',
  cached: '离线快照',
  unknown: '来源未知',
}

export function demoLabel(source: DemoSource): string {
  return DEMO_LABELS[source]
}

export interface CachedSnapshotTitle {
  case_id?: string
  title: string
  category?: string
  expected_state?: string
  expected_pred?: string | null
  data_source?: string
  has_cached_results?: boolean
  results?: Array<{
    model_id?: string
    status?: string
    pred_class?: string
    data_source?: string
  }>
  missing_models?: string[]
}

export interface CachedSnapshot {
  generated_at: string
  data_source: 'cached'
  demo?: { titles?: CachedSnapshotTitle[]; title_count?: number }
  llm?: { status?: string }
}

const observedSource = ref<DemoSource | null>(null)

function isDemoSource(value: unknown): value is DemoSource {
  return value === 'live' || value === 'mock' || value === 'cached' || value === 'unknown'
}

/** 分类/对比结果返回后由 useClassify 调用；只记录服务端显式来源。 */
export function setObservedSource(source: unknown): void {
  if (isDemoSource(source)) {
    observedSource.value = source
  } else {
    observedSource.value = null
  }
}

function readDemoUrlHint(): string {
  if (typeof window === 'undefined') return ''
  const value = new URLSearchParams(window.location.search).get('demo')
  if (value !== 'mock' && value !== 'cached') return ''
  return `演示参数 demo=${value}（仅提示文案，不改变数据来源）`
}

export function useDemoState() {
  const supported = DEMO_STATE_READY
  const state = ref<DemoState | null>(null)
  const explicitSource = ref<DemoSource | null>(null)
  const urlDemoHint = ref(readDemoUrlHint())
  const error = ref('')
  const loading = ref(false)
  const snapshot = ref<CachedSnapshot | null>(null)
  const snapshotError = ref('')
  const snapshotLoading = ref(false)
  const snapshotLoaded = computed(() => snapshot.value !== null)

  const displaySource = computed<DemoSource>(() => {
    if (explicitSource.value) return explicitSource.value
    if (observedSource.value) return observedSource.value
    if (snapshot.value?.data_source === 'cached') return 'cached'
    return state.value?.data_source ?? 'unknown'
  })
  const displayLabel = computed(() => demoLabel(displaySource.value))
  const snapshotGeneratedAt = computed(() => snapshot.value?.generated_at ?? '')
  const startupDetail = computed(() => {
    if (snapshot.value?.data_source === 'cached') {
      return snapshot.value.generated_at ? `生成于 ${snapshot.value.generated_at}` : '离线快照'
    }
    const current = state.value
    if (!current) return error.value ? '来源查询失败' : '正在查询来源…'
    const base = `${current.ready_models}/${current.total_models} 个模型已就绪`
    if (current.data_source === 'unknown') {
      if (current.mixed) return `${base} · 来源混合`
      return `${base} · 来源未确认`
    }
    return base
  })
  const sourceBreakdown = computed(() => {
    if (displaySource.value !== 'unknown' || !state.value) return ''
    return state.value.worker_sources
      .map((item) => `${item.model_id}=${item.data_source}${item.ready ? '' : '/down'}`)
      .join(' · ')
  })
  const cachedTitles = computed<CachedSnapshotTitle[]>(
    () => snapshot.value?.demo?.titles ?? [],
  )

  function setExplicitSource(source: DemoSource | null): void {
    explicitSource.value = source
  }

  function resetSnapshot(): void {
    snapshot.value = null
    snapshotError.value = ''
    snapshotLoading.value = false
  }

  async function loadSnapshot(signal?: AbortSignal): Promise<boolean> {
    snapshotLoading.value = true
    snapshotError.value = ''
    try {
      const response = await fetch('/snapshot.json', {
        cache: 'no-store',
        signal,
        headers: { Accept: 'application/json' },
      })
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const data = (await response.json()) as CachedSnapshot
      if (
        !data ||
        data.data_source !== 'cached' ||
        typeof data.generated_at !== 'string' ||
        !Array.isArray(data.demo?.titles) ||
        data.demo.titles.length === 0
      ) {
        throw new Error('snapshot contract invalid')
      }
      snapshot.value = data
      return true
    } catch (reason) {
      resetSnapshot()
      snapshotError.value = `离线快照不可用：${
        reason instanceof Error ? reason.message : '读取失败'
      }`
      return false
    } finally {
      snapshotLoading.value = false
    }
  }

  async function refresh(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      state.value = await getDemoState()
    } catch (reason) {
      state.value = null
      error.value = friendlyErrorMessage(reason)
    } finally {
      loading.value = false
    }
  }

  return {
    supported,
    state,
    explicitSource,
    observedSource,
    urlDemoHint,
    displaySource,
    displayLabel,
    displaySourceLabel: displayLabel,
    startupDetail,
    sourceBreakdown,
    hasExplicitSource: computed(
      () => explicitSource.value !== null || state.value !== null || snapshot.value !== null,
    ),
    error,
    loading,
    snapshot,
    snapshotError,
    snapshotLoading,
    snapshotLoaded,
    snapshotGeneratedAt,
    cachedTitles,
    setExplicitSource,
    loadSnapshot,
    resetSnapshot,
    refresh,
  }
}
