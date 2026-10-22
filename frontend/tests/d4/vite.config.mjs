import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// D4 隔离复验专用：只读取 tests/d4/env，避免 .env.local 把 API 指向 LAN/用户服务。
export default defineConfig({
  plugins: [vue()],
  envDir: 'tests/d4/env',
})
