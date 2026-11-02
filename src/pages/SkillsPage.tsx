import type { CSSProperties } from 'react'

const skillGroups = [
  {
    title: 'Frontend',
    items: ['React', 'TypeScript', 'Vite', 'Accessible UI'],
  },
  {
    title: 'Backend',
    items: ['Cloudflare Workers', 'Pages Functions', 'API design', 'SQL'],
  },
  {
    title: 'Infrastructure',
    items: ['D1', 'R2', 'Wrangler', 'Deployment automation'],
  },
]

export function SkillsPage() {
  return (
    <section className="glass-section reveal-card" style={{ '--delay': '120ms' } as CSSProperties}>
      <p className="eyebrow">Skills</p>
      <h1>Capabilities</h1>
      <div className="skills-grid">
        {skillGroups.map((group) => (
          <article key={group.title} className="skill-card">
            <h2>{group.title}</h2>
            <div className="chip-grid">
              {group.items.map((item) => (
                <span key={item}>{item}</span>
              ))}
            </div>
          </article>
        ))}
      </div>
    </section>
  )
}
