import type { CSSProperties } from 'react'

export function AboutPage() {
  return (
    <section className="glass-section reveal-card" style={{ '--delay': '120ms' } as CSSProperties}>
      <p className="eyebrow">About</p>
      <h1>About nbdevlab</h1>
      <p>
        I build pragmatic software with clear interfaces and production-focused design.
      </p>
      <p>
        This site is powered by Cloudflare Pages Functions with D1 + R2 so projects and contact
        data can be managed through the admin dashboard.
      </p>
    </section>
  )
}
