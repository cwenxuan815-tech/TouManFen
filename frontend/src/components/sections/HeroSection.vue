<script setup lang="ts">
// 静态 Hero 区块：无状态、无事件，仅承载原模板与样式。
</script>

<template>
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
</template>

<style scoped>
  .hero-section {
    max-width: var(--layout-wide);
    min-height: 600px;
    margin: 0 auto;
    padding: 92px 9.5% 75px;
    position: relative;
    display: grid;
    grid-template-columns: 1fr 1fr;
    align-items: center;
  }

  .hero-copy {
    position: relative;
    z-index: 2;
    padding-bottom: 26px;
  }

  .hero-copy h1 {
    font-size: clamp(52px,5.2vw,85px);
    line-height: 1.22;
    letter-spacing: -.075em;
    font-weight: 700;
    margin: 27px 0 var(--space-24);
  }

  .hero-copy h1 span {
    color: #2468f2;
  }

  .hero-description {
    font-size: 16px;
    line-height: 2;
    color: #747d8b;
    letter-spacing: .015em;
  }

  .hero-link {
    display: inline-flex;
    align-items: center;
    gap: var(--space-12);
    margin-top: var(--space-30);
    font-size: 13px;
    font-weight: 700;
  }

  .link-arrow {
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border: 1px solid #d7dee8;
    border-radius: var(--radius-circle);
    font-size: 16px;
    color: var(--brand);
    transition: all var(--duration-fast);
  }

  .hero-link:hover .link-arrow {
    color: var(--text-on-brand);
    background: var(--brand);
    border-color: var(--brand);
    transform: translateY(2px);
  }

  .hero-art {
    height: 430px;
    position: relative;
    display: grid;
    place-items: center;
  }

  .art-glow {
    position: absolute;
    width: 360px;
    height: 360px;
    border-radius: var(--radius-circle);
    background: radial-gradient(circle,#d8e8ff 0,#e9f2ff 39%,rgba(232,241,255,.12) 72%);
    filter: blur(4px);
  }

  .hero-orb {
    width: 275px;
    height: 275px;
    border-radius: var(--radius-circle);
    position: relative;
    z-index: 1;
    background: radial-gradient(circle at 35% 27%,var(--color-white) 0,var(--brand-soft-alt) 18%,#a9c9ff 42%,#6d9cff 67%,#4c75e7 100%);
    box-shadow: inset -25px -25px 54px #4167ca44,inset 19px 15px 44px var(--border-on-art),0 38px 90px #4d79dd32;
    overflow: hidden;
    transform: rotate(-18deg);
  }

  .orb-core {
    position: absolute;
    width: 124px;
    height: 124px;
    border-radius: var(--radius-circle);
    left: 77px;
    top: 74px;
    background: radial-gradient(circle at 35% 30%,var(--color-white) 0,#eaf4ff 18%,#bdd8ff 40%,#7ca5fb 74%,#537be4 100%);
    box-shadow: 0 12px 34px #345dd740,inset -9px -9px 22px #2c54c633;
  }

  .orb-ring {
    position: absolute;
    border-radius: var(--radius-circle);
    border: 1px solid var(--border-on-art);
    transform: rotate(-37deg);
  }

  .ring-a {
    width: 375px;
    height: 130px;
    left: -50px;
    top: 70px;
  }

  .ring-b {
    width: 340px;
    height: 95px;
    left: -32px;
    top: 90px;
    transform: rotate(43deg);
    border-color: #bdd5ff;
  }

  .orbit {
    position: absolute;
    border: 1px solid #8fb5ff70;
    border-radius: var(--radius-circle);
    z-index: 0;
  }

  .orbit-one {
    width: 390px;
    height: 135px;
    transform: rotate(-33deg);
  }

  .orbit-two {
    width: 360px;
    height: 115px;
    transform: rotate(39deg);
    border-color: #bfd2f366;
  }

  .float-chip {
    position: absolute;
    z-index: 3;
    display: flex;
    align-items: center;
    gap: var(--space-11);
    padding: var(--space-12) var(--space-15);
    border: 1px solid #ffffffc9;
    border-radius: var(--radius-xl);
    background: #ffffffd9;
    box-shadow: 0 15px 42px #263e6912;
    backdrop-filter: blur(14px);
  }

  .float-chip b,.float-chip small {
    display: block;
  }

  .float-chip b {
    font-size: 12px;
    font-weight: 700;
  }

  .float-chip small {
    margin-top: var(--space-4);
    color: #8b94a2;
    font-size: 10px;
  }

  .chip-news {
    top: 72px;
    left: 7px;
  }

  .chip-symbol {
    width: 32px;
    height: 32px;
    display: grid;
    place-items: center;
    border-radius: var(--radius-sm);
    background: var(--brand-soft-alt);
    color: #3579ff;
    font-size: 18px;
    font-weight: 700;
  }

  .chip-channel {
    right: 0;
    bottom: 82px;
  }

  .channel-dot {
    width: 10px;
    height: 10px;
    border-radius: var(--radius-circle);
    background: linear-gradient(135deg,#55a9ff,#586df4);
    box-shadow: 0 0 0 7px var(--brand-soft);
  }

  .art-caption {
    position: absolute;
    right: 22px;
    bottom: 23px;
    left: 25px;
    display: flex;
    justify-content: space-between;
    font-size: 9px;
    letter-spacing: .13em;
    color: #8b9bb6;
  }

  .hero-index {
    position: absolute;
    bottom: 31px;
    left: 9.5%;
    display: flex;
    align-items: center;
    gap: var(--space-12);
    color: #8d96a4;
    font-size: 10px;
    letter-spacing: .08em;
  }

  .hero-index span:first-child {
    font-size: 13px;
    color: var(--brand);
    font-weight: 700;
  }

  .hero-index i {
    width: 40px;
    height: 1px;
    background: #d4dbe5;
  }

  @media(max-width:1050px) {
    .hero-section {
      padding-left: 7%;
      padding-right: 7%;
      min-height: 540px;
    }
    .hero-copy h1 {
      font-size: 65px;
    }
    .hero-art {
      transform: scale(.88);
    }
  }

  @media(max-width:760px) {
    .hero-section {
      display: block;
      min-height: 670px;
      padding: 78px 7% 55px;
    }
    .hero-copy {
      padding: 0;
    }
    .hero-copy h1 {
      font-size: clamp(48px,11vw,69px);
      margin: 23px 0 var(--space-17);
    }
    .hero-description {
      font-size: 13px;
    }
    .hero-link {
      margin-top: 21px;
    }
    .hero-art {
      height: 335px;
      margin-top: var(--space-4);
      transform: scale(.83);
      transform-origin: center top;
    }
    .hero-orb {
      width: 250px;
      height: 250px;
    }
    .orb-core {
      left: 69px;
      top: 67px;
    }
    .chip-news {
      top: 42px;
      left: 0;
    }
    .chip-channel {
      right: -7px;
      bottom: 67px;
    }
    .hero-index {
      left: 7%;
      bottom: 19px;
    }
  }

  @media(max-width:390px) {
    .hero-copy h1 {
      font-size: 47px;
    }
    .hero-art {
      margin-left: -25px;
      margin-right: -25px;
      transform: scale(.72);
      height: 300px;
    }
  }
</style>
