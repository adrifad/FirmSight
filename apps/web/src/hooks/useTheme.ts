import { useCallback, useEffect, useState } from 'react'

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

function getInitialTheme(): Theme {
  if (typeof window === 'undefined') return 'light'
  const stored = readStoredTheme()
  if (stored) return stored
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

/**
 * Theme state for the app.
 *
 * - Applies the active theme as a `data-theme` attribute on <html>
 *   (consumed by the light/dark token sets in theme.css).
 * - Persists explicit choices to localStorage under "firmsight-theme".
 * - Until the user picks a theme, falls back to the OS
 *   `prefers-color-scheme` preference.
 */
export function useTheme() {
  const [theme, setThemeState] = useState<Theme>(getInitialTheme)

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
  }, [theme])

  const persistTheme = useCallback((next: Theme) => {
    try {
      window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // Storage unavailable (e.g. private mode): theme still applies for this session.
    }
  }, [])

  const setTheme = useCallback(
    (next: Theme) => {
      setThemeState(next)
      persistTheme(next)
    },
    [persistTheme],
  )

  const toggleTheme = useCallback(() => {
    const next: Theme = theme === 'dark' ? 'light' : 'dark'
    setThemeState(next)
    persistTheme(next)
  }, [theme, persistTheme])

  return { theme, setTheme, toggleTheme } as const
}
