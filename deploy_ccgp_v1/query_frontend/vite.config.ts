import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5174,
    proxy: {
      '/api/docs': { target: 'http://127.0.0.1:8765', rewrite: () => '/docs' },
      '/openapi.json': { target: 'http://127.0.0.1:8765' },
      '/api': { target: 'http://127.0.0.1:8765' },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/test/setup.ts',
  },
})
