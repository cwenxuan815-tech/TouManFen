<script setup lang="ts">
/**
 * 站点骨架（F2/F14）。
 *
 * F14：
 * - 首个可聚焦元素是「跳到主要内容」skip link，指向 `<main id="main-content">`；
 * - main 带 `tabindex="-1"`，供 skip link 聚焦并滚动（不进入 Tab 顺序）；
 * - 原 `#top` 锚点移到最外层容器，保持品牌链接 `href="#top"` 行为不变。
 */
defineSlots<{
  header?: () => unknown
  default?: () => unknown
  footer?: () => unknown
}>()
</script>

<template>
  <div id="top" class="site-shell">
    <a class="skip-link" href="#main-content" data-skip-link>跳到主要内容</a>
    <slot name="header" />
    <main id="main-content" tabindex="-1">
      <slot />
    </main>
    <slot name="footer" />
  </div>
</template>

<style scoped>
  .site-shell {
    overflow: hidden;
  }

  /* F14：skip link 平时移出视口，键盘聚焦时出现在左上角。
     使用 position: fixed 避免被 .site-shell 的 overflow 裁切。 */
  .skip-link {
    position: fixed;
    top: 12px;
    left: 12px;
    z-index: 200;
    display: inline-flex;
    align-items: center;
    min-height: 40px;
    padding: 0 var(--space-16);
    border: 1px solid var(--brand-dark);
    border-radius: var(--radius-pill);
    background: var(--surface-card);
    color: var(--brand-dark);
    font-size: 13px;
    font-weight: 700;
    box-shadow: 0 10px 26px #1e2f4d2e;
    transform: translateY(-160%);
    transition: transform var(--duration-fast) ease;
  }

  .skip-link:focus,
  .skip-link:focus-visible {
    transform: translateY(0);
  }

  @media (prefers-reduced-motion: reduce) {
    .skip-link {
      transition: none;
    }
  }
</style>
