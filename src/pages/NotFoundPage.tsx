import { Link } from 'react-router-dom'
import type { CSSProperties } from 'react'

export function NotFoundPage() {
  return (
    <section className="glass-section reveal-card" style={{ '--delay': '120ms' } as CSSProperties}>
      <h1>Page Not Found</h1>
      <p>The requested route does not exist.</p>
      <Link className="button button--solid" to="/">
        Back Home
      </Link>
    </section>
  )
}
