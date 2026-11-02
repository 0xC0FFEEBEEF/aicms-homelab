import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { publicApi } from '../lib/api'
import type { Project } from '../types/models'

export function ProjectsPage() {
  const [projects, setProjects] = useState<Project[]>([])
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        setProjects(await publicApi.listProjects())
      } catch (requestError) {
        setError(requestError instanceof Error ? requestError.message : 'Failed to load projects')
      } finally {
        setIsLoading(false)
      }
    })()
  }, [])

  return (
    <section className="glass-section projects-page reveal-card" style={{ '--delay': '120ms' } as CSSProperties}>
      <h1>Projects</h1>
      {isLoading && <p className="notice">Loading projects...</p>}
      {error && <p className="notice notice--error">{error}</p>}
      <div className="project-grid project-grid--full">
        {projects.map((project) => (
          <article key={project.id} className="project-card">
            {project.images[0] && (
              <img src={project.images[0].url} alt={project.images[0].altText || project.title} />
            )}
            <h2>{project.title}</h2>
            <p>{project.summary}</p>
            <div className="project-tags">
              {project.tech.map((tech) => (
                <span key={`${project.id}-${tech}`}>{tech}</span>
              ))}
            </div>
            <div className="project-links">
              {project.demoUrl && (
                <a href={project.demoUrl} target="_blank" rel="noreferrer">
                  Demo
                </a>
              )}
              {project.repoUrl && (
                <a href={project.repoUrl} target="_blank" rel="noreferrer">
                  Repo
                </a>
              )}
            </div>
          </article>
        ))}
      </div>
      {!isLoading && projects.length === 0 && (
        <p className="notice">No published projects yet. Add projects from the admin panel.</p>
      )}
    </section>
  )
}
