import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

// 监听地址/端口可通过环境变量覆盖（默认本地单机 127.0.0.1:5173）：
//   TMF_FRONTEND_HOST / TMF_FRONTEND_PORT
// 局域网示例：TMF_FRONTEND_HOST=0.0.0.0（监听所有网卡）或本机网卡 IP。
// 客户端访问地址（VITE_API_BASE_URL）与监听地址是不同概念，见 frontend/README.md。
export default defineConfig(({ mode }) => {
  // 从 .env* 读取 TMF_ 前缀变量；同时兼容 shell 环境变量。
  const fileEnv = loadEnv(mode, '.', 'TMF_')
  const processEnv =
    (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env ?? {}
  const host = fileEnv.TMF_FRONTEND_HOST || processEnv.TMF_FRONTEND_HOST || '127.0.0.1'
  const port = Number(fileEnv.TMF_FRONTEND_PORT || processEnv.TMF_FRONTEND_PORT || 5173)

  return {
    plugins: [vue()],
    server: {
      host,
      port,
      strictPort: true,
    },
  }
})
