import { projectNav, settingsNav, workspaceNav, type NavRoute, type Page } from './nav-routes'

export type { Page } from './nav-routes'

export function NavItem({ id, label, icon: Icon, active, disabled, variant = 'drawer', onNavigate }: NavRoute & { active: boolean; disabled?: boolean; variant?: 'drawer' | 'bar'; onNavigate: (page: Page) => void }) {
  return <button type="button" className={active ? 'active' : ''} disabled={disabled} title={label} aria-label={label} aria-current={active ? 'page' : undefined} onClick={() => onNavigate(id)}><Icon size={variant === 'bar' ? 15 : 16} /><span className={variant === 'bar' ? 'nav-pill-label' : 'sidebar-item-label'}>{label}</span></button>
}

export function NavGroup({ page, active, variant, onNavigate }: { page: Page; active: unknown; variant: 'drawer' | 'bar'; onNavigate: (page: Page) => void }) {
  return <>
    {workspaceNav.map(item => <NavItem key={item.id} {...item} variant={variant} active={page === item.id} onNavigate={onNavigate} />)}
    {variant === 'bar'
      ? <span className="desktop-nav-divider" aria-hidden="true" />
      : <p className="sidebar-group-label" title={active ? (active as { name: string }).name : undefined}>{active ? (active as { name: string }).name : 'Project workspace'}</p>}
    {projectNav.map(item => <NavItem key={item.id} {...item} variant={variant} active={page === item.id} disabled={!active} onNavigate={onNavigate} />)}
  </>
}

export function SettingsNavItem({ page, onNavigate }: { page: Page; onNavigate: (page: Page) => void }) {
  return <NavItem {...settingsNav} active={page === 'settings'} onNavigate={onNavigate} />
}

export function DesktopNav({ page, active, onNavigate }: { page: Page; active: unknown; onNavigate: (page: Page) => void }) {
  return <nav className="desktop-nav" aria-label="Primary"><NavGroup page={page} active={active} variant="bar" onNavigate={onNavigate} /></nav>
}
