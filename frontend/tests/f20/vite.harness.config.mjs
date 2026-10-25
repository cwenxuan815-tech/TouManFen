import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')
const port = Number(process.env.F20_SERVER_PORT || 18196)

/** F20 独立构建：只构建 tests/f20/harness.html，不碰 frontend/dist。 */
export default defineConfig({
  root: frontendRoot,
  plugins: [vue()],
  logLevel: 'warn',
  define: {
    'import.meta.env.VITE_API_BASE_URL': JSON.stringify(
      `http://127.0.0.1:${port}`,
    ),
  },
  build: {
    outDir: resolve(here, 'dist'),
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      input: resolve(here, 'harness.html'),
    },
  },
})
