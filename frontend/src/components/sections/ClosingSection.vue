<script setup lang="ts">
// 静态结尾区块：无状态、无事件。
</script>

<template>
  <section id="about" class="closing-section">
    <div class="closing-glow"></div>
    <div class="closing-content"><div class="eyebrow"><span class="eyebrow-line"></span> A SMALL EXPERIMENT, A CLEARER SIGNAL</div><h2>让内容抵达<br /><span>更合适的地方。</span></h2><p>TMF 新闻分类实验台<br />为模型验证与产品探索而设计。</p><a href="#lab" class="closing-button">回到分类工作台 <span>↑</span></a></div>
    <div class="closing-mark" aria-hidden="true"><span>T</span><i></i><i></i><i></i></div>
  </section>
</template>

<style scoped>
  .closing-section {
    max-width: var(--layout-content);
    min-height: 360px;
    margin: 0 auto 72px;
    padding: 60px 8%;
    position: relative;
    overflow: hidden;
    border-radius: 28px;
    background: #111d36;
    color: var(--text-on-dark);
  }

  .closing-glow {
    position: absolute;
    width: 520px;
    height: 520px;
    right: 8%;
    top: -180px;
    border-radius: var(--radius-circle);
    background: radial-gradient(circle,#386ecb77,#23396638 44%,transparent 70%);
    filter: blur(10px);
  }

  .closing-content {
    position: relative;
    z-index: 1;
  }

  .closing-content .eyebrow {
    color: #9baecf;
  }

  .closing-content .eyebrow-line {
    background: #6da2ff;
  }

  .closing-content h2 {
    margin: var(--space-24) 0 var(--space-14);
    font-size: 49px;
    line-height: 1.25;
    letter-spacing: -.06em;
  }

  .closing-content h2 span {
    color: #84adff;
  }

  .closing-content p {
    font-size: 11px;
    line-height: 1.9;
    color: #aab7cb;
  }

  .closing-button {
    display: inline-flex;
    align-items: center;
    gap: var(--space-15);
    margin-top: var(--space-14);
    padding: var(--space-11) var(--space-15);
    border: 1px solid #ffffff36;
    border-radius: var(--radius-pill);
    font-size: 11px;
    transition: all var(--duration-fast);
  }

  .closing-button:hover {
    background: var(--surface-inverse);
    color: #152342;
  }

  .closing-button span {
    color: #8eb4ff;
    font-size: 16px;
  }

  .closing-mark {
    position: absolute;
    right: 13%;
    top: 50%;
    width: 190px;
    height: 190px;
    transform: translateY(-50%);
    border: 1px solid #ffffff25;
    border-radius: var(--radius-circle);
    display: grid;
    place-items: center;
    color: #ffffffdd;
    font-size: 112px;
    font-weight: 700;
    letter-spacing: -.12em;
  }

  .closing-mark span {
    transform: translateX(-5px);
  }

  .closing-mark i {
    position: absolute;
    inset: -24px;
    border: 1px solid #ffffff19;
    border-radius: var(--radius-circle);
  }

  .closing-mark i:nth-of-type(2) {
    inset: -49px;
    border-color: #ffffff13;
  }

  .closing-mark i:nth-of-type(3) {
    inset: -74px;
    border-color: #ffffff0d;
  }

  @media(max-width:1050px) {
    .closing-section {
      margin-left: 4%;
      margin-right: 4%;
    }
  }

  @media(max-width:760px) {
    .closing-section {
      min-height: 340px;
      margin: 0 4% 52px;
      padding: 43px 7%;
      border-radius: var(--radius-4xl);
    }
    .closing-content h2 {
      font-size: 43px;
    }
    .closing-mark {
      right: -47px;
      top: 55%;
      transform: translateY(-50%) scale(.72);
      opacity: .45;
    }
    .closing-content {
      max-width: 84%;
    }
  }
</style>
