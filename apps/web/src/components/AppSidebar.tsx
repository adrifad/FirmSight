import { X } from 'lucide-react'
import type { Project } from '../api'
import logoUrl from '../assets/firmsight-logo.png'
import { DesktopNav, NavGroup, SettingsNavItem, type Page } from './navigation'
import { ThemeToggle } from './ThemeToggle'

export type { Page }
export { DesktopNav }

export function AppSidebar({ page, active, open, onNavigate, onClose }: { page: Page; active: Project | null; open: boolean; onNavigate: (page: Page) => void; onClose: () => void }) {
  return <>
    <div className={`sidebar-backdrop${open ? ' visible' : ''}`} onClick={onClose} aria-hidden="true" />
    <aside id="app-sidebar" className={`app-sidebar${open ? ' open' : ''}`} aria-label="Application navigation">
      <div className="sidebar-brand">
        <button type="button" onClick={() => onNavigate('projects')} aria-label="FirmSight home"><img src={logoUrl} alt="FirmSight" height={30} width={110} /></button>
        <button type="button" className="icon-button sidebar-close" onClick={onClose} aria-label="Close navigation"><X size={16} /></button>
      </div>
      <nav className="sidebar-nav" aria-label="Drawer navigation">
        <p className="sidebar-group-label">Workspace</p>
        <NavGroup page={page} active={active} variant="drawer" onNavigate={onNavigate} />
      </nav>
      <div className="sidebar-footer">
        <SettingsNavItem page={page} onNavigate={onNavigate} />
        {/* Rendered unconditionally (TASK-2026-0911): the theme toggle stays visible and clickable in every mode, including fullscreen. */}
        <ThemeToggle />
      </div>
    </aside>
  </>
}
