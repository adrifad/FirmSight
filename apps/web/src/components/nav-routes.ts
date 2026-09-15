import { BrainCircuit, Code2, FileCode2, FolderKanban, LayoutDashboard, LayoutGrid, MessageSquareCode, Network, ScanSearch, Settings2, TriangleAlert, type LucideIcon } from 'lucide-react'

export type Page = 'dashboard' | 'projects' | 'overview' | 'code' | 'review' | 'findings' | 'chat' | 'architecture' | 'memory' | 'yaml' | 'settings'

export interface NavRoute {
  id: Page
  label: string
  icon: LucideIcon
}

/** Single source of truth for every reachable route. The compact desktop bar
    and the off-canvas drawer both render these entries so labels, icons, and
    ids cannot diverge. */
export const workspaceNav: NavRoute[] = [
  { id: 'dashboard', label: 'Dashboard', icon: LayoutGrid },
  { id: 'projects', label: 'Projects', icon: FolderKanban },
]

export const projectNav: NavRoute[] = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'code', label: 'Code', icon: Code2 },
  { id: 'review', label: 'AI Review', icon: ScanSearch },
  { id: 'findings', label: 'Findings', icon: TriangleAlert },
  { id: 'chat', label: 'AI Chat', icon: MessageSquareCode },
  { id: 'architecture', label: 'Architecture', icon: Network },
  { id: 'memory', label: 'Project Intelligence', icon: BrainCircuit },
  { id: 'yaml', label: 'YAML', icon: FileCode2 },
]

export const settingsNav: NavRoute = { id: 'settings', label: 'Settings', icon: Settings2 }
