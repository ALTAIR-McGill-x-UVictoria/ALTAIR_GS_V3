import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Allow Cloudflare Tunnel access from the public hostname
    allowedHosts: ['altairlive.interstellarflight.space'],
    // Proxy WebSocket and API calls to the Python backend
    proxy: {
      '/ws': { target: 'ws://localhost:8000',  ws: true,  changeOrigin: true },
      '/api': { target: 'http://localhost:8000', changeOrigin: true, ws: true },
    },
  },
})
