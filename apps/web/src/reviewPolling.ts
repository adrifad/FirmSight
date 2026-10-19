import type { Review } from './api'

export const ACTIVE_REVIEW_STORAGE_KEY = 'firmsight.activeReview.v1'
const INITIAL_RETRY_MS = 1_000
const MAX_RETRY_MS = 30_000

export function reviewSessionStorage(): ReviewStorage {
  try { return window.sessionStorage } catch {
    return { getItem: () => null, setItem: () => undefined, removeItem: () => undefined }
  }
}

export type ActiveReview = { projectId: string; reviewId: string }
export type ReviewStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>
type PollWindow = Pick<Window, 'addEventListener' | 'removeEventListener' | 'setTimeout' | 'clearTimeout'>
type PollDocument = Pick<Document, 'addEventListener' | 'removeEventListener' | 'visibilityState'>

export function persistActiveReview(storage: ReviewStorage, active: ActiveReview): void {
  try { storage.setItem(ACTIVE_REVIEW_STORAGE_KEY, JSON.stringify(active)) } catch { /* Recovery is best-effort when storage is unavailable. */ }
}

export function readActiveReview(storage: ReviewStorage, projectId: string): ActiveReview | null {
  try {
    const raw = storage.getItem(ACTIVE_REVIEW_STORAGE_KEY)
    if (!raw) return null
    const value = JSON.parse(raw) as Partial<ActiveReview>
    if (typeof value.projectId !== 'string' || typeof value.reviewId !== 'string' || !value.projectId || !value.reviewId) return null
    return value.projectId === projectId ? { projectId: value.projectId, reviewId: value.reviewId } : null
  } catch { return null }
}

export function clearActiveReview(storage: ReviewStorage, active: ActiveReview): void {
  try {
    const raw = storage.getItem(ACTIVE_REVIEW_STORAGE_KEY)
    if (!raw) return
    const value = JSON.parse(raw) as Partial<ActiveReview>
    if (value.projectId === active.projectId && value.reviewId === active.reviewId) storage.removeItem(ACTIVE_REVIEW_STORAGE_KEY)
  } catch { /* A later page load can retry cleanup if storage recovers. */ }
}

export function isTerminalReviewStatus(status: string): boolean {
  return status === 'COMPLETED' || status === 'PARTIAL' || status === 'FAILED' || status === 'INTERRUPTED'
}

export function startReviewPolling(options: {
  active: ActiveReview
  fetchStatus: () => Promise<Review>
  onStatus: (review: Review) => void
  onConnectionChange: (connected: boolean) => void
  storage: ReviewStorage
  browserWindow?: PollWindow
  browserDocument?: PollDocument
}): () => void {
  const browserWindow = options.browserWindow ?? window
  const browserDocument = options.browserDocument ?? document
  let stopped = false
  let inFlight = false
  let queuedImmediatePoll = false
  let timer: number | undefined
  let retryDelay = INITIAL_RETRY_MS

  const clearTimer = () => {
    if (timer !== undefined) browserWindow.clearTimeout(timer)
    timer = undefined
  }
  const schedule = (delay: number) => {
    clearTimer()
    if (!stopped) timer = browserWindow.setTimeout(() => { timer = undefined; void poll() }, delay)
  }
  const poll = async () => {
    if (stopped || inFlight) return
    inFlight = true
    try {
      const review = await options.fetchStatus()
      if (stopped) return
      retryDelay = INITIAL_RETRY_MS
      options.onConnectionChange(true)
      options.onStatus(review)
      if (isTerminalReviewStatus(review.status)) {
        clearActiveReview(options.storage, options.active)
        clearTimer()
        stopped = true
        return
      }
      schedule(INITIAL_RETRY_MS)
    } catch {
      if (stopped) return
      options.onConnectionChange(false)
      schedule(retryDelay)
      retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS)
    } finally {
      inFlight = false
      if (!stopped && queuedImmediatePoll) {
        queuedImmediatePoll = false
        clearTimer()
        void poll()
      }
    }
  }
  const recover = () => {
    if (stopped) return
    clearTimer()
    if (inFlight) { queuedImmediatePoll = true; return }
    void poll()
  }
  const onVisibilityChange = () => { if (browserDocument.visibilityState === 'visible') recover() }
  browserWindow.addEventListener('focus', recover)
  browserWindow.addEventListener('online', recover)
  browserDocument.addEventListener('visibilitychange', onVisibilityChange)
  void poll()

  return () => {
    stopped = true
    clearTimer()
    browserWindow.removeEventListener('focus', recover)
    browserWindow.removeEventListener('online', recover)
    browserDocument.removeEventListener('visibilitychange', onVisibilityChange)
  }
}
