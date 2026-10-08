import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const evidenceBackend = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env?.FIRMSIGHT_EVIDENCE_BACKEND ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': evidenceBackend, '/health': evidenceBackend } },
})
