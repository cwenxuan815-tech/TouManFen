<script setup lang="ts">
// 静态页脚：品牌样式来自全局共享基础层，页脚布局样式在本组件 scoped 中。
</script>

<template>
  <footer class="footer">
    <a class="brand footer-brand" href="#top"><span class="brand-mark">T<span class="brand-mark-dot">·</span></span><span class="brand-name">投满分 <small>TMF</small></span></a>
    <span>中文新闻标题分类实验台</span>
    <div><span>LOCAL VALIDATION BUILD</span><span class="footer-divider"></span><span>© TMF</span></div>
  </footer>
</template>

<style scoped>
  .footer {
    max-width: var(--layout-wide);
    margin: 0 auto;
    padding: 0 6.1% var(--space-30);
    display: flex;
    align-items: center;
    gap: var(--space-24);
    color: #929aa7;
    font-size: 10px;
  }

  .footer-brand .brand-mark {
    font-size: 25px;
  }

  .footer-brand .brand-name {
    font-size: 13px;
    color: #3c4655;
  }

  .footer>div {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: var(--space-12);
    font-size: 9px;
    letter-spacing: .08em;
  }

  .footer-divider {
    width: 1px;
    height: 12px;
    background: #dce1e8;
  }

  @media(max-width:760px) {
    .footer {
      padding: 0 5% var(--space-24);
      flex-wrap: wrap;
      gap: var(--space-9) var(--space-16);
    }
    .footer>span {
      font-size: 9px;
    }
    .footer>div {
      width: 100%;
      margin-left: 0;
      margin-top: var(--space-7);
    }
  }
</style>
