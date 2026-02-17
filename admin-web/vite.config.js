import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://localhost:8000',
      '/articles': 'http://localhost:8000',
      '/pipeline': 'http://localhost:8000',
      '/feedback': 'http://localhost:8000',
      '/selection': 'http://localhost:8000',
      '/telegram': 'http://localhost:8000'
    }
  }
})
