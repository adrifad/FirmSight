import { type ReactNode } from 'react'
import { CircleAlert, FolderKanban, LoaderCircle } from 'lucide-react'

export function Loading() { return <div className="loading"><LoaderCircle className="spin" size={17} /> Loading project context…</div> }

export function Empty({ title, detail, action, icon = <FolderKanban size={27} />, compact = false }: { title: string; detail: string; action?: ReactNode; icon?: ReactNode; compact?: boolean }) {
  return <div className={`empty-state${compact ? ' compact' : ''}`}>{icon}<h3>{title}</h3><p>{detail}</p>{action}</div>
}

export function ErrorState({ error, retry }: { error: string; retry: () => void }) { return <div className="error"><CircleAlert size={17} /><span>{error}</span><button onClick={retry}>Retry</button></div> }
