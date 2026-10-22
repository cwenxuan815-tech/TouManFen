<script setup lang="ts">
import { nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { useModels } from './composables'
import { setObservedSource, useDemoState } from './composables/useDemoState'
import { categoryInfo } from './components/shared/meta'
import AppShell from './components/layout/AppShell.vue'
import NavBar from './components/layout/NavBar.vue'
import SiteFooter from './components/layout/SiteFooter.vue'
import HeroSection from './components/sections/HeroSection.vue'
import ClassifyPanel from './components/sections/ClassifyPanel.vue'
import ModelMatrix from './components/sections/ModelMatrix.vue'
import ArenaPage from './components/placeholders/ArenaPage.vue'
import BenchPage from './components/placeholders/BenchPage.vue'
import ClosingSection from './components/sections/ClosingSection.vue'
import DemoBadge from './components/placeholders/DemoBadge.vue'
import { Toaster } from './components/common'

type View = 'lab' | 'arena' | 'bench'

function resolveView(): View {
  if (typeof window === 'undefined') return 'lab'
  const hash = window.location.hash
  if (hash === '#arena') return 'arena'
  if (hash === '#bench') return 'bench'
  return 'lab'
}

const view = ref<View>(resolveView())

function onHashChange(): void {
  const hash = window.location.hash
  view.value = resolveView()
  if (view.value === 'lab' && hash && hash !== '#arena' && hash !== '#bench') {
    void nextTick(() => {
      document.querySelector(hash)?.scrollIntoView({ block: 'start' })
    })
  }
}

const { models, defaultModel, loading, error, load } = useModels()
const {
  displaySource,
  snapshotLoaded,
  snapshotGeneratedAt,
  snapshotError,
  snapshotLoading,
  cachedTitles,
  urlDemoHint,
  loadSnapshot,
  resetSnapshot,
  refresh,
  startupDetail,
  sourceBreakdown,
} = useDemoState()

watch(
  error,
  (message) => {
    if (message) {
      setObservedSource(null)
      void loadSnapshot()
    }
  },
  { immediate: true },
)

watch([loading, error], ([isLoading, message]) => {
  if (!isLoading && !message && snapshotLoaded.value) {
    resetSnapshot()
    setObservedSource(null)
  }
})

function retryBackend(): void {
  void load()
}

function cachedResultText(item: (typeof cachedTitles.value)[number]): string {
  const predicted = item.results
    ?.filter((entry) => entry.status === 'ok' && entry.pred_class)
    .map((entry) => {
      const info = categoryInfo(String(entry.pred_class))
      return `${entry.model_id ?? '模型'}：${info.zh}`
    })
  if (predicted?.length) return predicted.join('；')
  return '该案例无缓存模型预测，仅保留演示预期'
}

onMounted(() => {
  window.addEventListener('hashchange', onHashChange)
  void refresh()
  void load()
})

onUnmounted(() => {
  window.removeEventListener('hashchange', onHashChange)
})
</script>

<template>
  <AppShell>
    <template #header>
      <NavBar :loading="loading" :has-error="Boolean(error)" />
    </template>

    <div class="demo-status" data-testid="demo-status">
      <DemoBadge
        :source="displaySource"
        :label="displaySource === 'unknown' ? '来源未确认' : undefined"
        :detail="startupDetail"
      />
      <span v-if="urlDemoHint" class="demo-url-hint" role="note">{{ urlDemoHint }}</span>
      <span v-if="snapshotLoading" class="demo-hint" role="status">正在尝试读取离线快照…</span>
    </div>
    <p v-if="sourceBreakdown" class="demo-source-breakdown" data-testid="demo-source-breakdown">
      来源明细：{{ sourceBreakdown }}
    </p>

    <section
      v-if="snapshotLoaded"
      class="cached-snapshot"
      data-testid="cached-snapshot"
      aria-labelledby="cached-snapshot-title"
    >
      <header class="cached-head">
        <div>
          <p class="cached-kicker">OFFLINE SNAPSHOT · 非实时</p>
          <h2 id="cached-snapshot-title">离线快照</h2>
          <p class="cached-note">
            后端当前不可达，以下内容来自构建产物 <code>public/snapshot.json</code>，
            生成于 <strong>{{ snapshotGeneratedAt }}</strong>，数据来源统一标注为
            <strong>cached</strong>；不写入运行时统计，LLM 未纳入。
          </p>
        </div>
        <button type="button" class="cached-retry" :disabled="loading" @click="retryBackend">
          {{ loading ? '正在重连…' : '重试后端' }}
        </button>
      </header>
      <ul class="cached-list">
        <li v-for="item in cachedTitles" :key="item.case_id || item.title" class="cached-item">
          <div class="cached-item-main">
            <span class="cached-case">{{ item.case_id || 'demo-case' }}</span>
            <span class="cached-title">{{ item.title }}</span>
          </div>
          <div class="cached-meta">
            <span class="cached-state">{{ item.expected_state || 'cached' }}</span>
            <span class="cached-pred">
              预期：{{ item.expected_pred ? categoryInfo(item.expected_pred).zh : '无模型预测' }}
            </span>
          </div>
          <p class="cached-results">{{ cachedResultText(item) }}</p>
        </li>
      </ul>
      <p class="cached-foot">
        后端恢复后将自动退出离线快照；如需排查后端，请检查服务进程、端口与
        <code>/api/v1/healthz</code>。
      </p>
    </section>

    <p v-if="snapshotError" class="cached-error" role="alert" data-testid="snapshot-error">
      {{ snapshotError }}
    </p>

    <template v-if="view === 'arena'">
      <!-- 只在用户显式进入竞技场时挂载：避免默认首页触发 5 模型抽题。 -->
      <ArenaPage />
    </template>
    <template v-else-if="view === 'bench'">
      <BenchPage />
    </template>
    <template v-else>
      <HeroSection />
      <ClassifyPanel
        :models="models"
        :default-model="defaultModel"
        :loading-models="loading"
        :models-error="error"
        @retry-models="load()"
      />
      <ModelMatrix :models="models" :loading="loading" />
      <ClosingSection />
    </template>

    <template #footer>
      <SiteFooter />
    </template>
  </AppShell>
  <Toaster />
</template>

<style scoped>
.demo-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-8);
  max-width: var(--layout-wide);
  margin: 0 auto;
  padding: var(--space-10) clamp(20px, 4vw, 64px) 0;
}
.demo-url-hint {
  color: var(--text-quiet);
  font-size: 10px;
}
.demo-source-breakdown {
  max-width: var(--layout-wide);
  margin: var(--space-4) auto 0;
  padding: 0 clamp(20px, 4vw, 64px);
  color: var(--warning-text);
  font-size: 10px;
  line-height: 1.6;
}
.cached-snapshot {
  max-width: var(--layout-wide);
  margin: var(--space-12) auto 0;
  padding: var(--space-16);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-xl);
  background: var(--surface-card);
}
.cached-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-12);
}
.cached-kicker {
  margin: 0 0 var(--space-4);
  color: var(--text-quiet);
  font-size: 10px;
  letter-spacing: 0.08em;
}
.cached-head h2 {
  margin: 0;
  font-size: 20px;
}
.cached-note {
  max-width: 760px;
  margin: var(--space-6) 0 0;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.7;
}
.cached-retry {
  flex: 0 0 auto;
  padding: var(--space-6) var(--space-12);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-pill);
  background: var(--surface-page);
  color: var(--text-primary);
  font: inherit;
  font-size: 11px;
  cursor: pointer;
}
.cached-list {
  display: grid;
  gap: var(--space-6);
  margin: var(--space-12) 0 0;
  padding: 0;
  list-style: none;
}
.cached-item {
  padding: var(--space-8);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-md);
  background: var(--surface-page);
}
.cached-item-main {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6);
  align-items: baseline;
}
.cached-case {
  color: var(--text-quiet);
  font-size: 9px;
}
.cached-title {
  font-size: 12px;
  font-weight: 600;
}
.cached-meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-8);
  margin-top: var(--space-4);
  color: var(--text-secondary);
  font-size: 10px;
}
.cached-state {
  color: var(--warning-text);
}
.cached-results {
  margin: var(--space-4) 0 0;
  color: var(--text-quiet);
  font-size: 10px;
}
.cached-foot {
  margin: var(--space-10) 0 0;
  color: var(--text-quiet);
  font-size: 10px;
}
.cached-error {
  max-width: var(--layout-wide);
  margin: var(--space-8) auto;
  padding: var(--space-8) clamp(20px, 4vw, 64px);
  color: var(--warning-text);
  font-size: 11px;
}
</style>
