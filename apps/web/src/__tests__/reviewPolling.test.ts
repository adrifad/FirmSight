import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import type { Review } from '../api'
import { ACTIVE_REVIEW_STORAGE_KEY, clearActiveReview, isTerminalReviewStatus, persistActiveReview, readActiveReview, startReviewPolling } from '../reviewPolling.ts'

class MemoryStorage {
  values = new Map<string, string>()
  getItem(key: string) { return this.values.get(key) ?? null }
  setItem(key: string, value: string) { this.values.set(key, value) }
  removeItem(key: string) { this.values.delete(key) }
}

class EventTargetFake {
  listeners = new Map<string, Set<() => void>>()
  addEventListener(type: string, listener: () => void) { const set = this.listeners.get(type) ?? new Set(); set.add(listener); this.listeners.set(type, set) }
  removeEventListener(type: string, listener: () => void) { this.listeners.get(type)?.delete(listener) }
  dispatch(type: string) { for (const listener of this.listeners.get(type) ?? []) listener() }
}

class BrowserFake extends EventTargetFake {
  tasks = new Map<number, () => void>()
  nextId = 1
  setTimeout(callback: () => void) { const id = this.nextId++; this.tasks.set(id, callback); return id }
  clearTimeout(id: number) { this.tasks.delete(id) }
  runNextTimer() { const [id, callback] = this.tasks.entries().next().value ?? []; if (callback) { this.tasks.delete(id); callback() } }
}

class DocumentFake extends EventTargetFake {
  visibilityState: DocumentVisibilityState = 'visible'
}

const tick = async () => { await Promise.resolve(); await Promise.resolve() }
const runningReview = (status = 'RUNNING') => ({ id: 'REV-1', project_id: 'PRJ-1', status, progress: [], finding_count: 0, scope: 'Full Project', focus: [], context_files: [], context_chars: 0, total_batches: 0, validated_batches: 0, unavailable_batches: 0, output_budget_snapshot: {} } as unknown as Review)
const active = { projectId: 'PRJ-1', reviewId: 'REV-1' }

test('transient status failure keeps the review running and reports reconnecting', async () => {
  const storage = new MemoryStorage(); persistActiveReview(storage, active)
  const browser = new BrowserFake(); const doc = new DocumentFake()
  let running = true; let reconnecting = false; const error = ''
  const stop = startReviewPolling({ active, storage, browserWindow: browser as unknown as Window, browserDocument: doc as unknown as Document,
    fetchStatus: async () => { throw new Error('temporary network failure') }, onStatus: () => { running = false },
    onConnectionChange: connected => { reconnecting = !connected; if (!connected) running = true },
  })
  await tick()
  assert.equal(running, true)
  assert.equal(reconnecting, true)
  assert.equal(error, '')
  assert.match('Connection to review status was interrupted. Reconnecting…', /Reconnecting/)
  stop()
})

test('visibilitychange, focus, and online each trigger an immediate status request', async () => {
  for (const trigger of ['visibilitychange', 'focus', 'online'] as const) {
    const storage = new MemoryStorage(); const browser = new BrowserFake(); const doc = new DocumentFake(); let calls = 0
    const stop = startReviewPolling({ active, storage, browserWindow: browser as unknown as Window, browserDocument: doc as unknown as Document,
      fetchStatus: async () => { calls += 1; if (calls === 1) throw new Error('offline'); return runningReview() },
      onStatus: () => undefined, onConnectionChange: () => undefined,
    })
    await tick(); assert.equal(calls, 1)
    if (trigger === 'visibilitychange') doc.dispatch(trigger); else browser.dispatch(trigger)
    await tick(); assert.equal(calls, 2, `${trigger} should poll immediately`)
    stop()
  }
})

test('lifecycle recovery does not overlap an in-flight status request', async () => {
  const browser = new BrowserFake(); const doc = new DocumentFake(); let calls = 0
  let resolve!: (review: Review) => void
  const pending = new Promise<Review>(done => { resolve = done })
  const stop = startReviewPolling({ active, storage: new MemoryStorage(), browserWindow: browser as unknown as Window, browserDocument: doc as unknown as Document,
    fetchStatus: () => { calls += 1; return pending }, onStatus: () => undefined, onConnectionChange: () => undefined,
  })
  browser.dispatch('focus'); browser.dispatch('online'); doc.dispatch('visibilitychange'); await tick()
  assert.equal(calls, 1)
  resolve(runningReview()); await tick()
  assert.equal(calls, 2, 'queued lifecycle recovery polls immediately after the active request settles')
  stop()
})

test('session recovery is project scoped and terminal status clears only its marker', () => {
  const storage = new MemoryStorage(); persistActiveReview(storage, active)
  assert.deepEqual(readActiveReview(storage, 'PRJ-1'), active)
  assert.equal(readActiveReview(storage, 'PRJ-2'), null)
  assert.equal(storage.getItem(ACTIVE_REVIEW_STORAGE_KEY) !== null, true)
  assert.equal(isTerminalReviewStatus('RUNNING'), false)
  for (const status of ['COMPLETED', 'PARTIAL', 'FAILED', 'INTERRUPTED']) assert.equal(isTerminalReviewStatus(status), true)
  clearActiveReview(storage, { projectId: 'PRJ-1', reviewId: 'REV-OTHER' })
  assert.ok(storage.getItem(ACTIVE_REVIEW_STORAGE_KEY))
  clearActiveReview(storage, active)
  assert.equal(storage.getItem(ACTIVE_REVIEW_STORAGE_KEY), null)
})

test('ReviewPage restores a matching session marker and uses the persisted status endpoint', () => {
  const app = readFileSync(new URL('../App.tsx', import.meta.url), 'utf8')
  assert.match(app, /readActiveReview\(reviewSessionStorage\(\), projectId\)/)
  assert.match(app, /api\.reviewStatus\(pollProjectId, pollReviewId\)/)
  assert.match(app, /Connection to review status was interrupted\. Reconnecting/)
})

test('confirmed terminal API status removes the active review marker', async () => {
  const storage = new MemoryStorage(); persistActiveReview(storage, active)
  const browser = new BrowserFake(); const doc = new DocumentFake()
  const stop = startReviewPolling({ active, storage, browserWindow: browser as unknown as Window, browserDocument: doc as unknown as Document,
    fetchStatus: async () => runningReview('INTERRUPTED'), onStatus: () => undefined, onConnectionChange: () => undefined,
  })
  await tick()
  assert.equal(storage.getItem(ACTIVE_REVIEW_STORAGE_KEY), null)
  stop()
})

test('poll failures use bounded exponential retries and success resets delay', async () => {
  const browser = new BrowserFake(); const doc = new DocumentFake(); let calls = 0
  const stop = startReviewPolling({ active, storage: new MemoryStorage(), browserWindow: browser as unknown as Window, browserDocument: doc as unknown as Document,
    fetchStatus: async () => { calls += 1; if (calls < 3) throw new Error('offline'); return runningReview() },
    onStatus: () => undefined, onConnectionChange: () => undefined,
  })
  await tick(); assert.equal(browser.tasks.size, 1)
  browser.runNextTimer(); await tick(); assert.equal(calls, 2); assert.equal(browser.tasks.size, 1)
  browser.runNextTimer(); await tick(); assert.equal(calls, 3)
  assert.equal(browser.tasks.size, 1)
  stop()
  assert.equal(browser.tasks.size, 0)
  assert.equal(browser.listeners.get('focus')?.size ?? 0, 0)
  assert.equal(browser.listeners.get('online')?.size ?? 0, 0)
  assert.equal(doc.listeners.get('visibilitychange')?.size ?? 0, 0)
})
