import { Moon, Sun } from 'lucide-react'
import { useTheme } from '../useTheme'

/* Icon-only theme toggle for the app topbar or sidebar footer. It reuses the
   shared .icon-button styling. The accessible label always names the theme the
   next press will switch to; the icon shows what you will get (sun in dark
   mode, moon in light mode). */
export function ThemeToggle() {
  const { theme, toggleTheme } = useTheme()
  const nextTheme = theme === 'dark' ? 'light' : 'dark'
  const label = `Switch to ${nextTheme} theme`
  return (
    <button
      type="button"
      className="icon-button theme-toggle"
      onClick={toggleTheme}
      aria-label={label}
      title={label}
    >
      {theme === 'dark'
        ? <Sun size={16} aria-hidden="true" />
        : <Moon size={16} aria-hidden="true" />}
    </button>
  )
}
