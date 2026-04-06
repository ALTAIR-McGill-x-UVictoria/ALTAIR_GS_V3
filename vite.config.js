import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Proxy WebSocket and API calls to the Python backend
    proxy: {
      '/api/ws': { target: 'ws://localhost:8000', ws: true, changeOrigin: true },
      '/api':    { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
