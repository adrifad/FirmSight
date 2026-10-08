import { useCallback, useEffect, useState } from 'react'

/* Theme resolution for the app shell: an explicit choice stored in localStorage
   under 'firmsight-theme' wins; otherwise the OS prefers-color-scheme decides.
   The resolved value is written to <html data-theme="light|dark"> so theme.css
   can key off it. */

export type Theme = 'light' | 'dark'

const STORAGE_KEY = 'firmsight-theme'

function readStoredTheme(): Theme | null {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    return stored === 'light' || stored === 'dark' ? stored : null
  } catch {
    return null
  }
}

function systemTheme(): Theme {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function resolveInitialTheme(): Theme {
  return readStoredTheme() ?? systemTheme()
}

function persistTheme(theme: Theme) {
  try {
    window.localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // Storage unavailable (private mode, quota): the choice still holds for this session.
  }
}

function applyTheme(theme: Theme) {
  document.documentElement.dataset.theme = theme
}

/* Owns the light/dark theme: resolves the initial value from localStorage or
   the OS preference, keeps <html data-theme> in sync, follows live OS changes
   while the user has not made an explicit choice, and exposes toggleTheme. */
export function useTheme() {
  // Lazy initializer: localStorage and matchMedia are only read on first render.
  const [theme, setTheme] = useState<Theme>(resolveInitialTheme)

  useEffect(() => {
    applyTheme(theme)
  }, [theme])

  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onSystemChange = (event: MediaQueryListEvent) => {
      // Only follow the OS while the user has not picked a theme themselves.
      if (readStoredTheme() === null) setTheme(event.matches ? 'dark' : 'light')
    }
    media.addEventListener('change', onSystemChange)
    return () => media.removeEventListener('change', onSystemChange)
  }, [])

  const toggleTheme = useCallback(() => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    persistTheme(next)
    setTheme(next)
  }, [theme])

  return { theme, toggleTheme }
}
