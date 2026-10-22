<script setup lang="ts">
import type { ModelInfo } from '../../types'
import { modelStatus, statusText } from '../shared/meta'
import AsyncState from '../common/AsyncState.vue'

defineProps<{
  models: ModelInfo[]
  loading: boolean
}>()
</script>

<template>
  <section id="models" class="model-section">
    <div class="section-heading model-section-heading">
      <div><div class="eyebrow"><span class="eyebrow-line"></span> THE MODEL LINEUP</div><h2>五种视角，同一个标题。</h2></div>
      <p>从传统机器学习到预训练模型，<br class="desktop-break" />看看它们如何理解新闻。</p>
    </div>
    <AsyncState
      v-if="loading"
      state="loading"
      loading-text="正在读取模型状态…"
    />
    <AsyncState
      v-else-if="!models.length"
      state="empty"
      title="暂无模型"
      message="连接后端后，这里会展示当前模型列表。"
    />
    <div v-else class="model-lineup">
      <article v-for="(model, index) in models" :key="model.model_id" class="lineup-card">
        <div class="lineup-index">0{{ index + 1 }} <span>—</span> MODEL</div>
        <div class="lineup-art" :class="`lineup-art-${model.model_id}`"><span class="lineup-shape"></span><span class="lineup-shape-inner"></span><span class="lineup-orbit"></span></div>
        <div class="lineup-bottom"><div><h3>{{ model.display_name }}</h3><p>{{ model.model_id === 'llm' ? '大语言模型 · 外部服务' : model.model_id === 'bert' ? '预训练语言模型' : model.model_id === 'fasttext' ? '词向量分类模型' : model.model_id === 'distill' ? '轻量蒸馏模型' : '传统机器学习基线' }}</p></div><span class="lineup-status" :class="`lineup-${modelStatus(model)}`"><i></i>{{ statusText(modelStatus(model)) }}</span></div>
      </article>
    </div>
    <div class="model-footnote"><span>ⓘ</span> 大模型状态仅表示本地 worker 就绪，不代表真实供应商已验证。模型版本信息以服务端返回为准。</div>
  </section>
</template>

<style scoped>
  .lineup-status i {
    width: 7px;
    height: 7px;
    border-radius: var(--radius-circle);
    background: #b2bac7;
  }

  .model-section-heading h2 {
    font-size: 35px;
  }

  .model-lineup {
    display: grid;
    grid-template-columns: repeat(5,minmax(0,1fr));
    gap: var(--space-12);
  }

  .lineup-card {
    min-width: 0;
    padding: var(--space-15) var(--space-14) var(--space-14);
    border: 1px solid var(--border-card);
    border-radius: var(--radius-2xl);
    background: var(--surface-card);
    transition: transform var(--duration-fast),box-shadow var(--duration-fast);
  }

  .lineup-card:hover {
    transform: translateY(-3px);
    box-shadow: 0 14px 32px #273b5a0d;
  }

  .lineup-index {
    font-size: 8px;
    letter-spacing: .13em;
    color: var(--text-subtle);
  }

  .lineup-index span {
    color: #d2d8e1;
    padding: 0 3px;
  }

  .lineup-art {
    height: 108px;
    margin: var(--space-10) 0 var(--space-6);
    position: relative;
    overflow: hidden;
    border-radius: var(--radius-md);
    background: linear-gradient(135deg,#eff5ff,#dceaff);
  }

  .lineup-shape {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 55px;
    height: 55px;
    transform: translate(-50%,-50%) rotate(-30deg);
    border-radius: var(--radius-2xl);
    background: linear-gradient(145deg,var(--color-white),var(--brand-light));
    box-shadow: 0 12px 24px #3d70cf27;
  }

  .lineup-shape-inner {
    position: absolute;
    left: 50%;
    top: 50%;
    width: 24px;
    height: 24px;
    transform: translate(-50%,-50%);
    border-radius: var(--radius-circle);
    background: linear-gradient(145deg,var(--color-white),#7ea6fc);
    box-shadow: 0 6px 13px #4770c83b;
  }

  .lineup-orbit {
    position: absolute;
    width: 145px;
    height: 42px;
    left: 50%;
    top: 50%;
    transform: translate(-50%,-50%) rotate(-31deg);
    border: 1px solid #80aaff88;
    border-radius: var(--radius-circle);
  }

  .lineup-art-fasttext {
    background: linear-gradient(135deg,#effaf5,#d8f3e6);
  }

  .lineup-art-fasttext .lineup-shape {
    background: linear-gradient(145deg,var(--color-white),#9cddbc);
    border-radius: 50% 50% 18px 50%;
  }

  .lineup-art-bert {
    background: linear-gradient(135deg,#f5f1ff,#e8e0ff);
  }

  .lineup-art-bert .lineup-shape {
    background: linear-gradient(145deg,var(--color-white),#baa5f3);
    border-radius: var(--radius-circle);
    width: 60px;
    height: 60px;
  }

  .lineup-art-llm {
    background: linear-gradient(135deg,#fff8ec,#ffebc8);
  }

  .lineup-art-llm .lineup-shape {
    background: linear-gradient(145deg,var(--color-white),#f4cb7a);
    border-radius: 50% 50% 50% 10px;
    transform: translate(-50%,-50%) rotate(45deg);
  }

  .lineup-art-distill {
    background: linear-gradient(135deg,#eefaff,#d6f3f7);
  }

  .lineup-art-distill .lineup-shape {
    width: 48px;
    height: 62px;
    border-radius: var(--radius-3xl);
    background: linear-gradient(145deg,var(--color-white),#87ccdc);
  }

  .lineup-bottom {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-5);
  }

  .lineup-bottom h3 {
    margin: 0;
    font-size: 12px;
  }

  .lineup-bottom p {
    margin: var(--space-5) 0 0;
    color: #979fac;
    font-size: 9px;
  }

  .lineup-status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-5);
    white-space: nowrap;
    font-size: 8px;
    color: var(--text-quiet);
  }

  .lineup-status i {
    width: 5px;
    height: 5px;
  }

  .lineup-up i {
    background: #3cad7a;
  }

  .lineup-down i {
    background: #e07973;
  }

  .lineup-disabled i {
    background: #b4bac3;
  }

  .model-footnote {
    display: flex;
    align-items: center;
    gap: var(--space-7);
    margin-top: var(--space-16);
    color: var(--text-quiet);
    font-size: 10px;
  }

  .model-footnote span {
    color: #4b86f7;
    font-size: 15px;
  }

  @media(max-width:1050px) {
    .model-lineup {
      grid-template-columns: repeat(3,minmax(0,1fr));
    }
    .lineup-card:nth-child(n+4) {
      display: none;
    }
  }

  @media(max-width:760px) {
    .model-section-heading h2 {
      font-size: 29px;
    }
    .model-lineup {
      grid-template-columns: repeat(2,minmax(0,1fr));
      gap: var(--space-9);
    }
    .lineup-card:nth-child(n+4) {
      display: block;
    }
    .lineup-card {
      padding: var(--space-11);
    }
    .lineup-card:last-child {
      grid-column: 1/-1;
    }
    .lineup-art {
      height: 94px;
    }
    .lineup-bottom {
      align-items: flex-start;
      flex-direction: column;
    }
    .lineup-status {
      font-size: 8px;
    }
    .model-footnote {
      align-items: flex-start;
      line-height: 1.7;
    }
  }

  @media(max-width:390px) {
    .lineup-bottom h3 {
      font-size: 11px;
    }
  }
</style>
