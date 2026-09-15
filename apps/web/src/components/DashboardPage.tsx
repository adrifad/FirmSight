import { useEffect, useState } from 'react'
import { BrainCircuit, ChevronRight, CircleAlert, FolderKanban, LoaderCircle, Play, Plus, RefreshCcw, Trash2 } from 'lucide-react'
import { api, type Finding, type Memory, type Project } from '../api'
import type { Page } from './AppSidebar'
import { MetricCard } from './MetricCard'
import { Empty, ErrorState } from './ui'

/* Findings and Engineering Memory load independently so a failure in one
   aggregate never renders as a zero count or an empty state for the other. */
type FindingsState = { loading: boolean; error: string; findings: Finding[] }
type MemoriesState = { loading: boolean; error: string; insights: { memory: Memory; project: Project }[] }

/* Initial state is "loading": projects are already known when this component
   renders, so aggregates are pending, not zero. */
const initialFindings: FindingsState = { loading: true, error: '', findings: [] }
const initialMemories: MemoriesState = { loading: true, error: '', insights: [] }
const idleFindings: FindingsState = { loading: false, error: '', findings: [] }
const idleMemories: MemoriesState = { loading: false, error: '', insights: [] }

function MetricPending({ label }: { label: string }) {
  return <article className="metric-card pending" aria-busy="true"><p>{label}</p><strong><LoaderCircle className="spin" size={22} /></strong><span>Loading workspace data…</span></article>
}

function MetricFailed({ label, error, onRetry }: { label: string; error: string; onRetry: () => void }) {
  return <article className="metric-card failed"><p>{label}</p><strong><CircleAlert size={16} /> Unavailable</strong><span>{error}</span><button className="metric-retry" onClick={onRetry}><RefreshCcw size={14} /> Retry</button></article>
}

export function DashboardPage({ projects, loading, onOpen, onRunReview, onCreate, onDelete, navigate }: {
  projects: Project[] | null
  loading: boolean
  onOpen: (projectId: string) => void
  onRunReview: (projectId: string) => void
  onCreate: () => void
  onDelete: (project: Project) => void
  navigate: (page: Page) => void
}) {
  const [findingsState, setFindingsState] = useState<FindingsState>(initialFindings)
  const [findingsReload, setFindingsReload] = useState(0)
  const [memoriesState, setMemoriesState] = useState<MemoriesState>(initialMemories)
  const [memoriesReload, setMemoriesReload] = useState(0)

  useEffect(() => {
    if (!projects || projects.length === 0) { setFindingsState(idleFindings); return }
    let stale = false
    setFindingsState(current => ({ ...current, loading: true, error: '' }))
    void (async () => {
      try {
        const perProject = await Promise.all(projects.map(project => api.findings(project.id)))
        if (stale) return
        setFindingsState({ loading: false, error: '', findings: perProject.flat() })
      } catch (err) {
        if (stale) return
        setFindingsState({ loading: false, error: err instanceof Error ? err.message : 'FirmSight could not load project findings.', findings: [] })
      }
    })()
    return () => { stale = true }
  }, [projects, findingsReload])

  useEffect(() => {
    if (!projects || projects.length === 0) { setMemoriesState(idleMemories); return }
    let stale = false
    setMemoriesState(current => ({ ...current, loading: true, error: '' }))
    void (async () => {
      try {
        const perProject = await Promise.all(projects.map(project => api.memories(project.id)))
        if (stale) return
        setMemoriesState({ loading: false, error: '', insights: perProject.flatMap((memories, index) => memories.map(memory => ({ memory, project: projects[index] }))) })
      } catch (err) {
        if (stale) return
        setMemoriesState({ loading: false, error: err instanceof Error ? err.message : 'FirmSight could not load engineering memory.', insights: [] })
      }
    })()
    return () => { stale = true }
  }, [projects, memoriesReload])

  if (loading && !projects) return <div className="loading" role="status"><LoaderCircle className="spin" size={17} /> Loading workspace…</div>
  if (!projects) return null
  if (projects.length === 0) return <Empty title="No firmware projects yet." detail="Import a local project directory to see workspace review status, findings, and engineering insights on this dashboard." action={<button className="primary" onClick={onCreate}><Plus size={16} /> Import Project</button>} icon={<FolderKanban size={27} />} />

  const openCriticalHigh = findingsState.findings.filter(finding => (finding.severity === 'critical' || finding.severity === 'high') && finding.resolution !== 'SOLVED').length
  const indexedFiles = projects.reduce((total, project) => total + project.file_count, 0)
  const insights = memoriesState.insights.slice(0, 6)

  return <section className="page-stack">
    <div className="metric-grid">
      <MetricCard label="Active projects" value={projects.length} detail="Imported local firmware projects" />
      <MetricCard label="Indexed source files" value={indexedFiles} detail="Across all project source indexes" />
      {findingsState.loading
        ? <MetricPending label="Open critical / high findings" />
        : findingsState.error
          ? <MetricFailed label="Open critical / high findings" error={findingsState.error} onRetry={() => setFindingsReload(key => key + 1)} />
          : <MetricCard label="Open critical / high findings" value={openCriticalHigh} detail="Verifier-approved, not yet solved" tone={openCriticalHigh > 0 ? 'attention' : 'default'} />}
      {memoriesState.loading
        ? <MetricPending label="Saved engineering insights" />
        : memoriesState.error
          ? <MetricFailed label="Saved engineering insights" error={memoriesState.error} onRetry={() => setMemoriesReload(key => key + 1)} />
          : <MetricCard label="Saved engineering insights" value={memoriesState.insights.length} detail="Engineer-approved Engineering Memory" />}
    </div>
    <div className="dashboard-layout">
      <div className="dashboard-main">
        <section className="panel workspace-panel">
          <div className="section-head">
            <div><h2>Projects</h2><p>{projects.length} imported firmware project{projects.length === 1 ? '' : 's'}. Open a project workspace or start a review.</p></div>
            <button onClick={() => navigate('projects')}>All projects <ChevronRight size={15} /></button>
          </div>
          <div className="workspace-projects">
            {projects.map(project => <article key={project.id} className="workspace-project">
              <div className="workspace-project-main">
                <div className="workspace-project-title">
                  <strong>{project.name}</strong>
                  {(project.framework || project.target) && <span className="source-tag">{project.framework ?? 'Framework not detected'}{project.target ? ` · ${project.target}` : ''}</span>}
                </div>
                {project.description && <p>{project.description}</p>}
                <small>{project.file_count} files · {project.symbol_count} symbols</small>
              </div>
              <div className="workspace-project-actions">
                <button onClick={() => onOpen(project.id)}>Open <ChevronRight size={15} /></button>
                <button className="primary" disabled={!project.file_count} onClick={() => onRunReview(project.id)} title={project.file_count ? undefined : 'Import source files before running a review.'}><Play size={15} /> Run Review</button>
                <button className="delete-action" onClick={() => onDelete(project)}><Trash2 size={15} /> Delete</button>
              </div>
            </article>)}
          </div>
        </section>
      </div>
      <aside className="dashboard-side">
        <section className="panel insights-panel">
          <div className="section-head"><div><h2>Engineering insights</h2><p>Saved Engineering Memory across all projects.</p></div><BrainCircuit size={17} /></div>
          {memoriesState.loading
            ? <div className="loading compact" role="status"><LoaderCircle className="spin" size={16} /> Loading engineering memory…</div>
            : memoriesState.error
              ? <ErrorState error={memoriesState.error} retry={() => setMemoriesReload(key => key + 1)} />
              : insights.length > 0
                ? <div className="insight-list">{insights.map(({ memory, project }) => <article key={`${project.id}:${memory.id}`} className="insight-item"><p>{memory.statement}</p><small>{project.name} · {memory.type.replaceAll('_', ' ').toLowerCase()}</small></article>)}</div>
                : <Empty compact title="No saved engineering insights yet." detail="When you approve a lesson from a finding decision, it will appear here." icon={<BrainCircuit size={24} />} />}
        </section>
      </aside>
    </div>
  </section>
}
