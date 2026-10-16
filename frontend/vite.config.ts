import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
// vitest/config re-exports Vite's defineConfig with the `test` block typed.
import { defineConfig } from 'vitest/config'

/** The backend the dev server proxies to. Production serves both from one origin. */
const BACKEND = process.env.SENTINEL_BACKEND ?? 'http://127.0.0.1:8090'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom'],
          query: ['@tanstack/react-query'],
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    // Vitest's 5s default is a per-test budget, and the dialog tests spend most
    // of theirs inside userEvent, which yields to the event loop between every
    // keystroke. With every file running in parallel on a loaded machine they
    // stopped fitting, and the failures looked like flakes rather than what
    // they were: CPU contention. This is high enough to absorb a busy CI runner
    // and still low enough that a genuinely hung test fails rather than hangs.
    testTimeout: 20_000,
  },
})
