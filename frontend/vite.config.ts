import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '192.168.12.54',
    port: 5173,
    strictPort: true,
  },
})
