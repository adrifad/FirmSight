import { type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import Editor from '@monaco-editor/react'
import { BadgeCheck, BrainCircuit, Check, ChevronRight, CircleAlert, Code2, FileCode2, FolderOpen, LoaderCircle, MessageSquareCode, Network, PanelLeft, Play, Plus, RefreshCcw, ScanSearch, Search, Settings2, ShieldCheck, Trash2, TriangleAlert, X } from 'lucide-react'
import { api, type ChatMessage, type Finding, type FixVerificationEvidence, type IntelligenceRecord, type LearningSummary, type OutputBudget, type Project, type ProjectFile, type Review, type ReviewDiagnostic, type Symbol, type YamlResult } from './api'
import { AppSidebar, DesktopNav, type Page } from './components/AppSidebar'
import logoUrl from './assets/firmsight-logo.png'
import { DashboardPage } from './components/DashboardPage'
import { MetricCard } from './components/MetricCard'
import { Empty, ErrorState, Loading } from './components/ui'

const globalPages: Page[] = ['dashboard', 'projects', 'settings']
const pageLabels: Record<Page, string> = { dashboard: 'Dashboard', projects: 'Projects', overview: 'Overview', code: 'Code', review: 'AI Review', findings: 'Findings', chat: 'AI Chat', architecture: 'Architecture', memory: 'Project Intelligence', yaml: 'YAML Generator', settings: 'Settings' }

function useAsync<T>(loader: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState(''); const [loading, setLoading] = useState(false)
  const run = async () => { setLoading(true); setError(''); try { setData(await loader()) } catch (err) { setError(err instanceof Error ? err.message : 'Unexpected error') } finally { setLoading(false) } }
  useEffect(() => { void run() }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return { data, error, loading, run, setData }
}
function Severity({ finding }: { finding: Finding }) { return <span className={`severity ${finding.severity}`}><CircleAlert size={13} /> {finding.severity.toUpperCase()}</span> }

export default function App() {
  const projects = useAsync(api.projects, []); const [activeId, setActiveId] = useState(''); const [page, setPage] = useState<Page>('projects'); const [toast, setToast] = useState(''); const [showCreate, setShowCreate] = useState(false); const [sidebarOpen, setSidebarOpen] = useState(false); const [deleteTarget, setDeleteTarget] = useState<Project | null>(null)
  const active = useMemo(() => projects.data?.find(project => project.id === activeId) ?? projects.data?.[0] ?? null, [projects.data, activeId])
  useEffect(() => { if (active && active.id !== activeId) setActiveId(active.id); if (!active && !globalPages.includes(page)) setPage('projects') }, [active, activeId, page])
  useEffect(() => {
    if (!sidebarOpen) return
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') setSidebarOpen(false) }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [sidebarOpen])
  const navigate = (destination: Page) => { if (!globalPages.includes(destination) && !active) { setPage('projects'); setToast('Create or import a project before opening project tools.'); return } setPage(destination) }
  const navigateFromSidebar = (destination: Page) => { setSidebarOpen(false); navigate(destination) }
  const openProject = (projectId: string) => { setActiveId(projectId); setPage('overview') }
  const runProjectReview = (projectId: string) => { setActiveId(projectId); setPage('review') }
  const projectDeleted = async (project: Project) => {
    // The DELETE already succeeded; re-fetch the list directly so a refresh
    // failure is observable by the dialog (useAsync.run() swallows loader
    // errors). Routing, active-project reset, and the success toast only
    // happen after the refreshed list actually arrives.
    try {
      const refreshed = await api.projects()
      projects.setData(refreshed)
    } catch (error) {
      void projects.run()
      throw error
    }
    setDeleteTarget(null)
    if (activeId === project.id) setActiveId('')
    setPage('projects')
    setToast(`${project.name} was deleted from FirmSight. Your local firmware directory was not touched.`)
  }
  const syncSource = async (projectId: string, directory?: string) => { const result = await api.syncSource(projectId, directory); await projects.run(); setToast(result.changed_files.length ? `Local source synced: ${result.changed_files.length} file${result.changed_files.length === 1 ? '' : 's'} changed.` : 'Local source is already up to date.'); return result }
  const onProjectPage = Boolean(active) && !globalPages.includes(page)
  const utilityName = active && onProjectPage ? active.name : 'FirmSight'
  const eyebrow = page === 'projects' || page === 'dashboard' ? 'WORKSPACE' : active && onProjectPage ? active.name.toUpperCase() : 'FIRMSIGHT'
  const showNewProject = page === 'projects' || (page === 'dashboard' && (projects.data?.length ?? 0) > 0)
  return <div className="app-shell">
    <AppSidebar page={page} active={active} open={sidebarOpen} onNavigate={navigateFromSidebar} onClose={() => setSidebarOpen(false)} />
    <div className="workspace">
      <header className="utility-bar">
        <button type="button" className="icon-button sidebar-toggle" aria-expanded={sidebarOpen} aria-controls="app-sidebar" aria-label={sidebarOpen ? 'Close navigation' : 'Open navigation'} onClick={() => setSidebarOpen(open => !open)}><PanelLeft size={17} /></button>
        <button type="button" className="desktop-brand" onClick={() => navigate('projects')} aria-label="FirmSight home"><img src={logoUrl} alt="FirmSight" height={26} width={95} /></button>
        <DesktopNav page={page} active={active} onNavigate={navigate} />
        <div className="utility-context"><strong>{utilityName}</strong><span aria-hidden="true">/</span><span>{pageLabels[page]}</span></div>
        {active && <select className="project-select" value={active.id} aria-label="Active project" onChange={event => { setActiveId(event.target.value); setPage('overview') }}>{projects.data?.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}</select>}
        <button type="button" className="icon-button" aria-label="Refresh projects" title="Reload stored project records" onClick={() => void projects.run()}><RefreshCcw size={16} /></button>
        <button type="button" className={page === 'settings' ? 'icon-button active-icon' : 'icon-button'} aria-label="Settings" title="Open settings" onClick={() => navigate('settings')}><Settings2 size={16} /></button>
      </header>
      <main className="content">
        <header className="page-header"><div><p className="eyebrow">{eyebrow}</p><h1>{pageLabels[page]}</h1></div><div className="top-actions">{showNewProject && <button className="primary" onClick={() => setShowCreate(true)}><Plus size={16} /> New Project</button>}{onProjectPage && page !== 'review' && <button className="primary" onClick={() => navigate('review')}><Play size={16} /> Run Review</button>}</div></header>
        {toast && <div className="toast"><Check size={16} />{toast}<button onClick={() => setToast('')} aria-label="Dismiss message"><X size={15} /></button></div>}{projects.error && <ErrorState error={projects.error} retry={projects.run} />}
        {page === 'dashboard' && <DashboardPage projects={projects.data} loading={projects.loading} onOpen={openProject} onRunReview={runProjectReview} onCreate={() => setShowCreate(true)} onDelete={project => setDeleteTarget(project)} navigate={navigate} />}
        {page === 'projects' && <ProjectsPage projects={projects.data ?? []} active={active} loading={projects.loading} onOpen={openProject} onCreate={() => setShowCreate(true)} refresh={projects.run} onDelete={project => setDeleteTarget(project)} />}{page === 'overview' && <OverviewPage project={active} navigate={navigate} />}{page === 'code' && <CodePage project={active} syncSource={syncSource} />}{page === 'review' && <ReviewPage project={active} navigate={navigate} notify={setToast} />}{page === 'findings' && <FindingsPage project={active} navigate={navigate} notify={setToast} />}{page === 'chat' && <ChatPage project={active} />}{page === 'architecture' && <ArchitecturePage project={active} />}{page === 'memory' && <ProjectIntelligencePage project={active} notify={setToast} />}{page === 'yaml' && <YamlPage project={active} notify={setToast} />}{page === 'settings' && <SettingsPage />}
      </main>
    </div>
    {showCreate && <CreateProjectDialog refresh={projects.run} onCreated={project => { setShowCreate(false); setActiveId(project.id); setPage('overview'); setToast(`${project.name} imported and indexed.`) }} onClose={() => setShowCreate(false)} />}
    {deleteTarget && <DeleteProjectDialog project={deleteTarget} onCancel={() => setDeleteTarget(null)} onDeleted={project => projectDeleted(project)} />}</div>
}

function DeleteProjectDialog({ project, onCancel, onDeleted }: { project: Project; onCancel: () => void; onDeleted: (project: Project) => Promise<void> }) {
  const [deleting, setDeleting] = useState(false); const [deleted, setDeleted] = useState(false); const [error, setError] = useState('')
  const actionRef = useRef<HTMLButtonElement>(null)
  useEffect(() => { if (error) actionRef.current?.focus() }, [error])
  const refreshList = async () => {
    setDeleting(true); setError('')
    try { await onDeleted(project) }
    catch { setError(`“${project.name}” was deleted from FirmSight, but the project list could not be refreshed. Retry refresh — the delete request will not be sent again.`) }
    finally { setDeleting(false) }
  }
  const confirm = async () => {
    if (deleting) return
    setDeleting(true); setError('')
    try { await api.deleteProject(project.id); setDeleted(true) }
    catch (err) { setError(err instanceof Error ? err.message : 'FirmSight could not delete this project. It remains fully intact; try again.'); setDeleting(false); return }
    setDeleting(false)
    await refreshList()
  }
  return <div className="modal-backdrop" role="presentation"><section className="create-dialog delete-dialog" role="dialog" aria-modal="true" aria-labelledby="delete-project-title"><div className="dialog-head"><div><p className="eyebrow">DESTRUCTIVE ACTION</p><h2 id="delete-project-title">Delete “{project.name}”?</h2></div><button className="icon-button" disabled={deleting} onClick={onCancel} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">This permanently deletes the FirmSight records stored for <strong>{project.name}</strong>: source index, reviews, findings and decisions, chat history, YAML generations, Project Intelligence knowledge, learning jobs, and review learning summaries. <strong>Your local firmware directory and its files are not deleted.</strong></p>{error && <div className="error"><CircleAlert size={16} />{error}</div>}<div className="dialog-actions"><button autoFocus={!error} disabled={deleting} onClick={onCancel}>{deleted ? 'Close' : 'Cancel'}</button>{deleted
    ? <button ref={actionRef} className="primary" disabled={deleting} onClick={() => void refreshList()}>{deleting ? <LoaderCircle className="spin" size={16} /> : <RefreshCcw size={16} />}{deleting ? 'Refreshing…' : 'Retry refresh'}</button>
    : <button ref={actionRef} className="reject-confirm" disabled={deleting} onClick={() => void confirm()}>{deleting ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}{deleting ? 'Deleting…' : 'Delete project'}</button>}</div></section></div> }

function CreateProjectDialog({ refresh, onCreated, onClose }: { refresh: () => Promise<void>; onCreated: (project: Project) => void; onClose: () => void }) {
  const [name, setName] = useState(''); const [description, setDescription] = useState(''); const [directory, setDirectory] = useState(''); const [error, setError] = useState(''); const [saving, setSaving] = useState(false)
  const submit = async () => { if (!directory.trim()) return; setSaving(true); setError(''); try { const project = await api.importDirectory(directory.trim(), name.trim(), description.trim()); await refresh(); onCreated(project) } catch (err) { setError(err instanceof Error ? err.message : 'FirmSight could not inspect this local directory.') } finally { setSaving(false) } }
  return <div className="modal-backdrop" role="presentation"><section className="create-dialog" role="dialog" aria-modal="true" aria-labelledby="create-project-title"><div className="dialog-head"><div><p className="eyebrow">LOCAL DIRECTORY</p><h2 id="create-project-title">Import Project</h2></div><button className="icon-button" onClick={onClose} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">Enter a local firmware directory. FirmSight reads the selected analysis scope directly from the configured backend workspace, then indexes it without executing project code.</p><div className="dialog-section"><p className="section-label">PROJECT DIRECTORY</p><label>Directory path<input autoFocus value={directory} onChange={event => setDirectory(event.target.value)} placeholder="/path/to/firmware-project" /></label><p className="field-hint">The directory must be inside <code>FIRMSIGHT_IMPORT_ROOT</code> configured on the backend.</p></div><div className="dialog-section"><p className="section-label">PROJECT IDENTITY</p><label>Project name <span className="optional-label">optional</span><input value={name} onChange={event => setName(event.target.value)} placeholder="Defaults to the directory name" /></label><label>Optional description<textarea value={description} onChange={event => setDescription(event.target.value)} placeholder="Purpose, target hardware, or context for this firmware." /></label></div><section className="analysis-focus"><div className="analysis-focus-head"><span className="analysis-focus-icon"><ScanSearch size={18} /></span><div><p className="section-label">ANALYSIS FOCUS</p><strong>Focused local analysis</strong><small>Only the following project locations are included in the AI source index.</small></div><span className="source-tag">AUTO DETECT</span></div><div className="focus-paths"><code>lib/</code><code>src/</code><code>include/</code><code>platformio.ini</code><code>sdkconfig</code><code>sdkconfig.defaults</code></div></section>{error && <div className="error"><CircleAlert size={16} />{error}</div>}<div className="dialog-actions"><button onClick={onClose}>Cancel</button><button className="primary" disabled={!directory.trim() || saving} onClick={() => void submit()}>{saving ? <LoaderCircle className="spin" size={16} /> : <ScanSearch size={16} />}{saving ? 'Indexing…' : 'Analyze Directory'}</button></div></section></div>
}

function ProjectsPage({ projects, active, loading, onOpen, onCreate, refresh, onDelete }: { projects: Project[]; active: Project | null; loading: boolean; onOpen: (id: string) => void; onCreate: () => void; refresh: () => Promise<void>; onDelete: (project: Project) => void }) { if (loading && !projects.length) return <Loading />; return <section className="page-stack"><div className="section-head"><div><h2>Firmware projects</h2><p>Each project is imported from a configured local directory. FirmSight never creates project data automatically.</p></div><button className="quiet" title="Reload stored project records" onClick={() => void refresh()}><RefreshCcw size={16} /> Reload</button></div>{projects.length === 0 ? <Empty title="No firmware projects yet." detail="Import a local project directory to start reviewing firmware with FirmSight." action={<button className="primary" onClick={onCreate}><Plus size={16} /> Import Project</button>} /> : <div className="project-grid">{projects.map(project => <ProjectCard key={project.id} project={project} selected={active?.id === project.id} onOpen={onOpen} onDelete={onDelete} />)}</div>}</section> }
function ProjectCard({ project, selected, onOpen, onDelete }: { project: Project; selected: boolean; onOpen: (id: string) => void; onDelete: (project: Project) => void }) { return <article className={`project-card ${selected ? 'selected' : ''}`}><div><span className="source-tag">LOCAL DIRECTORY</span><h3>{project.name}</h3><p>{project.description || 'No description provided.'}</p></div><dl><div><dt>Framework</dt><dd>{project.framework ?? 'Awaiting source inspection'}</dd></div><div><dt>Source index</dt><dd>{project.file_count} files · {project.symbol_count} symbols</dd></div></dl><div className="card-actions"><button onClick={() => onOpen(project.id)}>Open <ChevronRight size={15} /></button><button className="delete-action" onClick={() => onDelete(project)}><Trash2 size={15} /> Delete</button></div></article> }

function OverviewPage({ project, navigate }: { project: Project | null; navigate: (page: Page) => void }) { const findings = useAsync(() => project ? api.findings(project.id) : Promise.resolve([]), [project?.id]); const memories = useAsync(() => project ? api.memories(project.id) : Promise.resolve([]), [project?.id]); const yaml = useAsync(() => project ? api.yaml(project.id) : Promise.resolve(null), [project?.id]); if (!project) return <Empty title="No project selected" detail="Create or open a firmware project to see its overview." />; const items = findings.data ?? []; const count = (classification: string) => items.filter(finding => finding.classification === classification).length; const rejected = items.filter(finding => finding.decision === 'REJECTED').length; return <section className="page-stack"><div className="project-hero"><div><span className="source-tag">{project.framework ?? 'UNINDEXED'}</span><h2>{project.name}</h2><p>{project.description || 'No project description has been provided.'}</p></div><dl><div><dt>Target</dt><dd>{project.target ?? 'Not detected'}</dd></div><div><dt>Build system</dt><dd>{project.build_system ?? 'Not detected'}</dd></div></dl></div><div className="overview-grid"><MetricCard label="Source index" value={`${project.file_count} files`} detail={`${project.symbol_count} extracted symbols`} />{items.length > 0 && <MetricCard label="Findings" value={items.length} detail={`${count('CONFIRMED_BUG')} confirmed · ${count('PROBABLE_BUG')} probable · ${count('DESIGN_RISK')} risks`} />}{rejected > 0 && <MetricCard label="Engineer decisions" value={`${rejected} rejected`} detail="Stored with explicit engineer reasoning" />}{(memories.data?.length ?? 0) > 0 && <MetricCard label="Engineering Memory" value={`${memories.data?.length} active`} detail="Approved context available to analysis" />}{yaml.data && <MetricCard label="firmware.ai.yaml" value={yaml.data.valid ? 'Validated' : 'Needs review'} detail="Project context file is available" />}</div><article className="next-action"><div><p className="eyebrow">NEXT ENGINEERING ACTION</p><h2>{project.file_count ? 'Start an evidence-based review' : 'Import firmware source'}</h2><p>{project.file_count ? 'FirmSight will use indexed source and explicit assumptions. It will not execute imported code.' : 'Add C/C++ source and project metadata before opening code, review, and architecture tools.'}</p></div><button className="primary" onClick={() => navigate(project.file_count ? 'review' : 'projects')}>{project.file_count ? 'Run First Review' : 'Open Projects'} <ChevronRight size={16} /></button></article></section> }

function CodePage({ project, syncSource }: { project: Project | null; syncSource: (projectId: string, directory?: string) => Promise<{ changed_files: string[] }> }) {
  const files = useAsync(() => project ? api.files(project.id) : Promise.resolve([]), [project?.id])
  const [selected, setSelected] = useState<ProjectFile | null>(null)
  const content = useAsync(() => project && selected ? api.file(project.id, selected.path) : Promise.resolve(null), [project?.id, selected?.path])
  const [syncing, setSyncing] = useState(false)
  const [syncError, setSyncError] = useState('')
  const [attachOpen, setAttachOpen] = useState(false)
  const [directory, setDirectory] = useState('')
  useEffect(() => { if (files.data?.length && !selected) setSelected(files.data[0]) }, [files.data, selected])
  const sync = async (directoryOverride?: string) => {
    if (!project) return
    setSyncing(true); setSyncError('')
    try {
      await syncSource(project.id, directoryOverride)
      await files.run()
      await content.run()
      if (directoryOverride) { setAttachOpen(false); setDirectory('') }
    } catch (error) {
      setSyncError(error instanceof Error ? error.message : 'FirmSight could not sync the local source directory.')
    } finally { setSyncing(false) }
  }
  const requestSync = () => {
    if (!project?.source_sync_available) { setSyncError(''); setAttachOpen(true); return }
    void sync()
  }
  if (!project) return <Empty title="No code context" detail="Select a project to browse indexed source." icon={<Code2 size={27} />} />
  if (files.loading) return <Loading />
  if (files.error) return <ErrorState error={files.error} retry={files.run} />
  if (!files.data?.length) return <Empty title="No indexed source files" detail="Attach or sync the local project directory to build the source index. FirmSight reads source only and never executes it." action={project.source_type === 'LOCAL_DIRECTORY' ? <button className="primary" disabled={syncing} onClick={requestSync}>{project.source_sync_available ? <RefreshCcw size={16} /> : <FolderOpen size={16} />}{project.source_sync_available ? 'Sync source' : 'Attach source'}</button> : undefined} icon={<Code2 size={27} />} />
  return <><section className="code-layout"><aside className="file-tree"><div className="source-sync-panel"><div className="source-sync-copy"><span className="source-sync-icon"><FolderOpen size={17} /></span><div><h2>Source files</h2><p>{project.source_sync_available ? 'Local directory connected' : 'Directory needs to be attached'}</p></div></div><button className="source-sync-action" disabled={syncing} onClick={requestSync}>{syncing ? <LoaderCircle className="spin" size={15} /> : project.source_sync_available ? <RefreshCcw size={15} /> : <FolderOpen size={15} />}{syncing ? 'Syncing…' : project.source_sync_available ? 'Sync source' : 'Attach source'}</button></div>{syncError && <div className="error source-sync-error"><CircleAlert size={16} />{syncError}</div>}<div className="source-file-list">{files.data.map(file => <button key={file.path} className={selected?.path === file.path ? 'active-file' : ''} onClick={() => setSelected(file)}><FileCode2 size={15} /><span>{file.path}</span></button>)}</div></aside><div className="editor-panel"><div className="editor-title"><div><FileCode2 size={17} /> {selected?.path}</div><span>{content.data?.language}</span></div>{content.loading ? <Loading /> : content.error ? <ErrorState error={content.error} retry={content.run} /> : <Editor height="calc(100vh - 270px)" language={content.data?.language === 'C++' ? 'cpp' : content.data?.language === 'C' ? 'c' : 'plaintext'} value={content.data?.content ?? ''} theme="vs" options={{ readOnly: true, minimap: { enabled: false }, fontSize: 13, lineNumbers: 'on', scrollBeyondLastLine: false }} />}</div></section>{attachOpen && <SourceSyncDialog directory={directory} error={syncError} saving={syncing} onDirectoryChange={setDirectory} onClose={() => { if (!syncing) { setAttachOpen(false); setSyncError('') } }} onSubmit={() => { if (!directory.trim()) { setSyncError('Enter the current local firmware directory.'); return } void sync(directory.trim()) }} />}</>
}

function ReviewPage({ project, navigate, notify }: { project: Project | null; navigate: (page: Page) => void; notify: (message: string) => void }) {
  const [focus, setFocus] = useState(['memory', 'concurrency', 'freertos', 'error_handling']); const [review, setReview] = useState<Review | null>(null); const [running, setRunning] = useState(false); const [error, setError] = useState('')
  const run = async () => { if (!project) return; setRunning(true); setError(''); setReview(null); try { const result = await api.review(project.id, 'Full Project', focus); setReview(result); if (result.status !== 'RUNNING') { setRunning(false); notify(result.finding_count ? `AI review completed with ${result.finding_count} verified finding${result.finding_count === 1 ? '' : 's'}.` : 'AI review completed: no candidate survived verifier scrutiny.') } } catch (err) { const message = err instanceof Error ? err.message : 'AI Review failed'; setError(message); notify(message); setRunning(false) } }
  const retryUnavailable = async () => { if (!project || !review) return; setRunning(true); setError(''); try { const result = await api.retryReview(project.id, review.id); setReview(result) } catch (err) { const message = err instanceof Error ? err.message : 'Unable to resume unavailable review batches'; setError(message); notify(message); setRunning(false) } }
  const pollProjectId = project?.id; const pollReviewId = review?.id; const pollReviewStatus = review?.status
  useEffect(() => { if (!pollProjectId || !pollReviewId || pollReviewStatus !== 'RUNNING') return; let stopped = false; const poll = async () => { try { const next = await api.reviewStatus(pollProjectId, pollReviewId); if (stopped) return; setReview(next); if (next.status !== 'RUNNING') { setRunning(false); if (next.status === 'COMPLETED') notify(next.finding_count ? `AI review completed with ${next.finding_count} verifier-approved finding${next.finding_count === 1 ? '' : 's'}.` : 'AI review completed: no candidate survived verifier scrutiny.'); else if (next.status === 'PARTIAL') notify(`AI review partially completed: ${next.validated_batches} / ${next.total_batches} batches validated.`); else setError(next.error ?? 'AI review failed before a validated result was available.') } } catch (err) { if (!stopped) { setRunning(false); setError(err instanceof Error ? err.message : 'Unable to read review progress.') } } }; void poll(); const interval = window.setInterval(() => { void poll() }, 1000); return () => { stopped = true; window.clearInterval(interval) } }, [pollProjectId, pollReviewId, pollReviewStatus, notify])
  if (!project) return <Empty title="No project selected" detail="Select a project before running an evidence-based review." icon={<ScanSearch size={27} />} />
  return <section className="page-stack"><div className="review-form"><div><p className="eyebrow">INVESTIGATOR + VERIFIER</p><h2>Firmware review</h2><p>Accepted confirmed findings are rechecked against current source first. Every reviewable project file is then queued in ordered source batches; Investigator proposes runtime candidates and Verifier actively attempts to disprove them.</p></div><div className="focus-list">{['memory', 'concurrency', 'freertos', 'interrupt', 'networking', 'mqtt', 'ota', 'security', 'error_handling'].map(item => <label key={item}><input type="checkbox" checked={focus.includes(item)} onChange={() => setFocus(current => current.includes(item) ? current.filter(value => value !== item) : [...current, item])} />{item.replaceAll('_', ' ')}</label>)}</div><button className="primary" disabled={running || !project.file_count} onClick={() => void run()}>{running ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}{running ? 'Review in progress…' : 'Run Full Review'}</button>{!project.file_count && <p className="field-hint">Import a local project directory before starting review.</p>}{project.source_type === 'LOCAL_DIRECTORY' && !project.source_sync_available && <p className="field-hint">Current source directory is not attached yet. Open an accepted confirmed finding and choose <strong>Verify fix</strong> once to attach it; future full reviews will refresh source automatically before AI recheck.</p>}</div>{review?.status === 'RUNNING' && <div className="ai-run-state"><LoaderCircle className="spin" size={19} /><div><p className="eyebrow">LIVE AI REVIEW</p><strong>{review.progress.at(-1) ?? 'Preparing review context'}</strong><p>FirmSight polls the persisted review job every second. The review includes every reviewable source file, so larger projects may take more batches before candidate verification begins.</p></div></div>}{error && <ErrorState error={error} retry={() => void run()} />}{review && <ReviewProgressPanel review={review} navigate={navigate} onRetry={retryUnavailable} />}{review && ['COMPLETED', 'PARTIAL'].includes(review.status) && <LearningSummaryPanel projectId={review.project_id} reviewId={review.id} />}</section>
}

const CONTEXT_FILE_PREVIEW_LIMIT = 8
const REVIEW_FOCUS_LABELS: Record<string, string> = { memory: 'Memory', concurrency: 'Concurrency', freertos: 'FreeRTOS', interrupt: 'Interrupt', networking: 'Networking', mqtt: 'MQTT', ota: 'OTA', security: 'Security', error_handling: 'Error Handling' }
const REVIEW_STATUS_LABELS: Record<string, string> = { RUNNING: 'LIVE REVIEW PROGRESS', COMPLETED: 'AI REVIEW COMPLETE', PARTIAL: 'AI REVIEW PARTIAL', INTERRUPTED: 'AI REVIEW INTERRUPTED', FAILED: 'AI REVIEW FAILED' }
function reviewFocusLabel(value: string): string {
  return REVIEW_FOCUS_LABELS[value] ?? value.split('_').filter(Boolean).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(' ')
}

function formatBudget(value: OutputBudget | undefined, fallback: string): string {
  if (value === 'PROVIDER_DEFAULT') return 'Provider default — no FirmSight output cap'
  return typeof value === 'number' ? `${value.toLocaleString()} tokens` : fallback
}

function ReviewProgressPanel({ review, navigate, onRetry }: { review: Review; navigate: (page: Page) => void; onRetry: () => void }) {
  const [showAllContextFiles, setShowAllContextFiles] = useState(false)
  useEffect(() => { setShowAllContextFiles(false) }, [review.id])
  const execution = review.execution_progress ?? { phase: review.status, total_units: review.total_batches, completed_units: review.validated_batches + review.unavailable_batches, reused_units: 0, unavailable_units: review.unavailable_batches, in_flight_requests: 0, parallel_request_limit: 1, current_units: [] }; const isTerminal = review.status !== 'RUNNING'; const totalUnits = execution.total_units || review.total_batches; const completedUnits = Math.min(totalUnits, execution.completed_units); const percent = totalUnits ? Math.round((completedUnits / totalUnits) * 100) : 0; const validatedBatches = review.validated_batches || 0; const unavailableBatches = review.unavailable_batches || 0; const coverageText = totalUnits ? `${completedUnits} / ${totalUnits} review units complete · ${validatedBatches} validated${execution.reused_units ? ` · ${execution.reused_units} reused` : ''}${unavailableBatches ? ` · ${unavailableBatches} unavailable` : ''}` : 'Planning review units'; const title = review.status === 'RUNNING' ? 'Reviewing selected context' : review.status === 'PARTIAL' || review.status === 'INTERRUPTED' ? 'Review incomplete' : review.status === 'FAILED' ? 'Review failed' : review.finding_count ? `${review.finding_count} findings created` : 'No verified findings'
  const outputLimitEvents = (review.diagnostics ?? []).filter(event => event.error_kind === 'OUTPUT_LIMIT_BEFORE_FINAL')
  const hasMoreContextFiles = review.context_files.length > CONTEXT_FILE_PREVIEW_LIMIT
  const visibleContextFiles = showAllContextFiles ? review.context_files : review.context_files.slice(0, CONTEXT_FILE_PREVIEW_LIMIT)
  const historicalBudgetLabels = Array.from(new Set(outputLimitEvents.map(event => {
    const role = event.role === 'investigator' ? 'Investigator' : event.role === 'fix-verifier' ? 'Verify Fix / accepted recheck' : 'Verifier'
    const value = event.role === 'investigator' ? review.output_budget_snapshot.investigator : event.role === 'fix-verifier' ? review.output_budget_snapshot.verifier_fix : review.output_budget_snapshot.verifier
    return `${role}: ${formatBudget(value, 'not available')}`
  })))
  return <div className="review-progress"><div className="section-head"><div><p className="eyebrow">{REVIEW_STATUS_LABELS[review.status] ?? review.status}</p><h2>{title}</h2></div>{isTerminal && <button onClick={() => navigate('findings')}>Open Findings <ChevronRight size={15} /></button>}</div><div className="review-progress-bar"><div className="progress-copy"><span>{coverageText}</span><strong>{percent}%</strong></div><div className="progress-track" role="progressbar" aria-label="AI review progress" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><span style={{ width: `${percent}%` }} /></div><p className="field-hint">Phase: {execution.phase.replaceAll('_', ' ').toLowerCase()} · {execution.in_flight_requests} active request{execution.in_flight_requests === 1 ? '' : 's'} (limit {execution.parallel_request_limit})</p>{execution.current_units.length > 0 && <p className="field-hint">Now analyzing: {execution.current_units.join(' · ')}</p>}</div><section className="review-context" aria-label="Review scope"><div className="review-context-section"><h3 className="section-label">Analysis focus</h3><ul className="review-focus-list">{review.focus.map(item => <li key={item}>{reviewFocusLabel(item)}</li>)}</ul></div>{review.context_files.length > 0 && <div className="review-context-section"><h3 className="section-label">Source context ({review.context_files.length} files)</h3><ul className="review-file-list" id="review-context-files">{visibleContextFiles.map(path => <li key={path}><code>{path}</code></li>)}</ul>{hasMoreContextFiles && <button type="button" className="review-disclosure" aria-expanded={showAllContextFiles} aria-controls="review-context-files" onClick={() => setShowAllContextFiles(current => !current)}>{showAllContextFiles ? 'Show fewer files' : `Show all ${review.context_files.length} files`}</button>}</div>}</section><ol className="review-event-list">{review.progress.map((step, index) => <li key={`${index}:${step}`} className={review.status === 'RUNNING' && index === review.progress.length - 1 ? 'review-event active-progress' : 'review-event'}>{review.status === 'RUNNING' && index === review.progress.length - 1 ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />} <span>{step}</span></li>)}</ol><ReviewDiagnostics diagnostics={review.diagnostics ?? []} />{outputLimitEvents.length > 0 && <div className="review-coverage-warning output-limit-guidance"><p><strong>Output budget reached before structured JSON.</strong> {outputLimitEvents.length} request{outputLimitEvents.length === 1 ? '' : 's'} did not produce a validated result, so those units were not added as findings.</p><p className="field-hint">Historical budget used by this review: {historicalBudgetLabels.join('; ')}. Provider default removes only FirmSight's cap; provider/model limits and usage charges still apply.</p><button type="button" className="secondary" onClick={() => navigate('settings')}><Settings2 size={15} /> Adjust AI output budget</button></div>}{['FAILED', 'PARTIAL'].includes(review.status) && review.unavailable_batches > 0 && <div className="review-coverage-warning"><p className="field-hint">Only validated batches contributed to these findings. Retry unavailable batches before treating the review as complete.</p><button type="button" className="secondary" onClick={onRetry}>Retry unavailable batches</button></div>}{review.status === 'INTERRUPTED' && <div className="review-coverage-warning"><p className="field-hint">The review worker stopped before completion. Resume unavailable batches to continue analysis.</p><button type="button" className="secondary" onClick={onRetry}>Resume unavailable batches</button></div>}{review.error && <div className="error"><CircleAlert size={16} />{review.error}</div>}</div> }

const DIAGNOSTIC_LABELS: Record<string, string> = {
  REQUEST_PREPARED: 'Prepared', REQUEST_SENT: 'Sent', WAITING_FOR_PROVIDER: 'Waiting for provider',
  RESPONSE_RECEIVED: 'Response received', STRUCTURED_VALIDATED: 'Validated', REPAIR_STARTED: 'Repair started',
  COMPLETED: 'Completed', FAILED: 'Failed', RETRY_SUPPRESSED: 'Retry suppressed', REPAIR_SKIPPED: 'Repair skipped',
}

function diagnosticLabel(state: string): string { return DIAGNOSTIC_LABELS[state] ?? state.replaceAll('_', ' ').toLowerCase() }
function formatCount(value: number | null | undefined): string { return value == null ? '—' : value.toLocaleString() }

function ReviewDiagnostics({ diagnostics }: { diagnostics: ReviewDiagnostic[] }) {
  const [expanded, setExpanded] = useState(false)
  const grouped = new Map<string, ReviewDiagnostic[]>()
  diagnostics.forEach(event => grouped.set(event.request_id, [...(grouped.get(event.request_id) ?? []), event]))
  const requests = Array.from(grouped.values()).map(events => {
    const latest = events[events.length - 1]
    const firstWith = (key: keyof ReviewDiagnostic): unknown => [...events].reverse().find(event => event[key] != null)?.[key] ?? null
    return { ...latest, elapsed_ms: firstWith('elapsed_ms') as number | null, response_chars: firstWith('response_chars') as number | null, http_status: firstWith('http_status') as number | null, content_type: firstWith('content_type') as string | null, usage: firstWith('usage') as ReviewDiagnostic['usage'] }
  })
  const latest = diagnostics.at(-1)
  const waiting = latest?.state === 'WAITING_FOR_PROVIDER'
  const usageEligible = (state: string) => state === 'COMPLETED' || state === 'STRUCTURED_VALIDATED'
  /*
  return <section className="review-diagnostics" aria-label="AI request diagnostics">
    <div className="review-diagnostics-head"><div><h3 className="section-label">Request diagnostics</h3><p>{latest ? `${requests.length} request${requests.length === 1 ? '' : 's'} tracked · ${waiting ? 'The latest request is waiting for the provider.' : diagnosticLabel(latest.state)}` : 'No provider request has emitted telemetry yet.'}</p><p className="review-diagnostics-note">Full review sends larger source and structured prompts than Chat. A successful Chat response does not prove every review batch can finish within the provider timeout.</p></div><button type="button" className="review-disclosure" aria-expanded={expanded} onClick={() => setExpanded(current => !current)}>{expanded ? 'Hide details' : 'Show details'}</button></div>
    {expanded && <div className="review-diagnostic-list">{requests.length === 0 ? <p className="field-hint">Diagnostics will appear after the first request is prepared.</p> : requests.map(event => <article className={`review-diagnostic ${event.state === 'WAITING_FOR_PROVIDER' ? 'waiting' : ''}`} key={event.request_id}><div className="review-diagnostic-row"><strong>{diagnosticLabel(event.state)}</strong><span>{event.role}{event.batch_number && event.total_batches ? ` · batch ${event.batch_number}/${event.total_batches}` : ''}</span><code>{event.request_id}</code></div><div className="review-diagnostic-meta"><span>{event.provider} · {event.model}</span><span>{event.file_count} file{event.file_count === 1 ? '' : 's'}</span><span>{event.elapsed_ms == null ? 'Elapsed —' : `${(event.elapsed_ms / 1000).toFixed(1)}s`}</span><span>{event.response_chars == null ? 'Response —' : `${formatCount(event.response_chars)} chars`}</span></div>{event.usage ? <div className="review-usage"><span>Input {formatCount(event.usage.prompt_tokens)}</span><span>Output {formatCount(event.usage.completion_tokens)}</span><span>Total {formatCount(event.usage.total_tokens)}</span>{event.usage.reasoning_tokens != null && <span>Reasoning {formatCount(event.usage.reasoning_tokens)}</span></div> : ['COMPLETED', 'STRUCTURED_VALIDATED'].includes(event.state) ? <p className="field-hint">Usage not reported by provider.</p> : null}{event.error_message && <p className="diagnostic-error">{event.error_kind ? `${event.error_kind}: ` : ''}{event.error_message}</p>}</article>)}</div>}
  </section> */
  return (
    <section className="review-diagnostics" aria-label="AI request diagnostics">
      <div className="review-diagnostics-head">
        <div>
          <h3 className="section-label">Request diagnostics</h3>
          <p>{latest ? `${requests.length} request${requests.length === 1 ? '' : 's'} tracked · ${waiting ? 'The latest request is waiting for the provider.' : diagnosticLabel(latest.state)}` : 'No provider request has emitted telemetry yet.'}</p>
          <p className="review-diagnostics-note">Full review sends larger source and structured prompts than Chat. A successful Chat response does not prove every review batch can finish within the provider timeout.</p>
        </div>
        <button type="button" className="review-disclosure" aria-expanded={expanded} onClick={() => setExpanded(current => !current)}>{expanded ? 'Hide details' : 'Show details'}</button>
      </div>
      {expanded && <div className="review-diagnostic-list">
        {requests.length === 0 ? <p className="field-hint">Diagnostics will appear after the first request is prepared.</p> : requests.map(event => {
          const usageUnavailable = !event.usage && usageEligible(event.state)
          const stopReason = event.error_kind === 'OUTPUT_LIMIT_BEFORE_FINAL' ? 'Model exhausted its output budget before producing the final structured response.' : event.error_kind === 'REASONING_ONLY_COMPLETION' ? 'Model returned reasoning without final message content.' : event.error_kind === 'PROVIDER_FORMAT_INCOMPATIBLE' ? 'Provider response format did not contain a usable assistant message.' : event.error_kind === 'INVALID_STRUCTURED_JSON' ? 'Provider returned content that was not valid JSON.' : event.error_kind === 'SCHEMA_VALIDATION_FAILED' ? `Structured result did not match the FirmSight schema${event.validation_fields?.length ? `: ${event.validation_fields.join(', ')}` : '.'}` : event.error_kind === 'STRUCTURED_MODE_UNSUPPORTED' ? 'Provider rejected the configured structured-output mode.' : null
          return <article className={`review-diagnostic ${event.state === 'WAITING_FOR_PROVIDER' ? 'waiting' : ''}`} key={event.request_id}>
            <div className="review-diagnostic-row"><strong>{diagnosticLabel(event.state)}</strong><span>{event.role}{event.batch_number && event.total_batches ? ` · batch ${event.batch_number}/${event.total_batches}` : ''}</span><code>{event.request_id}</code></div>
            <div className="review-diagnostic-meta"><span>{event.provider} · {event.model}</span><span>{event.file_count} file{event.file_count === 1 ? '' : 's'}</span><span>{event.elapsed_ms == null ? 'Elapsed —' : `${(event.elapsed_ms / 1000).toFixed(1)}s`}</span><span>{event.response_chars == null ? 'Response —' : `${formatCount(event.response_chars)} chars`}</span>{event.content_state && <span>Content {event.content_state.toLowerCase().replaceAll('_', ' ')}</span>}{event.finish_reason && <span>Finish {event.finish_reason}</span>}</div>
            {event.usage ? <div className="review-usage"><span>Input {formatCount(event.usage.prompt_tokens)}</span><span>Output {formatCount(event.usage.completion_tokens)}</span><span>Total {formatCount(event.usage.total_tokens)}</span>{event.usage.reasoning_tokens != null && <span>Reasoning {formatCount(event.usage.reasoning_tokens)}</span>}</div> : usageUnavailable ? <p className="field-hint">Usage not reported by provider.</p> : null}
            {stopReason && <p className="diagnostic-reason"><strong>Why this request stopped:</strong> {stopReason}</p>}
            {event.retry_suppressed && <p className="diagnostic-reason"><strong>Retry suppressed:</strong> repeating the same provider request is unlikely to produce a different result.</p>}
            {event.error_message && <p className="diagnostic-error">{event.error_kind ? `${event.error_kind}: ` : ''}{event.error_message}</p>}
          </article>
        })}
      </div>}
    </section>
  )
}

function FindingsPage({ project, navigate, notify }: { project: Project | null; navigate: (page: Page) => void; notify: (message: string) => void }) {
  const findings = useAsync(() => project ? api.findings(project.id) : Promise.resolve([]), [project?.id])
  const [selected, setSelected] = useState<Finding | null>(null)
  const [pendingAction, setPendingAction] = useState('')
  const [acceptOpen, setAcceptOpen] = useState(false)
  const [acceptError, setAcceptError] = useState('')
  const [rejectOpen, setRejectOpen] = useState(false)
  const [rejectionReason, setRejectionReason] = useState('')
  const [rejectionError, setRejectionError] = useState('')
  const [sourceAttached, setSourceAttached] = useState(Boolean(project?.source_sync_available))
  const [sourceAttachOpen, setSourceAttachOpen] = useState(false)
  const [sourceDirectory, setSourceDirectory] = useState('')
  const [sourceAttachError, setSourceAttachError] = useState('')
  useEffect(() => { setSelected(null); setAcceptOpen(false); setAcceptError(''); setRejectOpen(false); setRejectionReason(''); setRejectionError(''); setSourceAttachOpen(false); setSourceDirectory(''); setSourceAttachError('') }, [project?.id])
  useEffect(() => { setSourceAttached(Boolean(project?.source_sync_available)) }, [project?.id, project?.source_sync_available])
  useEffect(() => {
    if (!findings.data?.length) { setSelected(null); return }
    setSelected(current => findings.data?.find(finding => finding.id === current?.id) ?? findings.data?.[0] ?? null)
  }, [findings.data])
  const applyUpdate = (update: Finding) => {
    setSelected(update)
    findings.setData(current => current?.map(finding => finding.id === update.id ? update : finding) ?? [])
  }
  const decide = async (decision: string, reason?: string) => {
    if (!project || !selected) return false
    setPendingAction(decision)
    try {
      const update = await api.decide(project.id, selected.id, decision, reason)
      applyUpdate(update)
      notify(`Finding marked ${decision.toLowerCase()}.`)
      return true
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Unable to update finding'
      if (decision === 'REJECTED') setRejectionError(message)
      else notify(message)
      return false
    } finally { setPendingAction('') }
  }
  const submitReject = async () => {
    const reason = rejectionReason.trim()
    if (!reason) { setRejectionError('Explain why this finding is incorrect before rejecting it.'); return }
    setRejectionError('')
    if (await decide('REJECTED', reason)) { setRejectOpen(false); setRejectionReason('') }
  }
  const submitAccept = async () => {
    if (!project || !selected) return
    const previous = selected
    setAcceptError('')
    setPendingAction('ACCEPTED')
    applyUpdate({ ...selected, decision: 'ACCEPTED' })
    try {
      const update = await api.decide(project.id, selected.id, 'ACCEPTED')
      applyUpdate(update)
      setAcceptOpen(false)
      notify('Finding accepted. It will be rechecked before new discovery in the next review.')
    } catch (err) {
      applyUpdate(previous)
      setAcceptError(err instanceof Error ? err.message : 'Unable to accept finding.')
    } finally { setPendingAction('') }
  }
  const verifyFix = async (directoryOverride?: string) => {
    if (!project || !selected) return
    if (!sourceAttached && !directoryOverride) { setSourceAttachError(''); setSourceAttachOpen(true); return }
    setPendingAction('VERIFY_FIX')
    try {
      const update = await api.verifyFix(project.id, selected.id, directoryOverride)
      applyUpdate(update)
      if (directoryOverride) { setSourceAttached(true); setSourceAttachOpen(false); setSourceDirectory('') }
      notify(update.remediation.status === 'VERIFIED_FIXED' ? 'Source refresh and verifier check confirmed this fix.' : 'Source refresh completed; the fix still needs attention.')
    } catch (err) { const message = err instanceof Error ? err.message : 'Unable to update resolution'; if (directoryOverride) setSourceAttachError(message); else notify(message) } finally { setPendingAction('') }
  }
  if (!project) return <Empty title="No findings context" detail="Select a project to inspect evidence-based findings." icon={<TriangleAlert size={27} />} />
  if (findings.loading) return <Loading />
  if (findings.error) return <ErrorState error={findings.error} retry={findings.run} />
  if (!findings.data?.length) return <Empty title="No findings yet." detail="Run a firmware review to investigate realistic runtime risks. FirmSight will show only structured candidates it can explain." action={<button className="primary" onClick={() => navigate('review')}><ScanSearch size={16} /> Run First Review</button>} icon={<TriangleAlert size={27} />} />
  if (sourceAttachOpen && selected) return <AttachSourceDialog directory={sourceDirectory} error={sourceAttachError} saving={pendingAction === 'VERIFY_FIX'} onDirectoryChange={setSourceDirectory} onClose={() => { if (!pendingAction) { setSourceAttachOpen(false); setSourceAttachError('') } }} onSubmit={() => { if (!sourceDirectory.trim()) { setSourceAttachError('Enter the current local firmware directory.'); return } void verifyFix(sourceDirectory.trim()) }} />
  if (acceptOpen && selected) return <AcceptFindingDialog title={selected.title} error={acceptError} saving={pendingAction === 'ACCEPTED'} onClose={() => { if (!pendingAction) { setAcceptOpen(false); setAcceptError('') } }} onSubmit={() => void submitAccept()} />
  return <section className="findings-layout"><aside className="finding-list">{findings.data.map(finding => <button key={finding.id} className={selected?.id === finding.id ? 'active-finding' : ''} onClick={() => setSelected(finding)}><div className="finding-list-status"><Severity finding={finding} /><DecisionBadge decision={finding.decision} /><RemediationBadge remediation={finding.remediation} /></div><strong>{finding.title}</strong><span>{finding.classification.replaceAll('_', ' ')} · {Math.round(finding.confidence * 100)}%</span></button>)}</aside>{selected && <article className="finding-detail"><div className="finding-title"><div><div className="finding-status"><Severity finding={selected} /><span className="classification">{selected.classification.replaceAll('_', ' ')}</span><DecisionBadge decision={selected.decision} /><RemediationBadge remediation={selected.remediation} /></div><h2>{selected.title}</h2><p>{selected.location.file} · {selected.location.function ?? 'source'} · lines {selected.location.line_start}–{selected.location.line_end}</p></div><strong className="confidence">{Math.round(selected.confidence * 100)}%<small>confidence</small></strong></div>{selected.decision === 'ACCEPTED' && <div className="decision-confirmation"><Check size={18} /><div><strong>Accepted by you</strong><span>FirmSight will AI-recheck this confirmed finding before new discovery in the next review.</span></div></div>}<Detail label="Why FirmSight flagged this"><p>{selected.summary}</p><Evidence evidence={selected.evidence} /></Detail><Detail label="Execution path"><ol className="execution">{selected.execution_path.map(step => <li key={step}>{step}</li>)}</ol></Detail><Detail label="Runtime impact"><p>{selected.runtime_scenario}</p><p>{selected.impact}</p></Detail><Detail label="Assumptions"><ul>{selected.assumptions.map(item => <li key={item.statement}><span className={item.status === 'CHECKED' ? 'checked' : 'unverified'}>{item.status === 'CHECKED' ? '✓' : '?'}</span>{item.statement}</li>)}</ul></Detail><Detail label="Verifier result"><p><ShieldCheck size={16} /> {selected.verification.status}: {selected.verification.notes}</p></Detail><Detail label="Recommended fix"><p>{selected.recommendation}</p></Detail><Detail label="Fix verification"><FixVerificationDetails remediation={selected.remediation} /></Detail><div className="decision-bar"><button className={selected.decision === 'ACCEPTED' ? 'decision-active' : ''} disabled={Boolean(pendingAction)} onClick={() => { setAcceptError(''); setAcceptOpen(true) }}><Check size={16} /> {pendingAction === 'ACCEPTED' ? 'Accepting…' : selected.decision === 'ACCEPTED' ? 'Accepted' : 'Accept'}</button><button className={selected.decision === 'REJECTED' ? 'decision-active reject-action' : 'reject-action'} disabled={Boolean(pendingAction)} onClick={() => { setRejectionError(''); setRejectOpen(true) }}><X size={16} /> Reject</button><button className={selected.decision === 'INTENTIONAL' ? 'decision-active' : ''} disabled={Boolean(pendingAction)} onClick={() => void decide('INTENTIONAL')}><BadgeCheck size={16} /> {pendingAction === 'INTENTIONAL' ? 'Saving…' : 'Intentional'}</button>{selected.classification === 'CONFIRMED_BUG' && <button className={selected.remediation.status === 'VERIFIED_FIXED' ? 'solved-action' : ''} disabled={Boolean(pendingAction) || selected.decision !== 'ACCEPTED' || project.source_type !== 'LOCAL_DIRECTORY'} onClick={() => void verifyFix()} title={selected.decision !== 'ACCEPTED' ? 'Accept this finding before verifying a source fix.' : project.source_type !== 'LOCAL_DIRECTORY' ? 'Verify Fix requires a local-directory project.' : undefined}><ShieldCheck size={16} /> {pendingAction === 'VERIFY_FIX' ? 'Checking source…' : selected.remediation.status === 'VERIFIED_FIXED' ? 'Recheck fix' : 'Verify fix'}</button>}<div className="decision-status"><DecisionBadge decision={selected.decision} /><RemediationBadge remediation={selected.remediation} /></div></div>{selected.classification !== 'CONFIRMED_BUG' && <p className="field-hint">Fix verification is available only for findings classified as Confirmed Bug.</p>}{selected.classification === 'CONFIRMED_BUG' && selected.decision !== 'ACCEPTED' && <p className="field-hint">Accept this finding first. Verify Fix then refreshes the local source directory and asks the skeptic to recheck the old execution path.</p>}</article>}{rejectOpen && selected && <RejectFindingDialog title={selected.title} reason={rejectionReason} error={rejectionError} saving={pendingAction === 'REJECTED'} onReasonChange={setRejectionReason} onClose={() => { if (!pendingAction) { setRejectOpen(false); setRejectionError('') } }} onSubmit={() => void submitReject()} />}</section>
}
function DecisionBadge({ decision }: { decision: string }) { if (decision === 'UNREVIEWED') return null; const label = decision === 'INTENTIONAL' ? 'Intentional' : decision[0] + decision.slice(1).toLowerCase(); return <span className={`decision-badge ${decision.toLowerCase()}`}>{label}</span> }
function RemediationBadge({ remediation }: { remediation: Finding['remediation'] }) { if (remediation.status === 'UNVERIFIED') return null; const labels: Record<Finding['remediation']['status'], string> = { UNVERIFIED: 'Not verified', VERIFIED_FIXED: 'Solved · AI checked', STILL_PRESENT: 'Still present', INCONCLUSIVE: 'Needs recheck', MANUALLY_MARKED: 'Manually marked' }; return <span className={`remediation-badge ${remediation.status.toLowerCase()}`}>{remediation.status === 'VERIFIED_FIXED' && <Check size={11} />}{labels[remediation.status]}</span> }
function FixVerificationDetails({ remediation }: { remediation: Finding['remediation'] }) {
  const report = remediation.verification
  if (!report) return <div className="fix-report"><RemediationBadge remediation={remediation} /><p>{remediation.notes}</p>{remediation.changed_files.length > 0 && <div className="changed-files"><span>Refreshed source</span>{remediation.changed_files.map(path => <code key={path}>{path}</code>)}</div>}</div>
  const evidenceList = (title: string, items: FixVerificationEvidence[]) => <div className="fix-report-group"><strong>{title}</strong>{items.length ? <ul className="fix-report-evidence">{items.map((item, index) => <li key={`${item.file}:${item.line}:${index}`}><code>{item.file}:{item.line}</code><span>{item.description}{item.evidence_snippet && <small>{item.evidence_snippet}</small>}</span></li>)}</ul> : <p>None reported.</p>}</div>
  return <div className="fix-report">
    <div className="fix-report-heading"><RemediationBadge remediation={remediation} /><strong>{Math.round(report.confidence * 100)}% confidence</strong></div>
    <p className="fix-report-summary">{report.reasoning_summary || remediation.notes}</p>
    {report.original_failure_condition && <div className="fix-report-group"><strong>Original failure condition</strong><p>{report.original_failure_condition}</p></div>}
    {report.original_execution_path.length > 0 && <div className="fix-report-group"><strong>Original execution path</strong><ol className="execution">{report.original_execution_path.map((step, index) => <li key={`${step}:${index}`}>{step}</li>)}</ol></div>}
    {report.alternative_mitigation && <p className="fix-report-alternative">Alternative mitigation accepted</p>}
    {evidenceList('Mitigation found', report.mitigations_found)}
    {report.current_execution_path.length > 0 && <div className="fix-report-group"><strong>Current execution path</strong><ol className="execution">{report.current_execution_path.map((step, index) => <li key={`${step}:${index}`}>{step}</li>)}</ol></div>}
    {report.remaining_failure_evidence.length > 0 && evidenceList('Current failure evidence', report.remaining_failure_evidence)}
    {report.missing_context.length > 0 && <div className="fix-report-group"><strong>Missing context</strong><ul>{report.missing_context.map((item, index) => <li key={`${item}:${index}`}>{item}</li>)}</ul></div>}
    <details className="fix-report-inspected"><summary>Inspected source and snapshot identifiers</summary>
      <div className="fix-report-group"><strong>Changed files</strong><ul>{remediation.changed_files.map(path => <li key={path}><code>{path}</code></li>)}</ul></div>
      <div className="fix-report-group"><strong>Inspected files</strong><ul>{report.inspected_files.map(path => <li key={path}><code>{path}</code></li>)}</ul></div>
      <div className="fix-report-group"><strong>Inspected symbols</strong><ul>{report.inspected_symbols.map(symbol => <li key={symbol}><code>{symbol}</code></li>)}</ul></div>
      {remediation.baseline_snapshot_hash && <p>Baseline: <code>{remediation.baseline_snapshot_hash}</code></p>}
      {remediation.current_snapshot_hash && <p>Current: <code>{remediation.current_snapshot_hash}</code></p>}
    </details>
  </div>
}
function AcceptFindingDialog({ title, error, saving, onClose, onSubmit }: { title: string; error: string; saving: boolean; onClose: () => void; onSubmit: () => void }) { return <div className="modal-backdrop" role="presentation"><section className="create-dialog acceptance-dialog" role="dialog" aria-modal="true" aria-labelledby="accept-finding-title"><div className="dialog-head"><div><p className="eyebrow">FINDING DECISION</p><h2 id="accept-finding-title">Accept finding</h2></div><button className="icon-button" disabled={saving} onClick={onClose} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">Accept <strong>{title}</strong> as an engineering issue to track. A future Full Review will check this finding against current source before looking for new issues.</p>{error && <div className="error"><CircleAlert size={16} />{error}</div>}<div className="dialog-actions"><button disabled={saving} onClick={onClose}>Cancel</button><button className="primary accept-confirm" disabled={saving} onClick={onSubmit}>{saving ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />}{saving ? 'Accepting…' : 'Accept finding'}</button></div></section></div> }
function AttachSourceDialog({ directory, error, saving, onDirectoryChange, onClose, onSubmit }: { directory: string; error: string; saving: boolean; onDirectoryChange: (value: string) => void; onClose: () => void; onSubmit: () => void }) { return <div className="modal-backdrop" role="presentation"><section className="create-dialog source-attach-dialog" role="dialog" aria-modal="true" aria-labelledby="attach-source-title"><div className="dialog-head"><div><p className="eyebrow">SOURCE REFRESH</p><h2 id="attach-source-title">Attach local source</h2></div><button className="icon-button" disabled={saving} onClick={onClose} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">This existing project was imported before FirmSight saved its source location. Attach the current directory once; the path remains server-side and future Verify Fix checks refresh only the selected firmware scope.</p><label className="rejection-reason">Firmware directory<input autoFocus value={directory} onChange={event => onDirectoryChange(event.target.value)} placeholder="/path/to/firmware-project" /></label><p className="field-hint">FirmSight reads only <code>lib/</code>, <code>src/</code>, <code>include/</code>, <code>platformio.ini</code>, and ESP config files inside <code>FIRMSIGHT_IMPORT_ROOT</code>.</p>{error && <div className="error"><CircleAlert size={16} />{error}</div>}<div className="dialog-actions"><button disabled={saving} onClick={onClose}>Cancel</button><button className="primary" disabled={saving || !directory.trim()} onClick={onSubmit}>{saving ? <LoaderCircle className="spin" size={16} /> : <ShieldCheck size={16} />}{saving ? 'Checking source…' : 'Attach and verify'}</button></div></section></div> }
function SourceSyncDialog({ directory, error, saving, onDirectoryChange, onClose, onSubmit }: { directory: string; error: string; saving: boolean; onDirectoryChange: (value: string) => void; onClose: () => void; onSubmit: () => void }) { return <div className="modal-backdrop" role="presentation"><section className="create-dialog source-sync-dialog" role="dialog" aria-modal="true" aria-labelledby="source-sync-title"><div className="dialog-head"><div><p className="eyebrow">SOURCE CONNECTION</p><h2 id="source-sync-title">Attach project directory</h2></div><button className="icon-button" disabled={saving} onClick={onClose} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">FirmSight needs the current local directory once before it can keep this project index up to date. The path stays on the backend and source is read only.</p><label className="source-directory-input">Project directory<input autoFocus value={directory} onChange={event => onDirectoryChange(event.target.value)} placeholder="/path/to/firmware-project" /></label><div className="source-scope-summary"><FolderOpen size={17} /><div><strong>Included in sync</strong><span><code>src/</code><code>lib/</code><code>include/</code><code>platformio.ini</code><code>sdkconfig</code></span></div></div>{error && <div className="error"><CircleAlert size={16} />{error}</div>}<div className="dialog-actions"><button disabled={saving} onClick={onClose}>Cancel</button><button className="primary" disabled={saving || !directory.trim()} onClick={onSubmit}>{saving ? <LoaderCircle className="spin" size={16} /> : <RefreshCcw size={16} />}{saving ? 'Syncing source…' : 'Attach and sync'}</button></div></section></div> }
function RejectFindingDialog({ title, reason, error, saving, onReasonChange, onClose, onSubmit }: { title: string; reason: string; error: string; saving: boolean; onReasonChange: (value: string) => void; onClose: () => void; onSubmit: () => void }) { return <div className="modal-backdrop" role="presentation"><section className="create-dialog rejection-dialog" role="dialog" aria-modal="true" aria-labelledby="reject-finding-title"><div className="dialog-head"><div><p className="eyebrow">FINDING DECISION</p><h2 id="reject-finding-title">Reject finding</h2></div><button className="icon-button" disabled={saving} onClick={onClose} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">Explain the engineering context that disproves <strong>{title}</strong>. This reason is stored with the finding and may later support an Engineering Memory proposal.</p><label className="rejection-reason">Rejection reason<textarea autoFocus value={reason} onChange={event => onReasonChange(event.target.value)} placeholder="Example: Only measurement_task writes this buffer; MQTT receives a copied queue snapshot." /></label>{error && <div className="error"><CircleAlert size={16} />{error}</div>}<div className="dialog-actions"><button disabled={saving} onClick={onClose}>Cancel</button><button className="primary reject-confirm" disabled={saving || !reason.trim()} onClick={onSubmit}>{saving ? <LoaderCircle className="spin" size={16} /> : <X size={16} />}{saving ? 'Saving…' : 'Reject finding'}</button></div></section></div> }
function Detail({ label, children }: { label: string; children: ReactNode }) { return <section className="detail-section"><h3>{label}</h3>{children}</section> }
function Evidence({ evidence }: { evidence: Finding['evidence'] }) { return <ul className="evidence">{evidence.map(item => <li key={`${item.file}:${item.line}`}><code>{item.file}:{item.line}</code>{item.description}</li>)}</ul> }

function ChatPage({ project }: { project: Project | null }) { const messages = useAsync(() => project ? api.messages(project.id) : Promise.resolve([]), [project?.id]); const [text, setText] = useState(''); const [sending, setSending] = useState(false); const [error, setError] = useState(''); const send = async () => { if (!project || !text.trim()) return; setSending(true); setError(''); try { await api.chat(project.id, text.trim()); setText(''); await messages.run() } catch (err) { setError(err instanceof Error ? err.message : 'FirmSight could not send that message.') } finally { setSending(false) } }; if (!project) return <Empty title="No project conversation" detail="Select a project to ask FirmSight about its indexed source." icon={<MessageSquareCode size={27} />} />; return <section className="chat-shell"><div className="chat-context"><BrainCircuit size={17} /><span>Project-aware context · source and comments are untrusted data</span></div><div className="messages">{messages.loading ? <Loading /> : messages.error ? <ErrorState error={messages.error} retry={messages.run} /> : messages.data?.length ? messages.data.map((message: ChatMessage) => <article key={message.id} className={`message ${message.role}`}><strong>{message.role === 'assistant' ? 'FirmSight' : 'You'}</strong><p>{message.content}</p></article>) : <Empty title="Ask about this firmware" detail="Ask about execution paths, project architecture, or assumptions in a finding." icon={<MessageSquareCode size={27} />} />}</div>{error && <div className="error chat-error"><CircleAlert size={16} />{error}</div>}<div className="chat-compose"><textarea value={text} onChange={event => setText(event.target.value)} placeholder="Ask FirmSight about this project…" onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void send() } }} /><button className="primary" onClick={() => void send()} disabled={sending || !text.trim()}>{sending ? <LoaderCircle className="spin" size={16} /> : <ChevronRight size={16} />}</button></div></section> }

function ArchitecturePage({ project }: { project: Project | null }) { const symbols = useAsync(() => project ? api.symbols(project.id) : Promise.resolve([]), [project?.id]); if (!project) return <Empty title="No architecture context" detail="Select a project to inspect indexed firmware entities." icon={<Network size={27} />} />; return <section className="page-stack"><div className="architecture-panel"><div className="section-head"><div><h2>Indexed architecture entities</h2><p>Derived deterministically from source. FirmSight does not imagine a graph where the index has no evidence.</p></div><Search size={18} /></div>{symbols.loading ? <Loading /> : symbols.error ? <ErrorState error={symbols.error} retry={symbols.run} /> : symbols.data?.length ? <div className="symbol-table">{symbols.data.map((symbol: Symbol) => <div key={`${symbol.file}:${symbol.line}:${symbol.name}`}><code>{symbol.kind}</code><strong>{symbol.name}</strong><span>{symbol.file}:{symbol.line}</span></div>)}</div> : <Empty title="No architecture entities yet" detail="Import C/C++ source and re-index the project to extract functions, FreeRTOS tasks, queues, mutexes, and event groups." icon={<Network size={27} />} />}</div></section> }

const INTELLIGENCE_GROUPS: { label: string; types: string[] }[] = [
  { label: 'Project behaviors', types: ['PROJECT_FACT', 'BEHAVIORAL_PATTERN', 'DESIGN_INTENT'] },
  { label: 'False-positive knowledge', types: ['FALSE_POSITIVE_KNOWLEDGE'] },
  { label: 'Recurring bug patterns', types: ['BUG_PATTERN'] },
  { label: 'Resolution patterns', types: ['RESOLUTION_PATTERN'] },
  { label: 'Architecture knowledge', types: ['ARCHITECTURAL_PATTERN'] },
  { label: 'Review lessons', types: ['REVIEW_LESSON'] },
]
const STATE_LABELS: Record<string, string> = { PROVISIONAL: 'Provisional', REINFORCED: 'Reinforced', VERIFIED: 'Verified', NEEDS_REVALIDATION: 'Needs revalidation', CONFLICTED: 'Conflicted', SUPERSEDED: 'Superseded', DISABLED: 'Disabled' }
const INTELLIGENCE_STATES = ['', 'PROVISIONAL', 'REINFORCED', 'VERIFIED', 'NEEDS_REVALIDATION', 'CONFLICTED', 'SUPERSEDED', 'DISABLED']
const INTELLIGENCE_TYPES = ['', 'PROJECT_FACT', 'DESIGN_INTENT', 'FALSE_POSITIVE_KNOWLEDGE', 'BUG_PATTERN', 'RESOLUTION_PATTERN', 'ARCHITECTURAL_PATTERN', 'BEHAVIORAL_PATTERN', 'REVIEW_LESSON']

function StateBadge({ state }: { state: string }) { return <span className={`state-badge ${state.toLowerCase()}`}>{STATE_LABELS[state] ?? state}</span> }

function formatWhen(value: string | null | undefined): string {
  if (!value) return 'never'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

function ProjectIntelligencePage({ project, notify }: { project: Project | null; notify: (message: string) => void }) {
  const [stateFilter, setStateFilter] = useState(''); const [typeFilter, setTypeFilter] = useState('')
  const list = useAsync(() => project ? api.intelligence(project.id, stateFilter || undefined, typeFilter || undefined) : Promise.resolve(null), [project?.id, stateFilter, typeFilter])
  const summary = useAsync(() => project ? api.intelligenceSummary(project.id) : Promise.resolve(null), [project?.id])
  const [selectedId, setSelectedId] = useState('')
  const [pendingAction, setPendingAction] = useState('')
  const [actionError, setActionError] = useState('')
  const [disableTarget, setDisableTarget] = useState<IntelligenceRecord | null>(null)
  const [disableReason, setDisableReason] = useState('')
  const detail = useAsync(() => selectedId ? api.intelligenceDetail(selectedId) : Promise.resolve(null), [selectedId])
  const [statement, setStatement] = useState(''); const [symbol, setSymbol] = useState(''); const [note, setNote] = useState(''); const [proposal, setProposal] = useState('')
  useEffect(() => { setSelectedId(''); setActionError(''); setDisableTarget(null); setDisableReason(''); setStatement(''); setSymbol(''); setNote(''); setProposal('') }, [project?.id])
  const refresh = async () => { await Promise.all([list.run(), summary.run()]); if (selectedId) await detail.run() }
  const revalidate = async (record: IntelligenceRecord) => {
    if (!project) return
    setPendingAction(`revalidate:${record.id}`); setActionError('')
    try {
      const result = await api.revalidateIntelligence(record.id)
      notify(result.outcome === 'VERIFIED' ? 'Record verified against current source.' : result.outcome === 'CONFLICTED' ? 'Current source contradicts this record; it is marked conflicted.' : 'Revalidation recorded against the current indexed source.')
      await refresh()
    } catch (err) { setActionError(err instanceof Error ? err.message : 'Unable to revalidate this record.') } finally { setPendingAction('') }
  }
  const disable = async () => {
    if (!disableTarget) return
    setPendingAction(`disable:${disableTarget.id}`); setActionError('')
    try {
      await api.disableIntelligence(disableTarget.id, disableReason.trim() || undefined)
      notify('Knowledge disabled. It will not be used in future analysis.')
      setDisableTarget(null); setDisableReason('')
      await refresh()
    } catch (err) { setActionError(err instanceof Error ? err.message : 'Unable to disable this record.') } finally { setPendingAction('') }
  }
  const propose = async () => {
    if (!project) return; setActionError('')
    try { const result = proposal ? await api.updateMemoryProposal(proposal, statement, symbol, note) : await api.proposeMemory(project.id, statement, symbol, note); setProposal(result.id) }
    catch (err) { setActionError(err instanceof Error ? err.message : 'Unable to propose knowledge') }
  }
  const save = async () => {
    try { await api.approveMemory(proposal); setProposal(''); setStatement(''); setSymbol(''); setNote(''); await refresh() }
    catch (err) { setActionError(err instanceof Error ? err.message : 'Unable to save knowledge') }
  }
  const ignore = async () => {
    try { await api.ignoreMemoryProposal(proposal); setProposal('') } catch (err) { setActionError(err instanceof Error ? err.message : 'Unable to ignore proposal') }
  }
  if (!project) return <Empty title="No Project Intelligence context" detail="Select a project to see what FirmSight has learned about it." icon={<BrainCircuit size={27} />} />
  const records = list.data?.records ?? []
  const counts = summary.data?.counts
  const grouped = INTELLIGENCE_GROUPS.map(group => ({ ...group, items: records.filter(record => group.types.includes(record.type)) })).filter(group => group.items.length > 0)
  const ungrouped = records.filter(record => !INTELLIGENCE_GROUPS.some(group => group.types.includes(record.type)))
  return <section className="page-stack">
    <div className="section-head"><div><h2>What FirmSight has learned about {project.name}</h2><p>Automatic knowledge starts provisional and is never treated as fact. The skeptic verifier and current source decide when it is reinforced or verified; engineers can inspect, revalidate, or disable it.</p></div><button className="icon-button" title="Reload Project Intelligence" onClick={() => void refresh()}><RefreshCcw size={16} /></button></div>
    {summary.loading ? null : summary.error ? <ErrorState error={summary.error} retry={summary.run} /> : counts && <div className="metric-grid">
      <MetricCard label="Learned knowledge" value={counts.total - counts.superseded - counts.disabled} detail="Active records across all states" />
      <MetricCard label="Verified" value={counts.verified} detail="Confirmed by current source" />
      <MetricCard label="Reinforced" value={counts.reinforced} detail="Multiple independent observations" />
      <MetricCard label="Needs revalidation" value={counts.needs_revalidation} detail="Evidence changed or is missing" tone={counts.needs_revalidation > 0 ? 'attention' : 'default'} />
      <MetricCard label="Conflicted" value={counts.conflicted} detail="Contradicted by current source" tone={counts.conflicted > 0 ? 'attention' : 'default'} />
    </div>}
    {summary.data && summary.data.health.open_conflicts > 0 && <div className="error"><CircleAlert size={16} /><span>{summary.data.health.open_conflicts} open conflict{summary.data.health.open_conflicts === 1 ? '' : 's'} need engineer attention. Inspect the conflicted records below.</span></div>}
    <div className="intel-toolbar" role="search">
      <label>State<select value={stateFilter} onChange={event => setStateFilter(event.target.value)}><option value="">All states</option>{INTELLIGENCE_STATES.filter(Boolean).map(state => <option key={state} value={state}>{STATE_LABELS[state]}</option>)}</select></label>
      <label>Type<select value={typeFilter} onChange={event => setTypeFilter(event.target.value)}><option value="">All types</option>{INTELLIGENCE_TYPES.filter(Boolean).map(type => <option key={type} value={type}>{type.replaceAll('_', ' ').toLowerCase()}</option>)}</select></label>
      <span className="field-hint">{records.length} record{records.length === 1 ? '' : 's'} shown</span>
    </div>
    {actionError && <div className="error"><CircleAlert size={16} />{actionError}</div>}
    {list.loading ? <Loading /> : list.error ? <ErrorState error={list.error} retry={list.run} /> : records.length === 0
      ? <Empty title="No project intelligence yet." detail="FirmSight learns automatically from completed reviews, finding decisions, verified fixes, and grounded chat. Nothing is stored without evidence, and automatic knowledge never starts as fact." icon={<BrainCircuit size={27} />} />
      : <div className="intelligence-list">{[...grouped, ...(ungrouped.length ? [{ label: 'Other knowledge', types: [], items: ungrouped }] : [])].map(group => <div key={group.label} className="intel-group">
        <p className="section-label">{group.label.toUpperCase()} · {group.items.length}</p>
        {group.items.map(record => <article key={record.id} className={`intel-card${selectedId === record.id ? ' selected' : ''}`}>
          <div className="intel-card-head"><StateBadge state={record.state} /><span className="source-tag">{record.type.replaceAll('_', ' ').toLowerCase()}</span><strong className="confidence-mini">{Math.round(record.confidence * 100)}%</strong></div>
          <p className="intel-statement">{record.statement}</p>
          <div className="intel-meta">
            <span>{record.observation_count} observation{record.observation_count === 1 ? '' : 's'}{record.reinforcement_count > 0 ? ` · ${record.reinforcement_count} independent` : ''}</span>
            <span>origin: {record.origin === 'SYNTHESIZER' ? 'automatic' : record.origin === 'LEGACY_ENGINEER_APPROVED' ? 'engineer (legacy)' : 'engineer'}</span>
            <span>last validated: {formatWhen(record.last_validated_at)}{record.last_validated_commit ? ` @ ${record.last_validated_commit.slice(0, 8)}` : ''}</span>
          </div>
          {(record.links.length > 0 || record.last_evidence.length > 0) && <div className="intel-links">
            {record.links.filter(link => link.link_kind === 'SYMBOL' || link.link_kind === 'COMPONENT').slice(0, 4).map(link => <code key={link.id}>{link.link_value}</code>)}
            {record.last_evidence.slice(0, 2).map(item => <code key={item.id}>{item.file}{item.line ? `:${item.line}` : ''}</code>)}
          </div>}
          {record.conflict_summary && <div className="error intel-conflict"><CircleAlert size={15} />{record.conflict_summary}</div>}
          <div className="card-actions">
            <button onClick={() => setSelectedId(current => current === record.id ? '' : record.id)}>{selectedId === record.id ? 'Hide evidence' : 'Inspect evidence'}</button>
            <button disabled={record.state === 'DISABLED' || record.state === 'SUPERSEDED' || Boolean(pendingAction)} onClick={() => void revalidate(record)}>{pendingAction === `revalidate:${record.id}` ? 'Revalidating…' : 'Revalidate'}</button>
            <button className="reject-action" disabled={record.state === 'DISABLED' || Boolean(pendingAction)} onClick={() => { setDisableTarget(record); setDisableReason('') }}>Disable</button>
          </div>
          {selectedId === record.id && (detail.data && detail.data.id === record.id
            ? <div className="intel-detail">
                <p className="section-label">EVIDENCE ({detail.data.evidence.length})</p>
                <ul className="intel-evidence-list">{detail.data.evidence.map(item => <li key={item.id}><code>{item.kind.toLowerCase()}{item.file ? ` · ${item.file}${item.line ? `:${item.line}` : ''}` : ''}</code><span>{item.description}</span></li>)}</ul>
                <p className="section-label">OBSERVATION HISTORY ({detail.data.observations.length})</p>
                <ul className="observation-list">{detail.data.observations.map(observation => <li key={observation.id}><code>{observation.created_at.slice(0, 19).replace('T', ' ')}</code><span><strong>{observation.kind.toLowerCase().replaceAll('_', ' ')}</strong>{observation.from_state && observation.to_state ? ` · ${STATE_LABELS[observation.from_state] ?? observation.from_state} → ${STATE_LABELS[observation.to_state] ?? observation.to_state}` : ''}{observation.detail ? ` · ${observation.detail}` : ''}</span></li>)}</ul>
                {detail.data.conflicts.length > 0 && <><p className="section-label">CONFLICTS ({detail.data.conflicts.length})</p><ul className="observation-list">{detail.data.conflicts.map(conflict => <li key={conflict.id}><code>{conflict.resolution_state.toLowerCase()}</code><span>{conflict.detail}</span></li>)}</ul></>}
                {record.superseded_by && <p className="field-hint">Superseded by {record.superseded_by}.</p>}
              </div>
            : <Loading />)}
        </article>)}
      </div>)}</div>}
    <section className="panel suggest-panel">
      <div className="section-head"><div><h2>Suggest knowledge</h2><p>Engineer-authored knowledge uses the same lifecycle: it is stored as reinforced engineer knowledge and still requires source revalidation before it is called verified.</p></div><span className="source-tag">ENGINEER-AUTHORED</span></div>
      <div className="suggest-form">
        <label>Knowledge statement<textarea value={statement} onChange={event => setStatement(event.target.value)} placeholder="MQTT reads a copied measurement snapshot rather than shared measurement_state." /></label>
        <label>Evidence symbol<input value={symbol} onChange={event => setSymbol(event.target.value)} placeholder="measurement_queue" /></label>
        <label>Engineer rationale<textarea value={note} onChange={event => setNote(event.target.value)} placeholder="Confirmed while rejecting a finding." /></label>
        <div className="card-actions"><button disabled={!statement || !symbol || !note} onClick={() => void propose()}>{proposal ? 'Edit proposal' : 'Propose knowledge'}</button>{proposal && <><button className="primary" onClick={() => void save()}><Check size={16} /> Save knowledge</button><button onClick={() => void ignore()}>Ignore</button></>}</div>
      </div>
    </section>
    {disableTarget && <div className="modal-backdrop" role="presentation"><section className="create-dialog" role="dialog" aria-modal="true" aria-labelledby="disable-knowledge-title"><div className="dialog-head"><div><p className="eyebrow">PROJECT INTELLIGENCE</p><h2 id="disable-knowledge-title">Disable knowledge</h2></div><button className="icon-button" disabled={Boolean(pendingAction)} onClick={() => { setDisableTarget(null); setDisableReason('') }} aria-label="Close"><X size={17} /></button></div><p className="dialog-copy">Disabling removes this record from all future analysis. The observation history is preserved for audit.</p><label className="rejection-reason">Optional reason<textarea value={disableReason} onChange={event => setDisableReason(event.target.value)} placeholder="Why this knowledge should not be used." /></label>{actionError && <div className="error"><CircleAlert size={16} />{actionError}</div>}<div className="dialog-actions"><button disabled={Boolean(pendingAction)} onClick={() => { setDisableTarget(null); setDisableReason('') }}>Cancel</button><button className="reject-confirm" disabled={Boolean(pendingAction)} onClick={() => void disable()}>{pendingAction.startsWith('disable:') ? 'Disabling…' : 'Disable knowledge'}</button></div></section></div>}
  </section>
}

function LearningSummaryPanel({ projectId, reviewId }: { projectId: string; reviewId: string }) {
  const summary = useAsync(() => api.learningSummary(projectId, reviewId), [projectId, reviewId])
  if (summary.loading) return <div className="loading compact" role="status">Loading learning summary…</div>
  if (summary.error || !summary.data) return null
  const data: LearningSummary = summary.data
  const chips = [
    data.provisional ? `${data.provisional} provisional` : '',
    data.reinforced ? `${data.reinforced} reinforced` : '',
    data.verified ? `${data.verified} verified` : '',
    data.needs_revalidation ? `${data.needs_revalidation} needing revalidation` : '',
    data.conflicted ? `${data.conflicted} conflicted` : '',
    data.superseded ? `${data.superseded} superseded` : '',
    data.rejected ? `${data.rejected} weak candidates rejected` : '',
  ].filter(Boolean)
  return <section className="panel learning-summary">
    <div className="section-head"><div><h2>What FirmSight learned</h2><p>Persisted results of the Memory Synthesizer and skeptical Memory Verifier for this review.</p></div><span className={`state-badge ${data.status.toLowerCase()}`}>{data.status.toLowerCase()}</span></div>
    {data.status === 'FAILED' ? <p className="field-hint">The review completed, but intelligence processing was unavailable{data.error ? `: ${data.error}` : '.'} Findings are unaffected.</p>
      : data.status === 'SKIPPED' ? <p className="field-hint">Learning was skipped{data.error ? `: ${data.error}` : '.'}</p>
      : chips.length > 0 ? <div className="learning-chips">{chips.map(chip => <span key={chip} className="learning-chip">{chip}</span>)}</div>
      : <p className="field-hint">This review produced no new project knowledge.</p>}
  </section>
}

function YamlPage({ project, notify }: { project: Project | null; notify: (message: string) => void }) {
  const existing = useAsync(() => project ? api.yaml(project.id) : Promise.resolve(null), [project?.id]); const [description, setDescription] = useState(''); const [result, setResult] = useState<YamlResult | null>(null); const [focus, setFocus] = useState(['freertos', 'concurrency', 'memory', 'mqtt', 'ota', 'security']); const [generating, setGenerating] = useState(false); const [error, setError] = useState('')
  useEffect(() => { if (existing.data) setResult(existing.data); else if (!existing.loading) setResult(null) }, [existing.data, existing.loading])
  const generate = async () => { if (!project) return; setGenerating(true); setError(''); try { const generated = await api.generateYaml(project.id, description, focus); setResult(generated); notify(generated.valid ? 'AI generated firmware.ai.yaml and validation passed.' : 'AI generated YAML needs validation review.') } catch (err) { const message = err instanceof Error ? err.message : 'YAML generation failed'; setError(message); notify(message) } finally { setGenerating(false) } }
  const validate = async () => { if (!project || !result) return; try { const checked = await api.validateYaml(project.id, result.content); setResult(checked); notify(checked.valid ? 'YAML structure is valid.' : checked.errors.join(' ')) } catch (err) { notify(err instanceof Error ? err.message : 'YAML validation failed') } }
  if (!project) return <Empty title="No YAML project context" detail="Select a project before generating firmware.ai.yaml." icon={<FileCode2 size={27} />} />
  return <section className="yaml-layout"><div className="yaml-form"><div><p className="eyebrow">AI YAML GENERATOR</p><h2>Describe your firmware</h2><p>FirmSight sends selected repository evidence and your declared context to the configured YAML model. Observed facts remain distinct from requirements.</p></div><textarea className="yaml-description" value={description} onChange={event => setDescription(event.target.value)} placeholder="ESP32-S3 using ESP-IDF. measurement_task reads ADS1115. MQTT is monitoring only. Device must continue measuring without MQTT." /><div className="focus-list">{['freertos', 'concurrency', 'memory', 'mqtt', 'ota', 'security'].map(item => <label key={item}><input type="checkbox" checked={focus.includes(item)} onChange={() => setFocus(current => current.includes(item) ? current.filter(value => value !== item) : [...current, item])} />{item}</label>)}</div><button className="primary" disabled={!description.trim() || generating} onClick={() => void generate()}>{generating ? <LoaderCircle className="spin" size={16} /> : <FileCode2 size={16} />}{generating ? 'Generating with AI…' : 'Generate with AI'}</button>{error && <ErrorState error={error} retry={() => void generate()} />}</div><div className="yaml-output"><div className="section-head"><div><h2>firmware.ai.yaml</h2><p>{result ? (result.valid ? 'AI output passed structural checks' : result.errors.join(' ')) : 'No project context file has been generated.'}</p></div>{result && <button onClick={() => void validate()}><ShieldCheck size={16} /> Validate</button>}</div>{generating ? <div className="ai-run-state yaml-running"><LoaderCircle className="spin" size={19} /><div><p className="eyebrow">YAML GENERATION IN PROGRESS</p><strong>Inspecting indexed repository evidence</strong><p>Waiting for the configured YAML Generator model. A deterministic template is not used.</p></div></div> : existing.loading ? <Loading /> : existing.error ? <ErrorState error={existing.error} retry={existing.run} /> : result ? <Editor height="560px" language="yaml" theme="vs" value={result.content} onChange={content => setResult(current => current ? { ...current, content: content ?? '' } : current)} options={{ minimap: { enabled: false }, fontSize: 13 }} /> : <Empty title="firmware.ai.yaml has not been generated." detail="FirmSight can function without it, but an approved project context file improves review relevance." action={<button onClick={() => document.querySelector<HTMLTextAreaElement>('.yaml-description')?.focus()}>Describe firmware</button>} icon={<FileCode2 size={27} />} />}</div></section>
}


const REVIEW_CONTEXT_OPTIONS = [
  { value: 12_000, label: '12,000 — Compact', detail: 'Lighter requests; more batches.' },
  { value: 18_000, label: '18,000 — Conservative', detail: 'Lower latency with useful context.' },
  { value: 24_000, label: '24,000 — Balanced', detail: 'A practical default for most gateways.' },
  { value: 32_000, label: '32,000 — Extended', detail: 'More context per request; slower.' },
  { value: 42_000, label: '42,000 — Maximum', detail: 'Current compatibility default.' },
]

function SettingsPage() {
  const settings = useAsync(api.settings, [])
  const [provider, setProvider] = useState('')
  const [endpoint, setEndpoint] = useState('')
  const [models, setModels] = useState<Record<string, string>>({})
  const [message, setMessage] = useState('')
  const [saving, setSaving] = useState(false)
  const [reviewContextChars, setReviewContextChars] = useState(42_000)
  const [structuredOutputMode, setStructuredOutputMode] = useState<'PROMPT_ONLY' | 'JSON_OBJECT' | 'JSON_SCHEMA'>('PROMPT_ONLY')
  const [reasoningEffort, setReasoningEffort] = useState<'UNSPECIFIED' | 'LOW' | 'MEDIUM' | 'HIGH'>('UNSPECIFIED')
  const [investigatorMaxTokens, setInvestigatorMaxTokens] = useState<OutputBudget>(2000)
  const [verifierMaxTokens, setVerifierMaxTokens] = useState<OutputBudget>(1200)
  const [reviewParallelRequests, setReviewParallelRequests] = useState(2)

  useEffect(() => {
    if (settings.data) {
      setProvider(settings.data.provider)
      setEndpoint(settings.data.endpoint)
      setModels(settings.data.models)
      setReviewContextChars(settings.data.review_context_chars)
      setStructuredOutputMode(settings.data.structured_output_mode ?? 'PROMPT_ONLY')
      setReasoningEffort(settings.data.reasoning_effort ?? 'UNSPECIFIED')
      setInvestigatorMaxTokens(settings.data.investigator_max_tokens ?? 2000)
      setVerifierMaxTokens(settings.data.verifier_max_tokens ?? 1200)
      setReviewParallelRequests(settings.data.review_parallel_requests ?? 2)
    }
  }, [settings.data])

  const applyNineRouterPreset = () => {
    setProvider('9router')
    setEndpoint('http://127.0.0.1:20128/v1/chat/completions')
    setMessage('Local 9router preset applied. Choose the model exposed by your router, then save.')
  }

  const save = async () => {
    setMessage('')
    setSaving(true)
    try {
      const updated = await api.updateSettings(provider.trim(), endpoint.trim(), models, reviewContextChars, structuredOutputMode, reasoningEffort, investigatorMaxTokens, verifierMaxTokens, reviewParallelRequests)
      settings.setData(updated)
      setMessage('Provider configuration saved. New AI requests will use it.')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Unable to save provider configuration.')
    } finally {
      setSaving(false)
    }
  }

  return <section className="page-stack">
    <div className="settings-form">
      <div>
        <p className="eyebrow">AI GATEWAY</p>
        <h2>Connect your model gateway</h2>
        <p>FirmSight sends requests through one OpenAI-compatible endpoint. Credentials stay in the backend environment.</p>
      </div>
      {settings.loading ? <Loading /> : settings.error ? <ErrorState error={settings.error} retry={settings.run} /> : settings.data && <>
        <div className="provider-presets">
          <div className="provider-preset-copy">
            <div><p className="eyebrow">QUICK SETUP</p><h3>Local 9router</h3></div>
            <p>Use the local gateway endpoint when 9router is running on this machine.</p>
          </div>
          <button type="button" onClick={applyNineRouterPreset}>Use local preset</button>
          <code>http://127.0.0.1:20128/v1/chat/completions</code>
          <span>Example model: <code>glm/glm-4-flash</code>. Confirm the exact ID in your 9router catalog.</span>
        </div>
        <label>Provider identifier<input value={provider} onChange={event => setProvider(event.target.value)} placeholder="openrouter, 9router, or openai-compatible" /></label>
        <label>Chat completions endpoint<input value={endpoint} onChange={event => setEndpoint(event.target.value)} placeholder="https://…/v1/chat/completions" /></label>
        <div className="model-grid">{Object.keys(models).map(role => <label key={role}>{role.replaceAll('_', ' ')}<input value={models[role] ?? ''} onChange={event => setModels(current => ({ ...current, [role]: event.target.value }))} placeholder="provider/model" /></label>)}</div>
        <label>Review context per AI request<select value={reviewContextChars} onChange={event => setReviewContextChars(Number(event.target.value))}>{REVIEW_CONTEXT_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select><span className="field-hint">{REVIEW_CONTEXT_OPTIONS.find(option => option.value === reviewContextChars)?.detail} Smaller requests can reduce gateway timeouts, but create more batches. Every reviewable file remains covered.</span></label>
        <div className="settings-two-column">
          <label>Structured response mode<select value={structuredOutputMode} onChange={event => setStructuredOutputMode(event.target.value as typeof structuredOutputMode)}><option value="PROMPT_ONLY">Prompt only — widest compatibility</option><option value="JSON_OBJECT">JSON object — provider enforced</option><option value="JSON_SCHEMA">JSON schema — strictest validation</option></select><span className="field-hint">Use provider-enforced modes only when your gateway documents support for <code>response_format</code>. Prompt-only remains the safest default.</span></label>
          <label>Reasoning effort<select value={reasoningEffort} onChange={event => setReasoningEffort(event.target.value as typeof reasoningEffort)}><option value="UNSPECIFIED">Provider default</option><option value="LOW">Low</option><option value="MEDIUM">Medium</option><option value="HIGH">High</option></select><span className="field-hint">This is a single provider hint. Final answer and reasoning share the configured token limit.</span></label>
        </div>
        <div className="settings-two-column">
          <label>Investigator output budget<select value={investigatorMaxTokens} onChange={event => setInvestigatorMaxTokens(event.target.value === 'PROVIDER_DEFAULT' ? 'PROVIDER_DEFAULT' : Number(event.target.value))}><option value="PROVIDER_DEFAULT">Provider default — no FirmSight output cap</option><option value={1200}>1,200 tokens</option><option value={1600}>1,600 tokens</option><option value={2000}>2,000 tokens — recommended</option><option value={2400}>2,400 tokens</option><option value={3200}>3,200 tokens</option></select><span className="field-hint">This omits FirmSight's <code>max_tokens</code> cap so the gateway can choose its native limit. It can improve completion headroom, but may increase latency/cost and remains subject to provider/model limits. Increase a numeric budget when diagnostics report <code>OUTPUT_LIMIT_BEFORE_FINAL</code>.</span></label>
          <label>Verifier output budget<select value={verifierMaxTokens} onChange={event => setVerifierMaxTokens(event.target.value === 'PROVIDER_DEFAULT' ? 'PROVIDER_DEFAULT' : Number(event.target.value))}><option value="PROVIDER_DEFAULT">Provider default — no FirmSight output cap</option><option value={800}>800 tokens</option><option value={1200}>1,200 tokens — recommended</option><option value={1600}>1,600 tokens</option><option value={2000}>2,000 tokens</option></select><span className="field-hint">This removes FirmSight's cap for this verification request; provider limits, latency, and usage charges still apply. A numeric value gives more predictable cost and timing.</span></label>
        </div>
        <label>Parallel review requests<select value={reviewParallelRequests} onChange={event => setReviewParallelRequests(Number(event.target.value))}><option value={1}>1 — conservative</option><option value={2}>2 — recommended</option><option value={3}>3 — fastest</option></select><span className="field-hint">Shared limit for Investigator and Verifier requests. Lower it when the gateway rate-limits or times out.</span></label>
        {reasoningEffort === 'HIGH' && typeof investigatorMaxTokens === 'number' && investigatorMaxTokens < 2000 && <div className="settings-warning" role="status"><TriangleAlert size={16} /> High reasoning with an Investigator budget below 2,000 tokens can exhaust the output before the final structured response. Increase the budget or choose Provider default/Low for a new review.</div>}
        <div className="key-status"><ShieldCheck size={17} /><div><strong>{settings.data.api_key_configured ? `Server key configured (${settings.data.api_key_masked})` : 'No server API key configured'}</strong><span>Set <code>{settings.data.api_key_environment}</code> in the backend environment; the key is never sent to or stored by this UI.</span></div></div>
        {message && <div className={message.includes('saved') || message.includes('preset applied') ? 'toast' : 'error'} role="status">{message}</div>}
        <button className="primary" disabled={!provider.trim() || !endpoint.trim() || saving} onClick={() => void save()}>{saving ? <LoaderCircle className="spin" size={16} /> : <Check size={16} />} {saving ? 'Saving provider settings…' : 'Save provider settings'}</button>
      </>}
    </div>
    <div className="settings-info"><h2>Provider examples</h2><dl className="settings-grid"><div><dt>OpenRouter</dt><dd>Provider: <code>openrouter</code><br />Endpoint: <code>https://openrouter.ai/api/v1/chat/completions</code></dd></div><div><dt>Other OpenAI-compatible API</dt><dd>Provider: <code>openai-compatible</code><br />Endpoint: your provider’s full <code>/chat/completions</code> URL</dd></div></dl></div>
    <div className="settings-info"><h2>Security boundary</h2><p>Imported source is treated as untrusted data. FirmSight does not execute firmware, shell scripts, build commands, or binaries during indexing or review.</p></div>
  </section>
}
