// REV-041: deterministic coverage for the Knowledge Base sync-state card.
//
// The web project has no component runner, so this exercises the pure
// decision logic the card uses (`resolveKnowledgeSyncView` /
// `isRetryOverrideResolved`) with Node's built-in test runner and native
// TypeScript type stripping. Run from `apps/web`:
//
//   node --test src/__tests__/knowledgeSyncView.test.ts
//
// It proves the acceptance condition: a stale persisted `SYNCED` row plus a
// retry response `{ status: 'INDEX_FAILED', ... }` still presents the safe
// error and the `Retry knowledge index` recovery action, and a later durable
// `SYNCED` refresh clears the transient override.

import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isRetryOverrideResolved, resolveKnowledgeSyncView } from '../knowledgeSyncView.ts'
import type { RetryOverride } from '../knowledgeSyncView.ts'
import type { KnowledgeRetryOutcome, VaultSyncState } from '../api.ts'

const STALE_UPDATED_AT = '2026-01-01T00:00:00+00:00'

const STALE_SYNCED_ROW: VaultSyncState = {
  project_id: 'PRJ-1',
  status: 'SYNCED',
  counts: { documents: 4, updated: 3, unchanged: 1 },
  error_summary: null,
  updated_at: STALE_UPDATED_AT,
}

const INDEX_FAILED_OUTCOME: KnowledgeRetryOutcome = {
  project_id: 'PRJ-1',
  status: 'INDEX_FAILED',
  vault: 'SYNCED',
  index: 'INDEX_FAILED',
  message: 'Knowledge index unavailable; vault projection remains available. Retry from the Knowledge Base.',
}

const INDEX_FAILED_OVERRIDE: RetryOverride = { outcome: INDEX_FAILED_OUTCOME, staleUpdatedAt: STALE_UPDATED_AT }

test('stale SYNCED row plus INDEX_FAILED retry still presents the safe error and retry-index', () => {
  const view = resolveKnowledgeSyncView(STALE_SYNCED_ROW, INDEX_FAILED_OVERRIDE)
  assert.ok(view)
  assert.equal(view?.status, 'INDEX_FAILED')
  assert.equal(view?.summary, INDEX_FAILED_OUTCOME.message)
  assert.equal(view?.recovery, 'retry-index')
})

test('transient FAILED retry offers retry-vault recovery', () => {
  const view = resolveKnowledgeSyncView(STALE_SYNCED_ROW, {
    outcome: {
      project_id: 'PRJ-1',
      status: 'FAILED',
      vault: 'FAILED',
      index: 'NOT_RUN',
      message: 'Knowledge vault projection could not complete safely; no index was attempted. Retry vault sync from the Knowledge Base.',
    },
    staleUpdatedAt: STALE_UPDATED_AT,
  })
  assert.equal(view?.status, 'FAILED')
  assert.equal(view?.recovery, 'retry-vault')
})

test('with no transient override, a FAILED persisted row drives the retry-vault control', () => {
  const failed: VaultSyncState = { ...STALE_SYNCED_ROW, status: 'FAILED', error_summary: 'safe summary' }
  const view = resolveKnowledgeSyncView(failed, null)
  assert.equal(view?.status, 'FAILED')
  assert.equal(view?.summary, 'safe summary')
  assert.equal(view?.recovery, 'retry-vault')
})

test('a durable SYNCED row with no override shows no recovery control', () => {
  const view = resolveKnowledgeSyncView(STALE_SYNCED_ROW, null)
  assert.equal(view?.status, 'SYNCED')
  assert.equal(view?.recovery, null)
})

test('no persisted row and no override yields no view', () => {
  assert.equal(resolveKnowledgeSyncView(null, null), null)
})

test('the transient override is retained while the durable row stays the same stale SYNCED', () => {
  assert.equal(isRetryOverrideResolved(INDEX_FAILED_OVERRIDE, STALE_SYNCED_ROW), false)
})

test('a later durable SYNCED refresh (new updated_at) clears the transient override', () => {
  const refreshed: VaultSyncState = { ...STALE_SYNCED_ROW, updated_at: '2026-01-02T00:00:00+00:00' }
  assert.equal(isRetryOverrideResolved(INDEX_FAILED_OVERRIDE, refreshed), true)
})

test('a durable row matching the retry status clears the transient override', () => {
  const recovered: VaultSyncState = {
    ...STALE_SYNCED_ROW,
    status: 'INDEX_FAILED',
    error_summary: INDEX_FAILED_OUTCOME.message,
    updated_at: '2026-01-02T00:00:00+00:00',
  }
  assert.equal(isRetryOverrideResolved(INDEX_FAILED_OVERRIDE, recovered), true)
})

test('a refreshed row with an unrelated status does not clear the override', () => {
  const skipped: VaultSyncState = { ...STALE_SYNCED_ROW, status: 'SKIPPED', updated_at: '2026-01-02T00:00:00+00:00' }
  assert.equal(isRetryOverrideResolved(INDEX_FAILED_OVERRIDE, skipped), false)
})

test('no transient override is trivially resolved', () => {
  assert.equal(isRetryOverrideResolved(null, STALE_SYNCED_ROW), true)
})
