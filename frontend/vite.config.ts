import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发服务器：/v1 与 /health 代理到本地 FastAPI 后端
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/v1': { target: 'http://localhost:8080', changeOrigin: true },
      '/health': { target: 'http://localhost:8080', changeOrigin: true },
    },
  },
})
