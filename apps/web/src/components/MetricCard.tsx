import { type ReactNode } from 'react'

export function MetricCard({ label, value, detail, tone = 'default' }: { label: string; value: ReactNode; detail?: ReactNode; tone?: 'default' | 'attention' }) {
  return <article className={`metric-card${tone === 'attention' ? ' attention' : ''}`}><p>{label}</p><strong>{value}</strong>{detail ? <span>{detail}</span> : null}</article>
}
