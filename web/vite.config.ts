import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const backend = process.env.PARSEFLOW_BACKEND || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { '/api': backend, '/health': backend },
  },
  test: { environment: 'jsdom', setupFiles: './src/test-setup.ts', restoreMocks: true, exclude: ['e2e/**', 'node_modules/**'] },
})
