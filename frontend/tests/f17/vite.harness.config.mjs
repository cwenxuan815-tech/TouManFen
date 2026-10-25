import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')

/** F17 独立构建：只构建 tests/f17/harness.html，不碰 frontend/dist。 */
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
