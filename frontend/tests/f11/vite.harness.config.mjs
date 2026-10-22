import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')

/**
 * F11 验收 harness 的独立 Vite 构建（不触碰用户 frontend/dist）：
 * - envDir 指向 tests/f11/env，避免读取 frontend/.env.local 的局域网地址；
 * - 只构建 tests/f11/harness.html；产物写入 tests/f11/dist（gitignore 忽略）。
 */
export default defineConfig({
  root: frontendRoot,
  envDir: resolve(here, 'env'),
  plugins: [vue()],
  logLevel: 'warn',
  build: {
    outDir: resolve(here, 'dist'),
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      input: resolve(here, 'harness.html'),
    },
  },
})
