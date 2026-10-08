// REV-041: pure decision logic for the Knowledge Base sync-state card.
//
// The atomic retry endpoint returns a typed outcome. When its final durable
// write did not land (or it otherwise reports a non-SYNCED status), the
// persisted `knowledge_sync_state` row can be stale — for example an old
// `SYNCED`. The UI must keep showing the *current* failure and its recovery
// action until a durable refresh is consistent again. This module isolates that
// decision so it can be unit-tested deterministically without a component
// runner.

import type { KnowledgeRetryOutcome, VaultSyncState } from './api'

export type KnowledgeSyncStatus = VaultSyncState['status']

export type KnowledgeSyncView = {
  /** Status the card should render (from the transient outcome when present). */
  status: KnowledgeSyncStatus
  /** Safe message shown for FAILED / INDEX_FAILED states, if any. */
  summary: string | null
  /** Recovery action for the current status. */
  recovery: 'retry-index' | 'retry-vault' | null
}

/** The retry outcome plus the stale durable row it was computed against. */
export type RetryOverride = {
  outcome: KnowledgeRetryOutcome
  /**
   * `updated_at` of the persisted row observed when the retry reported a
   * persistence-unavailable / non-SYNCED outcome. A durable refresh only counts
   * as recovered when it presents a *different* `updated_at` (a genuine new
   * write), so the very row that was stale cannot clear the override.
   */
  staleUpdatedAt: string | null
}

function recoveryFor(status: KnowledgeSyncStatus): KnowledgeSyncView['recovery'] {
  if (status === 'SYNCED') return null
  if (status === 'INDEX_FAILED') return 'retry-index'
  // FAILED and SKIPPED both use the manual vault-sync recovery, matching the
  // prior card behavior (a recovery control for every non-SYNCED state).
  return 'retry-vault'
}

/**
 * Resolve the sync-state card view from the persisted row and an optional
 * transient retry override.
 *
 * The override takes precedence whenever it is present: it represents the
 * outcome of the retry the user just performed, and must not be hidden by a
 * stale persisted row. Counts/timestamps are never taken from it — the caller
 * still renders those only from the persisted row.
 */
export function resolveKnowledgeSyncView(
  persisted: VaultSyncState | null | undefined,
  override: RetryOverride | null | undefined,
): KnowledgeSyncView | null {
  if (override) {
    return {
      status: override.outcome.status,
      summary: override.outcome.message,
      recovery: recoveryFor(override.outcome.status),
    }
  }
  if (!persisted) return null
  return {
    status: persisted.status,
    summary: persisted.error_summary,
    recovery: recoveryFor(persisted.status),
  }
}

/**
 * Whether a transient retry override should be cleared now that a durable row
 * is available: it is cleared only once a *genuine* durable refresh presents a
 * state consistent with the retry — either the durable state recovered to
 * SYNCED, or it now shows the same terminal status the retry reported.
 *
 * A row is only considered "refreshed" when its `updated_at` differs from the
 * stale value observed at retry time (or none was observed). This prevents the
 * pre-existing stale `SYNCED` row from immediately clearing the override — the
 * exact REV-041 failure.
 */
export function isRetryOverrideResolved(
  override: RetryOverride | null | undefined,
  persisted: VaultSyncState | null | undefined,
): boolean {
  if (!override) return true
  const status = persisted?.status
  if (!status) return false
  const refreshed = persisted?.updated_at !== override.staleUpdatedAt
  if (!refreshed) return false
  return status === 'SYNCED' || status === override.outcome.status
}

