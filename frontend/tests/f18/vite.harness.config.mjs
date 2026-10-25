import { defineConfig } from 'vite'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')

/** F18 独立构建：只构建 tests/f18/harness.html，不碰 frontend/dist。 */
export default defineConfig({
  root: frontendRoot,
  plugins: [],
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
