export type Project = { id: string; name: string; description: string; source_type: string; language: string | null; framework: string | null; target: string | null; build_system: string | null; file_count: number; symbol_count: number; source_sync_available: boolean }
export type ProjectFile = { path: string; language: string; size: number; content?: string }
export type ProjectSourceSync = { project_id: string; file_count: number; symbol_count: number; language: string | null; framework: string | null; target: string | null; build_system: string | null; changed_files: string[] }
export type FixVerificationEvidence = { file: string; line: number; symbol?: string | null; evidence_snippet: string; description: string }
export type FixVerificationPathEdge = { source: string; relation: string; target: string; file?: string | null; line?: number | null; relation_state?: string | null; source_id?: string | null; target_id?: string | null }
export type FixPathCoverage = { original_entry: string; original_path: string[]; status: 'MITIGATED' | 'REMOVED' | 'REDIRECTED_SAFE' | 'STILL_UNSAFE' | 'UNRESOLVED'; current_path_edges: FixVerificationPathEdge[]; evidence: FixVerificationEvidence[]; note: string }
export type FixVerificationReport = {
  verdict: 'FIXED' | 'STILL_PRESENT' | 'INCONCLUSIVE'
  original_failure_condition: string
  original_execution_path: string[]
  current_execution_path: string[]
  current_path_edges: FixVerificationPathEdge[]
  original_path_coverage?: FixPathCoverage[]
  mitigations_found: FixVerificationEvidence[]
  remaining_failure_evidence: FixVerificationEvidence[]
  inspected_files: string[]
  inspected_symbols: string[]
  missing_context: string[]
  alternative_mitigation: boolean
  confidence: number
  reasoning_summary: string
  finding_baseline_snapshot_hash?: string | null
  pre_refresh_snapshot_hash?: string | null
  current_snapshot_hash?: string | null
  model_verdict?: 'FIXED' | 'STILL_PRESENT' | 'INCONCLUSIVE' | null
  validation_status?: 'UNVALIDATED' | 'VALIDATED' | 'DOWNGRADED'
  validation_reasons?: string[]
  notes?: string | null
}
export type FindingRemediation = { status: 'UNVERIFIED' | 'VERIFIED_FIXED' | 'STILL_PRESENT' | 'INCONCLUSIVE' | 'MANUALLY_MARKED'; notes: string; verified_at?: string | null; source_refreshed: boolean; changed_files: string[]; baseline_snapshot_hash?: string | null; current_snapshot_hash?: string | null; verification?: FixVerificationReport | null }
export type Finding = { id: string; project_id: string; title: string; classification: string; severity: string; category: string; confidence: number; location: { file: string; function?: string; line_start: number; line_end: number }; summary: string; evidence: { description: string; file: string; line: number }[]; execution_path: string[]; runtime_scenario: string; impact: string; assumptions: { statement: string; status: string }[]; recommendation: string; verification: { status: string; notes: string }; decision: string; decision_reason?: string; resolution: 'OPEN' | 'SOLVED'; resolved_at?: string | null; remediation: FindingRemediation }
export type ReviewUsage = { prompt_tokens?: number | null; completion_tokens?: number | null; total_tokens?: number | null; reasoning_tokens?: number | null }
export type ReviewDiagnostic = { id: number; review_id: string; request_id: string; operation: string; role: string; batch_number?: number | null; total_batches?: number | null; file_count: number; state: string; attempt: number; execution_attempt?: number; repair_attempted: boolean; provider: string; model: string; endpoint: string; created_at: string; elapsed_ms?: number | null; http_status?: number | null; content_type?: string | null; request_chars?: number | null; response_chars?: number | null; usage?: ReviewUsage | null; error_kind?: string | null; error_message?: string | null; content_state?: string | null; finish_reason?: string | null; validation_category?: string | null; validation_fields?: string[]; retry_suppressed?: boolean }
export type ReviewExecutionProgress = { phase: string; total_units: number; completed_units: number; reused_units: number; unavailable_units: number; in_flight_requests: number; parallel_request_limit: number; current_units: string[] }
export type ReviewOutputBudgetSnapshot = { investigator: OutputBudget; verifier: OutputBudget; verifier_fix: number }
export type Review = { id: string; project_id: string; status: string; progress: string[]; finding_count: number; scope: string; focus: string[]; context_files: string[]; context_chars: number; source_snapshot_hash?: string | null; total_batches: number; validated_batches: number; unavailable_batches: number; execution_attempt?: number; execution_progress?: ReviewExecutionProgress; last_activity_at?: string | null; diagnostics?: ReviewDiagnostic[]; output_budget_snapshot: ReviewOutputBudgetSnapshot; error?: string | null }
export type ChatMessage = { id: string; role: string; content: string; created_at: string }
export type Memory = { id: string; type: string; statement: string; status: string; scope: { type: string; symbol?: string; component?: string }; evidence: { symbol: string }[] }
export type IntelligenceLink = { id: number; memory_id: string; link_kind: string; link_value: string; role: string; created_at: string }
export type IntelligenceEvidence = { id: number; memory_id: string; kind: string; file: string | null; line: number | null; symbol: string | null; file_hash: string | null; description: string | null; created_at: string }
export type IntelligenceObservation = { id: number; memory_id: string; kind: string; from_state: string | null; to_state: string | null; confidence: number | null; confidence_delta: number | null; fingerprint: string | null; review_id: string | null; finding_id: string | null; chat_message_id: string | null; detail: string | null; created_at: string }
export type IntelligenceConflict = { id: number; memory_id: string; evidence: Record<string, unknown>; snapshot: Record<string, unknown>; resolution_state: string; detail: string | null; created_at: string; resolved_at: string | null }
export type IntelligenceRecord = {
  id: string; project_id: string; type: string; statement: string; state: string; status: string;
  confidence: number; origin: string; observation_count: number; reinforcement_count: number;
  superseded_by: string | null; conflict_summary: string | null; scope: { type: string; symbol?: string; component?: string };
  links: IntelligenceLink[]; evidence_count: number; last_evidence: IntelligenceEvidence[];
  first_observed_at: string | null; last_observed_at: string | null; last_validated_at: string | null;
  last_validated_commit: string | null; created_at: string; updated_at: string
}
export type IntelligenceDetail = IntelligenceRecord & { observations: IntelligenceObservation[]; evidence: IntelligenceEvidence[]; conflicts: IntelligenceConflict[] }
export type IntelligenceCounts = { total: number; provisional: number; reinforced: number; verified: number; needs_revalidation: number; conflicted: number; superseded: number; disabled: number; by_type: Record<string, number> }
export type IntelligenceList = { records: IntelligenceRecord[]; counts: IntelligenceCounts }
export type IntelligenceSummary = { counts: IntelligenceCounts; health: { open_conflicts: number; stale_records: number; last_job_status: string | null; last_job_at: string | null } }
export type LearningSummary = { review_id: string; project_id: string; job_id: string | null; status: string; provisional: number; reinforced: number; verified: number; needs_revalidation: number; conflicted: number; superseded: number; rejected: number; error: string | null; updated_at: string | null }
export type YamlResult = { project_id: string; content: string; valid: boolean; errors: string[] }
export type Symbol = { name: string; kind: string; file: string; line: number }
export type OutputBudget = number | 'PROVIDER_DEFAULT'
export type AISettings = { provider: string; endpoint: string; api_key_configured: boolean; api_key_masked: string | null; api_key_environment: string; models: Record<string, string>; review_context_chars: number; structured_output_mode: 'PROMPT_ONLY' | 'JSON_OBJECT' | 'JSON_SCHEMA'; reasoning_effort: 'UNSPECIFIED' | 'LOW' | 'MEDIUM' | 'HIGH'; investigator_max_tokens: OutputBudget; verifier_max_tokens: OutputBudget; review_parallel_requests: number }
export type VaultSyncStatus = 'SYNCED' | 'FAILED' | 'INDEX_FAILED' | 'SKIPPED'
export type VaultSyncState = { project_id: string; status: VaultSyncStatus; counts: Record<string, number>; error_summary: string | null; updated_at: string }
export type KnowledgeRetryOutcome = { project_id: string; status: VaultSyncStatus; vault: 'SYNCED' | 'FAILED' | 'SKIPPED' | 'UNAVAILABLE'; index: 'SYNCED' | 'INDEX_FAILED' | 'SKIPPED' | 'NOT_RUN'; message: string }

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json', ...(options.headers ?? {}) }, ...options })
  const body = response.status === 204 ? '' : await response.text()
  let data: { detail?: string } | null = null
  if (body) {
    try { data = JSON.parse(body) as { detail?: string } } catch { throw new Error(`FirmSight received an invalid response from ${path} (HTTP ${response.status}). Check that the FastAPI backend is running and up to date.`) }
  }
  if (!response.ok) throw new Error(data?.detail ?? `FirmSight could not complete the request (HTTP ${response.status}).`)
  return data as T
}
const projectPath = (id: string) => `/api/projects/${encodeURIComponent(id)}`
const buildQuery = (params: Record<string, string | undefined>) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value) search.set(key, value)
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  projects: () => request<Project[]>('/api/projects'),
  createProject: (name: string, description: string) => request<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name, description, source_type: 'MANUAL' }) }),
  importDirectory: (directory: string, name?: string, description = '') => request<Project>('/api/projects/import-directory', { method: 'POST', body: JSON.stringify({ directory, name: name || undefined, description }) }),
  deleteProject: (id: string) => request<void>(projectPath(id), { method: 'DELETE' }),
  files: (id: string) => request<ProjectFile[]>(`${projectPath(id)}/files`),
  file: (id: string, path: string) => request<ProjectFile>(`${projectPath(id)}/files/content?path=${encodeURIComponent(path)}`),
  index: (id: string) => request(`${projectPath(id)}/index`, { method: 'POST' }),
  syncSource: (id: string, directory?: string) => request<ProjectSourceSync>(`${projectPath(id)}/sync-source`, { method: 'POST', body: JSON.stringify(directory ? { directory } : {}) }),
  symbols: (id: string) => request<Symbol[]>(`${projectPath(id)}/symbols`),
  review: (id: string, scope = 'Full Project', focus = ['memory', 'concurrency', 'freertos', 'error_handling']) => request<Review>(`${projectPath(id)}/reviews`, { method: 'POST', body: JSON.stringify({ scope, focus }) }),
  reviewStatus: (id: string, reviewId: string) => request<Review>(`${projectPath(id)}/reviews/${encodeURIComponent(reviewId)}`),
  retryReview: (id: string, reviewId: string) => request<Review>(`${projectPath(id)}/reviews/${encodeURIComponent(reviewId)}/retry`, { method: 'POST' }),
  findings: (id: string) => request<Finding[]>(`${projectPath(id)}/findings`),
  finding: (id: string, findingId: string) => request<Finding>(`${projectPath(id)}/findings/${findingId}`),
  decide: (id: string, findingId: string, decision: string, reason?: string) => request<Finding>(`${projectPath(id)}/findings/${findingId}/decision`, { method: 'PATCH', body: JSON.stringify({ decision, reason, propose_memory: false }) }),
  resolve: (id: string, findingId: string, resolution: 'OPEN' | 'SOLVED') => request<Finding>(`${projectPath(id)}/findings/${findingId}/resolution`, { method: 'PATCH', body: JSON.stringify({ resolution }) }),
  verifyFix: (id: string, findingId: string, directory?: string) => request<Finding>(`${projectPath(id)}/findings/${findingId}/verify-fix`, { method: 'POST', body: JSON.stringify(directory ? { directory } : {}) }),
  messages: (id: string) => request<ChatMessage[]>(`${projectPath(id)}/chat`),
  chat: (id: string, message: string, selected_file?: string, finding_id?: string) => request<{ message: ChatMessage; context_summary: string[] }>(`${projectPath(id)}/chat`, { method: 'POST', body: JSON.stringify({ message, selected_file, finding_id }) }),
  memories: (id: string) => request<Memory[]>(`${projectPath(id)}/memory`),
  intelligence: (id: string, state?: string, type?: string) => request<IntelligenceList>(`${projectPath(id)}/intelligence${buildQuery({ state, type })}`),
  intelligenceSummary: (id: string) => request<IntelligenceSummary>(`${projectPath(id)}/intelligence/summary`),
  intelligenceDetail: (memoryId: string) => request<IntelligenceDetail>(`/api/intelligence/${encodeURIComponent(memoryId)}`),
  revalidateIntelligence: (memoryId: string) => request<{ record: IntelligenceRecord; outcome: string; detail: string }>(`/api/intelligence/${encodeURIComponent(memoryId)}/revalidate`, { method: 'POST' }),
  disableIntelligence: (memoryId: string, reason?: string) => request<IntelligenceRecord>(`/api/intelligence/${encodeURIComponent(memoryId)}/disable`, { method: 'POST', body: JSON.stringify({ reason: reason ?? null }) }),
  learningSummary: (id: string, reviewId: string) => request<LearningSummary>(`${projectPath(id)}/reviews/${encodeURIComponent(reviewId)}/learning-summary`),
  proposeMemory: (id: string, statement: string, symbol: string, note: string) => request<{ id: string }>(`${projectPath(id)}/memory/proposals`, { method: 'POST', body: JSON.stringify({ type: 'LESSON_LEARNED', statement, scope: { type: 'PROJECT' }, evidence: [{ symbol }], source: { type: 'ENGINEER_AUTHORED', engineer_note: note }, proposed_by: 'ENGINEER' }) }),
  updateMemoryProposal: (proposalId: string, statement: string, symbol: string, note: string) => request<{ id: string }>(`/api/memory/proposals/${proposalId}`, { method: 'PATCH', body: JSON.stringify({ statement, evidence: [{ symbol }], source: { type: 'ENGINEER_AUTHORED', engineer_note: note } }) }),
  approveMemory: (proposalId: string) => request<Memory>(`/api/memory/proposals/${proposalId}/approve`, { method: 'POST', body: JSON.stringify({ approved_by: 'Engineer' }) }),
  ignoreMemoryProposal: (proposalId: string) => request<void>(`/api/memory/proposals/${proposalId}`, { method: 'DELETE' }),
  yaml: (id: string) => request<YamlResult | null>(`${projectPath(id)}/yaml`),
  generateYaml: (id: string, description: string, focus: string[]) => request<YamlResult>(`${projectPath(id)}/yaml/generate`, { method: 'POST', body: JSON.stringify({ description, focus }) }),
  validateYaml: (id: string, content: string) => request<YamlResult>(`${projectPath(id)}/yaml/validate`, { method: 'POST', body: JSON.stringify({ content }) }),
  settings: () => request<AISettings>('/api/settings'),
  updateSettings: (provider: string, endpoint: string, models: Record<string, string>, review_context_chars: number, structured_output_mode: 'PROMPT_ONLY' | 'JSON_OBJECT' | 'JSON_SCHEMA', reasoning_effort: 'UNSPECIFIED' | 'LOW' | 'MEDIUM' | 'HIGH', investigator_max_tokens: OutputBudget, verifier_max_tokens: OutputBudget, review_parallel_requests = 2) => request<AISettings>('/api/settings', { method: 'PUT', body: JSON.stringify({ provider, endpoint, models, review_context_chars, structured_output_mode, reasoning_effort, investigator_max_tokens, verifier_max_tokens, review_parallel_requests }) }),
}
