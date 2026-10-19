import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'

const source = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('project navigation and page routes expose RAG Brain without AI Chat', () => {
  const routes = source('../components/nav-routes.ts')
  const app = source('../App.tsx')
  const api = source('../api.ts')
  assert.match(routes, /'rag-brain'/)
  assert.match(routes, /label: 'RAG Brain'/)
  assert.doesNotMatch(routes, /AI Chat|id: 'chat'|'chat' \|/)
  assert.match(app, /page === 'rag-brain'/)
  assert.doesNotMatch(app, /ChatPage|page === 'chat'/)
  assert.doesNotMatch(api, /messages: \(|chat: \(|\/chat`/)
})

