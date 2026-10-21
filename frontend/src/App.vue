<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ApiClientError, classify, compare, getModels } from './api'
import type { ClassifyResponse, CompareResponse, ModelInfo } from './types'

type Mode = 'single' | 'compare'

const categoryNames: Record<string, { zh: string; hint: string; color: string }> = {
  finance: { zh: '财经', hint: '宏观经济 · 银行金融', color: 'violet' },
  realty: { zh: '房产', hint: '楼市 · 地产动态', color: 'blue' },
  stocks: { zh: '股票', hint: '证券市场 · 股价走势', color: 'cyan' },
  education: { zh: '教育', hint: '校园 · 教育政策', color: 'amber' },
  science: { zh: '科技', hint: '科学发现 · 技术创新', color: 'indigo' },
  society: { zh: '社会', hint: '民生 · 社会事件', color: 'green' },
  politics: { zh: '政治', hint: '政务 · 公共政策', color: 'red' },
  sports: { zh: '体育', hint: '赛事 · 运动人物', color: 'orange' },
  game: { zh: '游戏', hint: '主机 · 电竞 · 手游', color: 'pink' },
  entertainment: { zh: '娱乐', hint: '影视 · 音乐 · 明星', color: 'purple' },
}

const examples = [
  '北京二手房成交量创年内新高',
  '新一代人工智能芯片发布，算力提升显著',
  '中国女篮公布亚洲杯参赛名单',
]

const models = ref<ModelInfo[]>([])
const defaultModel = ref('')
const title = ref('')
const mode = ref<Mode>('single')
const selectedModel = ref('')
const selectedModels = ref<string[]>([])
const includeLlm = ref(false)
const loadingModels = ref(true)
const modelsError = ref('')
const submitting = ref(false)
const pageError = ref('')
const result = ref<ClassifyResponse | CompareResponse | null>(null)
const lastTitle = ref('')
const lastRequestId = ref('')
const mobileMenuOpen = ref(false)

const trimmedTitle = computed(() => title.value.trim())
const charsLeft = computed(() => 200 - title.value.length)
const selectedModelNames = computed(() => selectedModels.value
  .map((id) => models.value.find((model) => model.model_id === id)?.display_name)
  .filter(Boolean))
const canSubmit = computed(() => {
  if (!trimmedTitle.value || title.value.length > 200 || submitting.value || modelsError.value) return false
  if (mode.value === 'single') return Boolean(selectedModel.value) && (selectedModel.value !== 'llm' || includeLlm.value)
  return selectedModels.value.length > 0 && (includeLlm.value || !selectedModels.value.includes('llm'))
})
const compareResult = computed(() => result.value && 'results' in result.value ? result.value : null)
const singleResult = computed(() => result.value && 'pred_class' in result.value ? result.value : null)
const pageHeading = computed(() => mode.value === 'single' ? '把标题，交给模型理解。' : '让不同模型，给出各自的判断。')

function categoryInfo(id: string) {
  return categoryNames[id] || { zh: id, hint: '模型分类结果', color: 'blue' }
}

function modelLabel(id: string) {
  return models.value.find((item) => item.model_id === id)?.display_name || id
}

function modelStatus(model: ModelInfo) {
  if (!model.enabled) return 'disabled'
  return model.status === 'up' ? 'up' : model.status === 'down' ? 'down' : 'unknown'
}

function statusText(status: string) {
  if (status === 'up') return '服务就绪'
  if (status === 'down') return '暂不可用'
  if (status === 'disabled') return '未启用'
  return '状态未知'
}

function setMode(nextMode: Mode) {
  mode.value = nextMode
  pageError.value = ''
  result.value = null
  if (nextMode === 'compare' && !selectedModels.value.length) {
    selectedModels.value = models.value.filter((item) => item.enabled && item.model_id !== 'llm').map((item) => item.model_id)
  }
}

function toggleModel(modelId: string) {
  if (modelId === 'llm' && !includeLlm.value) return
  selectedModels.value = selectedModels.value.includes(modelId)
    ? selectedModels.value.filter((id) => id !== modelId)
    : [...selectedModels.value, modelId]
}

function activateLlm() {
  // v-model has already changed includeLlm before this event handler runs.
  if (!includeLlm.value) {
    selectedModels.value = selectedModels.value.filter((id) => id !== 'llm')
    if (selectedModel.value === 'llm') selectedModel.value = defaultModel.value === 'llm' ? '' : defaultModel.value
  }
}

function chooseExample(value: string) {
  title.value = value
  result.value = null
  pageError.value = ''
}

function friendlyError(error: unknown) {
  if (error instanceof ApiClientError) {
    if (error.code === 'TITLE_TOO_LONG') return '标题最多支持 200 个字符。'
    if (error.code === 'INVALID_TITLE') return '请先输入新闻标题。'
    if (error.code === 'WORKER_TIMEOUT') return '模型响应超时，可以稍后重试。'
    return error.message
  }
  return '请求没有完成，请检查服务状态后重试。'
}

async function submit() {
  if (!canSubmit.value) return
  submitting.value = true
  pageError.value = ''
  result.value = null
  lastTitle.value = trimmedTitle.value

  try {
    if (mode.value === 'single') {
      result.value = await classify(trimmedTitle.value, selectedModel.value)
    } else {
      result.value = await compare(trimmedTitle.value, selectedModels.value)
    }
    lastRequestId.value = result.value.request_id
  } catch (error) {
    pageError.value = friendlyError(error)
    if (error instanceof ApiClientError && error.requestId) lastRequestId.value = error.requestId
  } finally {
    submitting.value = false
  }
}

async function loadModels() {
  loadingModels.value = true
  modelsError.value = ''
  try {
    const data = await getModels()
    models.value = data.models
    defaultModel.value = data.default_model
    selectedModel.value = data.default_model === 'llm'
      ? data.models.find((item) => item.enabled && item.model_id !== 'llm')?.model_id || ''
      : data.default_model
    selectedModels.value = data.models
      .filter((item) => item.enabled && item.model_id !== 'llm')
      .map((item) => item.model_id)
  } catch {
    modelsError.value = '暂时无法连接 TMF 后端。请先启动后端服务，再重新加载。'
  } finally {
    loadingModels.value = false
  }
}

onMounted(loadModels)
</script>

<template>
  <div class="site-shell">
    <header class="topbar">
      <a class="brand" href="#top" aria-label="投满分首页">
        <span class="brand-mark">T<span class="brand-mark-dot">·</span></span>
        <span class="brand-name">投满分 <small>TMF</small></span>
      </a>

      <button class="menu-toggle" type="button" :aria-expanded="mobileMenuOpen" aria-label="切换导航" @click="mobileMenuOpen = !mobileMenuOpen">
        <span></span><span></span>
      </button>

      <nav class="nav-links" :class="{ 'nav-open': mobileMenuOpen }" aria-label="主导航">
        <a class="nav-active" href="#lab" @click="mobileMenuOpen = false">分类实验台</a>
        <a href="#models" @click="mobileMenuOpen = false">模型矩阵</a>
        <a href="#about" @click="mobileMenuOpen = false">关于 TMF</a>
      </nav>

      <div class="topbar-right">
        <span class="api-status" :class="{ 'is-online': !modelsError && !loadingModels, 'is-loading': loadingModels }">
          <i></i>{{ loadingModels ? '连接中' : modelsError ? '后端离线' : '实验环境' }}
        </span>
        <a class="top-cta" href="#lab">开始体验 <span>↗</span></a>
      </div>
    </header>

    <main id="top">
      <section class="hero-section" aria-labelledby="hero-title">
        <div class="hero-copy">
          <div class="eyebrow"><span class="eyebrow-line"></span> NEWS ROUTING · TMF LAB</div>
          <h1 id="hero-title">让每一条新闻，<br /><span>找到它的频道。</span></h1>
          <p class="hero-description">从标题出发，看看不同模型如何理解同一条新闻。<br class="desktop-break" />一个轻量、直观的中文新闻分类实验台。</p>
          <a class="hero-link" href="#lab">开始一次分类 <span class="link-arrow">↓</span></a>
        </div>

        <div class="hero-art" aria-hidden="true">
          <div class="art-glow"></div>
          <div class="orbit orbit-one"></div>
          <div class="orbit orbit-two"></div>
          <div class="hero-orb"><span class="orb-core"></span><span class="orb-ring ring-a"></span><span class="orb-ring ring-b"></span></div>
          <div class="float-chip chip-news"><span class="chip-symbol">N</span><span><b>标题语义</b><small>理解内容，而不只是关键词</small></span></div>
          <div class="float-chip chip-channel"><span class="channel-dot"></span><span><b>智能分发</b><small>10 个新闻频道</small></span></div>
          <div class="art-caption"><span>01 / 10</span><span>FIND THE RIGHT CHANNEL</span></div>
        </div>

        <div class="hero-index"><span>01</span><i></i><span>分类 · 对比 · 探索</span></div>
      </section>

      <section id="lab" class="lab-section" aria-labelledby="lab-title">
        <div class="section-heading">
          <div>
            <div class="eyebrow"><span class="eyebrow-line"></span> CLASSIFICATION STUDIO</div>
            <h2 id="lab-title">从一个标题开始。</h2>
          </div>
          <p>选择模型，输入新闻标题，<br class="desktop-break" />观察分类结果与模型差异。</p>
        </div>

        <div class="studio-card">
          <div class="studio-topline">
            <div class="studio-label"><span class="studio-icon">✳</span> 分类工作台 <span class="label-en">/ WORKSPACE</span></div>
            <div class="studio-live"><span class="live-dot"></span> {{ loadingModels ? '正在读取模型状态' : modelsError ? '等待后端连接' : `${models.filter((item) => item.enabled).length} 个模型已登记` }}</div>
          </div>

          <div class="mode-switch" role="tablist" aria-label="分类模式">
            <button type="button" role="tab" :aria-selected="mode === 'single'" :class="{ active: mode === 'single' }" @click="setMode('single')">
              <span class="mode-icon">◉</span> 单模型分类 <small>选择一个模型</small>
            </button>
            <button type="button" role="tab" :aria-selected="mode === 'compare'" :class="{ active: mode === 'compare' }" @click="setMode('compare')">
              <span class="mode-icon compare-icon">↗</span> 模型对比 <small>并行查看结果</small>
            </button>
          </div>

          <div class="workspace-grid">
            <div class="input-column">
              <label class="field-label" for="news-title"><span>新闻标题</span><span class="field-hint">粘贴或输入一条中文标题</span></label>
              <div class="textarea-wrap" :class="{ 'textarea-error': title.length > 200 }">
                <textarea id="news-title" v-model="title" maxlength="240" placeholder="例如：北京二手房成交量创年内新高" rows="4" @input="result = null; pageError = ''"></textarea>
                <div class="textarea-footer">
                  <span class="input-tip"><span class="sparkle">✦</span> 让模型读懂标题背后的主题</span>
                  <span class="char-count" :class="{ 'count-warn': charsLeft < 0 }">{{ title.length }} <i>/</i> 200</span>
                </div>
              </div>

              <div class="examples-row">
                <span class="examples-label">试试这些</span>
                <button v-for="example in examples" :key="example" type="button" class="example-chip" @click="chooseExample(example)">{{ example }}</button>
              </div>

              <div v-if="mode === 'single'" class="model-select-wrap">
                <label class="field-label" for="model-select"><span>选择分类模型</span><span class="field-hint">默认：{{ modelLabel(defaultModel) }}</span></label>
                <div class="select-shell">
                  <select id="model-select" v-model="selectedModel" :disabled="loadingModels || !!modelsError">
                    <option v-for="model in models.filter((item) => item.enabled)" :key="model.model_id" :value="model.model_id" :disabled="model.model_id === 'llm' && !includeLlm">
                      {{ model.display_name }}{{ model.model_id === 'llm' && !includeLlm ? ' · 需手动启用' : '' }}
                    </option>
                  </select>
                  <span class="select-chevron">⌄</span>
                </div>
              </div>

              <div v-else id="model-selection" class="models-select-wrap">
                <div class="field-label"><span>参与对比的模型</span><span class="field-hint">{{ selectedModels.length }} 个已选择</span></div>
                <div v-if="loadingModels" class="model-picker-loading">正在读取模型列表…</div>
                <div v-else class="model-picker">
                  <button
                    v-for="model in models.filter((item) => item.enabled)"
                    :key="model.model_id"
                    type="button"
                    class="model-pill"
                    :class="{ chosen: selectedModels.includes(model.model_id), 'model-pill-locked': model.model_id === 'llm' && !includeLlm }"
                    :aria-pressed="selectedModels.includes(model.model_id)"
                    :disabled="model.model_id === 'llm' && !includeLlm"
                    @click="toggleModel(model.model_id)"
                  >
                    <span class="pill-check">{{ selectedModels.includes(model.model_id) ? '✓' : '' }}</span>
                    {{ model.display_name }}
                    <span v-if="model.model_id === 'llm' && !includeLlm" class="pill-lock">需启用</span>
                  </button>
                </div>
              </div>

              <label class="llm-consent" :class="{ 'llm-consent-on': includeLlm }">
                <input v-model="includeLlm" type="checkbox" @change="activateLlm" />
                <span class="custom-check">{{ includeLlm ? '✓' : '' }}</span>
                <span class="consent-copy"><b>本次允许调用大模型</b><small>真实供应商尚未验证；调用可能产生费用。默认关闭。</small></span>
                <span class="consent-tag">LLM</span>
              </label>
            </div>

            <aside class="action-column">
              <div class="action-card">
                <div class="action-card-kicker">{{ mode === 'single' ? 'ONE MODEL' : 'SIDE BY SIDE' }}</div>
                <h3>{{ pageHeading }}</h3>
                <p>{{ mode === 'single' ? '用一个模型，快速找到标题所属的新闻频道。' : '让多个模型并行判断，比较它们对标题的理解。' }}</p>

                <div class="action-summary">
                  <span class="summary-icon">✦</span>
                  <span><b>{{ mode === 'single' ? modelLabel(selectedModel) || '选择一个模型' : `${selectedModels.length} 个模型` }}</b><small>{{ mode === 'single' ? '单模型推理' : selectedModelNames.join(' · ') || '至少选择一个模型' }}</small></span>
                </div>

                <button class="submit-button" type="button" :disabled="!canSubmit" @click="submit">
                  <span v-if="submitting" class="button-spinner"></span>
                  <span>{{ submitting ? '正在分析标题' : mode === 'single' ? '开始分类' : '开始模型对比' }}</span>
                  <span v-if="!submitting" class="button-arrow">↗</span>
                </button>
                <div class="privacy-note"><span class="lock-icon">⌑</span> 标题仅用于本次推理，不会显示在服务日志中</div>
              </div>

              <div class="mini-stat-row">
                <div><b>10</b><span>新闻频道</span></div>
                <i></i>
                <div><b>{{ models.filter((item) => item.enabled).length || '—' }}</b><span>可用模型</span></div>
                <i></i>
                <div><b>API</b><span>本地推理</span></div>
              </div>
            </aside>
          </div>

          <div v-if="modelsError" class="inline-alert alert-error" role="alert">
            <span class="alert-symbol">!</span><span>{{ modelsError }}</span><button type="button" @click="loadModels">重新连接</button>
          </div>
          <div v-if="pageError" class="inline-alert alert-error" role="alert">
                <span class="alert-symbol">!</span><span>{{ pageError }}<small v-if="lastRequestId" class="error-request-id">请求编号 {{ lastRequestId }}</small></span>
          </div>
          <div v-if="includeLlm" class="inline-alert alert-warn" role="status">
            <span class="alert-symbol">i</span><span>已启用大模型。请确认后端 8105 当前连接的是本地 mock；若是真实供应商，调用可能产生费用。</span>
          </div>
        </div>

        <Transition name="result-reveal">
          <section v-if="singleResult" class="result-section" aria-live="polite">
            <div class="result-heading"><div><div class="eyebrow"><span class="eyebrow-line"></span> CLASSIFICATION RESULT</div><h3>这条新闻，属于 <span>{{ categoryInfo(singleResult.pred_class).zh }}</span></h3></div><span class="result-request">REQUEST · {{ singleResult.request_id.slice(0, 8) }}</span></div>
            <div class="single-result-card" :class="`tone-${categoryInfo(singleResult.pred_class).color}`">
              <div class="result-category-mark">{{ categoryInfo(singleResult.pred_class).zh.slice(0, 1) }}</div>
              <div class="result-main"><div class="result-kicker">{{ singleResult.pred_class }}</div><div class="result-category-name">{{ categoryInfo(singleResult.pred_class).zh }}</div><div class="result-context">{{ lastTitle }}</div></div>
              <div class="result-meta"><span>由 {{ modelLabel(singleResult.model_id) }} 判断</span><b>{{ singleResult.latency_ms }}<small> ms</small></b><span>模型耗时</span></div>
              <span class="result-orbit result-orbit-a"></span><span class="result-orbit result-orbit-b"></span>
            </div>
          </section>

          <section v-else-if="compareResult" class="result-section" aria-live="polite">
            <div class="result-heading"><div><div class="eyebrow"><span class="eyebrow-line"></span> MODEL COMPARISON</div><h3>同一个标题，<span>{{ compareResult.summary.ok }} 个模型完成判断。</span></h3></div><span class="result-request">REQUEST · {{ compareResult.request_id.slice(0, 8) }}</span></div>
            <div class="compare-overview" :class="`summary-${compareResult.summary.status}`">
              <div class="overview-icon">{{ compareResult.summary.status === 'ok' ? '✓' : compareResult.summary.status === 'partial' ? '◐' : '!' }}</div>
              <div><b>{{ compareResult.summary.status === 'ok' ? '全部模型已完成' : compareResult.summary.status === 'partial' ? '部分结果已返回' : '模型暂时无法完成判断' }}</b><span>{{ compareResult.summary.ok }} 成功 <i>·</i> {{ compareResult.summary.failed }} 失败</span></div>
              <div class="overview-title">{{ lastTitle }}</div>
            </div>
            <div class="compare-grid">
              <article v-for="item in compareResult.results" :key="item.model_id" class="compare-card" :class="{ 'compare-card-error': item.status === 'error' }">
                <div class="compare-card-head"><span class="model-avatar" :class="`avatar-${item.model_id}`">{{ modelLabel(item.model_id).slice(0, 1) }}</span><span class="model-status-dot" :class="item.status"></span></div>
                <div class="compare-model-name">{{ modelLabel(item.model_id) }}</div>
                <template v-if="item.status === 'ok' && item.pred_class">
                  <div class="compare-category" :class="`text-${categoryInfo(item.pred_class).color}`">{{ categoryInfo(item.pred_class).zh }}</div>
                  <div class="compare-category-id">{{ item.pred_class }}</div>
                </template>
                <div v-else class="compare-failure">{{ item.error?.message || '模型暂不可用' }}</div>
                <div class="compare-card-foot"><span>{{ item.status === 'ok' ? '分类完成' : item.error?.code || '调用失败' }}</span><b>{{ item.latency_ms }}<small> ms</small></b></div>
              </article>
            </div>
          </section>
        </Transition>
      </section>

      <section id="models" class="model-section">
        <div class="section-heading model-section-heading">
          <div><div class="eyebrow"><span class="eyebrow-line"></span> THE MODEL LINEUP</div><h2>五种视角，同一个标题。</h2></div>
          <p>从传统机器学习到预训练模型，<br class="desktop-break" />看看它们如何理解新闻。</p>
        </div>
        <div class="model-lineup">
          <article v-for="(model, index) in models" :key="model.model_id" class="lineup-card">
            <div class="lineup-index">0{{ index + 1 }} <span>—</span> MODEL</div>
            <div class="lineup-art" :class="`lineup-art-${model.model_id}`"><span class="lineup-shape"></span><span class="lineup-shape-inner"></span><span class="lineup-orbit"></span></div>
            <div class="lineup-bottom"><div><h3>{{ model.display_name }}</h3><p>{{ model.model_id === 'llm' ? '大语言模型 · 外部服务' : model.model_id === 'bert' ? '预训练语言模型' : model.model_id === 'fasttext' ? '词向量分类模型' : model.model_id === 'distill' ? '轻量蒸馏模型' : '传统机器学习基线' }}</p></div><span class="lineup-status" :class="`lineup-${modelStatus(model)}`"><i></i>{{ statusText(modelStatus(model)) }}</span></div>
          </article>
          <div v-if="!models.length && !loadingModels" class="lineup-empty">连接后端后，这里会展示当前模型列表。</div>
        </div>
        <div class="model-footnote"><span>ⓘ</span> 大模型状态仅表示本地 worker 就绪，不代表真实供应商已验证。模型版本信息以服务端返回为准。</div>
      </section>

      <section id="about" class="closing-section">
        <div class="closing-glow"></div>
        <div class="closing-content"><div class="eyebrow"><span class="eyebrow-line"></span> A SMALL EXPERIMENT, A CLEARER SIGNAL</div><h2>让内容抵达<br /><span>更合适的地方。</span></h2><p>TMF 新闻分类实验台<br />为模型验证与产品探索而设计。</p><a href="#lab" class="closing-button">回到分类工作台 <span>↑</span></a></div>
        <div class="closing-mark" aria-hidden="true"><span>T</span><i></i><i></i><i></i></div>
      </section>
    </main>

    <footer class="footer">
      <a class="brand footer-brand" href="#top"><span class="brand-mark">T<span class="brand-mark-dot">·</span></span><span class="brand-name">投满分 <small>TMF</small></span></a>
      <span>中文新闻标题分类实验台</span>
      <div><span>LOCAL VALIDATION BUILD</span><span class="footer-divider"></span><span>© TMF</span></div>
    </footer>
  </div>
</template>
