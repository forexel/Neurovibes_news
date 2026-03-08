import path from 'path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://localhost:8000',
      '/articles': 'http://localhost:8000',
      '/article': 'http://localhost:8000',
      '/admin-data': 'http://localhost:8000',
      '/setup': 'http://localhost:8000',
      '/login': 'http://localhost:8000',
      '/logout': 'http://localhost:8000',
      '/register': 'http://localhost:8000',
      '/pipeline': 'http://localhost:8000',
      '/scoring': 'http://localhost:8000',
      '/content': 'http://localhost:8000',
      '/sources': 'http://localhost:8000',
      '/score-params': 'http://localhost:8000',
      '/runtime-settings': 'http://localhost:8000',
      '/feedback': 'http://localhost:8000',
      '/selection': 'http://localhost:8000',
      '/telegram': 'http://localhost:8000'
    }
  },
  build: {
    rollupOptions: {
      output: {
        entryFileNames: 'assets/app.js',
        chunkFileNames: 'assets/chunk-[name].js',
        assetFileNames: (assetInfo) => {
          const name = String(assetInfo?.name || '')
          if (name.endsWith('.css')) return 'assets/app.css'
          return 'assets/[name][extname]'
        }
      }
    }
  },
  assetsInclude: ['**/*.svg', '**/*.csv']
})
