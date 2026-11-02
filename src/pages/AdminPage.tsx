import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { adminApi, type HomeCardInput, type ProjectInput } from '../lib/api'
import type {
  ContactMessage,
  HomeCard,
  MessageStatus,
  Project,
  ProjectStatus,
  SiteSettings,
} from '../types/models'

type AdminTab = 'home' | 'projects' | 'contact' | 'messages'

interface ProjectFormState {
  title: string
  summary: string
  techCsv: string
  demoUrl: string
  repoUrl: string
  status: ProjectStatus
  publishAt: string
}

interface HomeCardFormState {
  title: string
  subtitle: string
  body: string
  ctaLabel: string
  ctaUrl: string
  widthUnits: string
  heightUnits: string
  isVisible: boolean
}

const blankProjectForm: ProjectFormState = {
  title: '',
  summary: '',
  techCsv: '',
  demoUrl: '',
  repoUrl: '',
  status: 'draft',
  publishAt: '',
}

const blankHomeCardForm: HomeCardFormState = {
  title: '',
  subtitle: '',
  body: '',
  ctaLabel: '',
  ctaUrl: '',
  widthUnits: '1',
  heightUnits: '1',
  isVisible: true,
}

const blankSiteSettings: SiteSettings = {
  email: '',
  phone: '',
  location: '',
  socials: {},
  contactIntro: '',
  homeEyebrow: 'Homepage',
  homeTitle: 'nbdevlab',
  homeIntro: 'Homepage cards are managed from Admin so you can build your own layout blocks.',
}

function normalizeProjectInput(form: ProjectFormState): ProjectInput {
  return {
    title: form.title.trim(),
    summary: form.summary.trim(),
    tech: form.techCsv
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean),
    demoUrl: form.demoUrl.trim() || null,
    repoUrl: form.repoUrl.trim() || null,
    status: form.status,
    publishAt: form.publishAt || null,
  }
}

function formFromProject(project: Project): ProjectFormState {
  return {
    title: project.title,
    summary: project.summary,
    techCsv: project.tech.join(', '),
    demoUrl: project.demoUrl ?? '',
    repoUrl: project.repoUrl ?? '',
    status: project.status,
    publishAt: project.publishAt ? project.publishAt.slice(0, 16) : '',
  }
}

function clampUnits(value: number) {
  return Math.max(1, Math.min(4, Math.round(value)))
}

function normalizeHomeCardInput(form: HomeCardFormState): HomeCardInput {
  return {
    title: form.title.trim(),
    subtitle: form.subtitle.trim(),
    body: form.body.trim(),
    ctaLabel: form.ctaLabel.trim(),
    ctaUrl: form.ctaUrl.trim(),
    widthUnits: clampUnits(Number(form.widthUnits)),
    heightUnits: clampUnits(Number(form.heightUnits)),
    isVisible: form.isVisible,
  }
}

function formFromHomeCard(card: HomeCard): HomeCardFormState {
  return {
    title: card.title,
    subtitle: card.subtitle,
    body: card.body,
    ctaLabel: card.ctaLabel,
    ctaUrl: card.ctaUrl,
    widthUnits: String(card.widthUnits),
    heightUnits: String(card.heightUnits),
    isVisible: card.isVisible,
  }
}

export function AdminPage() {
  const [isLoggedIn, setIsLoggedIn] = useState(false)
  const [isCheckingAuth, setIsCheckingAuth] = useState(true)
  const [loginUsername, setLoginUsername] = useState('')
  const [loginPassword, setLoginPassword] = useState('')
  const [activeTab, setActiveTab] = useState<AdminTab>('home')
  const [homeCards, setHomeCards] = useState<HomeCard[]>([])
  const [selectedHomeCardId, setSelectedHomeCardId] = useState<number | null>(null)
  const [homeCardForm, setHomeCardForm] = useState<HomeCardFormState>(blankHomeCardForm)
  const [projects, setProjects] = useState<Project[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null)
  const [projectForm, setProjectForm] = useState<ProjectFormState>(blankProjectForm)
  const [siteSettings, setSiteSettings] = useState<SiteSettings>(blankSiteSettings)
  const [socialsText, setSocialsText] = useState('{}')
  const [messages, setMessages] = useState<ContactMessage[]>([])
  const [notice, setNotice] = useState<string | null>(null)
  const [isBusy, setIsBusy] = useState(false)

  const selectedHomeCard = useMemo(
    () => homeCards.find((card) => card.id === selectedHomeCardId) ?? null,
    [homeCards, selectedHomeCardId],
  )

  const selectedProject = useMemo(
    () => projects.find((project) => project.id === selectedProjectId) ?? null,
    [projects, selectedProjectId],
  )

  const loadDashboard = async () => {
    setIsBusy(true)
    setNotice(null)

    try {
      const [homeCardData, projectData, settingsData, messageData] = await Promise.all([
        adminApi.listHomeCards(),
        adminApi.listProjects(),
        adminApi.getSiteSettings(),
        adminApi.listMessages(),
      ])

      setHomeCards(homeCardData)
      setProjects(projectData)
      setSiteSettings(settingsData)
      setSocialsText(JSON.stringify(settingsData.socials, null, 2))
      setMessages(messageData)

      if (homeCardData.length > 0) {
        const firstCard = homeCardData[0]
        setSelectedHomeCardId(firstCard.id)
        setHomeCardForm(formFromHomeCard(firstCard))
      } else {
        setSelectedHomeCardId(null)
        setHomeCardForm(blankHomeCardForm)
      }

      if (projectData.length > 0) {
        const firstProject = projectData[0]
        setSelectedProjectId(firstProject.id)
        setProjectForm(formFromProject(firstProject))
      } else {
        setSelectedProjectId(null)
        setProjectForm(blankProjectForm)
      }
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to load admin data.')
    } finally {
      setIsBusy(false)
    }
  }

  useEffect(() => {
    void (async () => {
      try {
        const session = await adminApi.getSession()
        setIsLoggedIn(session.authenticated)
      } catch {
        setIsLoggedIn(false)
      } finally {
        setIsCheckingAuth(false)
      }
    })()
  }, [])

  useEffect(() => {
    if (!isLoggedIn) {
      return
    }

    void loadDashboard()
  }, [isLoggedIn])

  const onLogin = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setIsBusy(true)
    setNotice(null)

    try {
      const result = await adminApi.login({
        username: loginUsername,
        password: loginPassword,
      })
      setIsLoggedIn(result.authenticated)
      setLoginPassword('')
      setNotice(result.authenticated ? 'Signed in.' : 'Sign in failed.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Sign in failed.')
      setIsLoggedIn(false)
    } finally {
      setIsBusy(false)
    }
  }

  const onLogout = async () => {
    setIsBusy(true)
    setNotice(null)

    try {
      await adminApi.logout()
    } catch {
      // Keep logout resilient even if request fails.
    } finally {
      setIsLoggedIn(false)
      setIsBusy(false)
    }
  }

  const onSelectHomeCard = (card: HomeCard) => {
    setSelectedHomeCardId(card.id)
    setHomeCardForm(formFromHomeCard(card))
    setNotice(null)
  }

  const onSaveHomeCard = async () => {
    const payload = normalizeHomeCardInput(homeCardForm)

    if (!payload.title) {
      setNotice('Home card title is required.')
      return
    }

    setIsBusy(true)
    setNotice(null)

    try {
      const saved = selectedHomeCard
        ? await adminApi.updateHomeCard(selectedHomeCard.id, payload)
        : await adminApi.createHomeCard(payload)

      await loadDashboard()
      setSelectedHomeCardId(saved.id)
      setNotice('Home card saved.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to save home card.')
    } finally {
      setIsBusy(false)
    }
  }

  const onDeleteHomeCard = async () => {
    if (!selectedHomeCard) {
      return
    }

    setIsBusy(true)
    setNotice(null)

    try {
      await adminApi.deleteHomeCard(selectedHomeCard.id)
      await loadDashboard()
      setNotice('Home card deleted.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to delete home card.')
    } finally {
      setIsBusy(false)
    }
  }

  const onMoveHomeCard = async (direction: -1 | 1) => {
    if (!selectedHomeCard) {
      return
    }

    const currentIndex = homeCards.findIndex((card) => card.id === selectedHomeCard.id)
    const targetIndex = currentIndex + direction

    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= homeCards.length) {
      return
    }

    const reordered = [...homeCards]
    const [item] = reordered.splice(currentIndex, 1)
    reordered.splice(targetIndex, 0, item)
    setHomeCards(reordered)

    try {
      await adminApi.reorderHomeCards(reordered.map((card) => card.id))
      setNotice('Home card order updated.')
      await loadDashboard()
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to reorder home cards.')
    }
  }

  const onSelectProject = (project: Project) => {
    setSelectedProjectId(project.id)
    setProjectForm(formFromProject(project))
    setNotice(null)
  }

  const onSaveProject = async () => {
    const payload = normalizeProjectInput(projectForm)
    if (!payload.title || !payload.summary) {
      setNotice('Title and summary are required.')
      return
    }

    setIsBusy(true)
    setNotice(null)

    try {
      const saved = selectedProject
        ? await adminApi.updateProject(selectedProject.id, payload)
        : await adminApi.createProject(payload)

      await loadDashboard()
      setSelectedProjectId(saved.id)
      setNotice('Project saved.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to save project.')
    } finally {
      setIsBusy(false)
    }
  }

  const onDeleteProject = async () => {
    if (!selectedProject) {
      return
    }

    setIsBusy(true)
    setNotice(null)

    try {
      await adminApi.deleteProject(selectedProject.id)
      await loadDashboard()
      setNotice('Project deleted.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to delete project.')
    } finally {
      setIsBusy(false)
    }
  }

  const onMoveProject = async (direction: -1 | 1) => {
    if (!selectedProject) {
      return
    }

    const currentIndex = projects.findIndex((project) => project.id === selectedProject.id)
    const targetIndex = currentIndex + direction

    if (currentIndex < 0 || targetIndex < 0 || targetIndex >= projects.length) {
      return
    }

    const reordered = [...projects]
    const [item] = reordered.splice(currentIndex, 1)
    reordered.splice(targetIndex, 0, item)

    setProjects(reordered)

    try {
      await adminApi.reorderProjects(reordered.map((project) => project.id))
      setNotice('Project order updated.')
      await loadDashboard()
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to update order.')
    }
  }

  const onUploadImages = async (files: FileList | null) => {
    if (!selectedProject || !files || files.length === 0) {
      return
    }

    setIsBusy(true)
    setNotice(null)

    try {
      await adminApi.uploadProjectImages(selectedProject.id, Array.from(files))
      await loadDashboard()
      setNotice('Images uploaded.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Image upload failed.')
    } finally {
      setIsBusy(false)
    }
  }

  const onDeleteImage = async (imageId: number) => {
    setIsBusy(true)

    try {
      await adminApi.deleteImage(imageId)
      await loadDashboard()
      setNotice('Image deleted.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Image delete failed.')
    } finally {
      setIsBusy(false)
    }
  }

  const onSaveContactSettings = async () => {
    setIsBusy(true)
    setNotice(null)

    try {
      const socials = JSON.parse(socialsText) as Record<string, string>
      const payload: SiteSettings = {
        ...siteSettings,
        socials,
      }
      const saved = await adminApi.updateSiteSettings(payload)
      setSiteSettings(saved)
      setSocialsText(JSON.stringify(saved.socials, null, 2))
      setNotice('Contact settings saved.')
    } catch (requestError) {
      setNotice(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to save contact settings. Ensure socials is valid JSON.',
      )
    } finally {
      setIsBusy(false)
    }
  }

  const onSaveHomeHeader = async () => {
    setIsBusy(true)
    setNotice(null)

    try {
      const payload: SiteSettings = {
        ...siteSettings,
        homeEyebrow: siteSettings.homeEyebrow.trim() || 'Homepage',
        homeTitle: siteSettings.homeTitle.trim() || 'nbdevlab',
        homeIntro:
          siteSettings.homeIntro.trim() ||
          'Homepage cards are managed from Admin so you can build your own layout blocks.',
      }

      const saved = await adminApi.updateSiteSettings(payload)
      setSiteSettings(saved)
      setNotice('Homepage main card content saved.')
    } catch (requestError) {
      setNotice(
        requestError instanceof Error
          ? requestError.message
          : 'Failed to save homepage main card content.',
      )
    } finally {
      setIsBusy(false)
    }
  }

  const onUpdateMessageStatus = async (messageId: number, status: MessageStatus) => {
    setIsBusy(true)

    try {
      const updated = await adminApi.updateMessageStatus(messageId, status)
      setMessages((current) =>
        current.map((message) => (message.id === messageId ? updated : message)),
      )
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to update message.')
    } finally {
      setIsBusy(false)
    }
  }

  if (isCheckingAuth) {
    return (
      <section className="admin-login">
        <h1>Admin Login</h1>
        <p>Checking session...</p>
      </section>
    )
  }

  if (!isLoggedIn) {
    return (
      <section className="admin-login">
        <h1>Admin Login</h1>
        <p>Sign in with the credentials configured in Worker secrets.</p>
        <Link className="button admin-login__back-link" to="/">
          Back To Homepage
        </Link>
        <form className="admin-login__form" onSubmit={(event) => void onLogin(event)}>
          <label>
            Username
            <input
              type="text"
              name="username"
              value={loginUsername}
              onChange={(event) => setLoginUsername(event.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              name="password"
              value={loginPassword}
              onChange={(event) => setLoginPassword(event.target.value)}
              required
            />
          </label>
          <button className="button button--solid" type="submit" disabled={isBusy}>
            Enter Dashboard
          </button>
        </form>
        {notice && <p className="notice">{notice}</p>}
      </section>
    )
  }

  return (
    <section className="admin-shell">
      <header className="admin-shell__header">
        <h1>Admin CMS</h1>
        <div className="admin-shell__header-actions">
          <Link className="button" to="/">
            Back To Homepage
          </Link>
          <button className="button" type="button" onClick={() => void loadDashboard()}>
            Refresh
          </button>
          <button className="button" type="button" onClick={() => void onLogout()}>
            Logout
          </button>
        </div>
      </header>

      <nav className="admin-tabs" aria-label="Admin sections">
        <button
          className={activeTab === 'home' ? 'is-active' : ''}
          type="button"
          onClick={() => setActiveTab('home')}
        >
          Home Layout
        </button>
        <button
          className={activeTab === 'projects' ? 'is-active' : ''}
          type="button"
          onClick={() => setActiveTab('projects')}
        >
          Projects
        </button>
        <button
          className={activeTab === 'contact' ? 'is-active' : ''}
          type="button"
          onClick={() => setActiveTab('contact')}
        >
          Contact
        </button>
        <button
          className={activeTab === 'messages' ? 'is-active' : ''}
          type="button"
          onClick={() => setActiveTab('messages')}
        >
          Messages
        </button>
      </nav>

      {notice && <p className="notice">{notice}</p>}
      {isBusy && <p className="notice">Working...</p>}

      {activeTab === 'home' && (
        <div className="admin-grid">
          <aside className="admin-list">
            <button
              className="button button--solid"
              type="button"
              onClick={() => {
                setSelectedHomeCardId(null)
                setHomeCardForm(blankHomeCardForm)
              }}
            >
              New Home Card
            </button>
            <ul>
              {homeCards.map((card) => (
                <li key={card.id}>
                  <button
                    type="button"
                    className={selectedHomeCardId === card.id ? 'is-active' : ''}
                    onClick={() => onSelectHomeCard(card)}
                  >
                    <span>{card.title}</span>
                    <small>{card.widthUnits}x{card.heightUnits}</small>
                  </button>
                </li>
              ))}
            </ul>
          </aside>

          <div className="admin-editor">
            <h2>Main Card (Homepage Header)</h2>
            <label>
              Eyebrow
              <input
                type="text"
                value={siteSettings.homeEyebrow}
                onChange={(event) =>
                  setSiteSettings((current) => ({ ...current, homeEyebrow: event.target.value }))
                }
              />
            </label>
            <label>
              Title
              <input
                type="text"
                value={siteSettings.homeTitle}
                onChange={(event) =>
                  setSiteSettings((current) => ({ ...current, homeTitle: event.target.value }))
                }
              />
            </label>
            <label>
              Intro Text
              <textarea
                rows={3}
                value={siteSettings.homeIntro}
                onChange={(event) =>
                  setSiteSettings((current) => ({ ...current, homeIntro: event.target.value }))
                }
              />
            </label>
            <div className="admin-editor__actions">
              <button className="button button--solid" type="button" onClick={() => void onSaveHomeHeader()}>
                Save Main Card
              </button>
            </div>

            <h2>Layout Cards</h2>
            <label>
              Title
              <input
                type="text"
                value={homeCardForm.title}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, title: event.target.value }))
                }
              />
            </label>
            <label>
              Subtitle
              <input
                type="text"
                value={homeCardForm.subtitle}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, subtitle: event.target.value }))
                }
              />
            </label>
            <label>
              Body
              <textarea
                rows={4}
                value={homeCardForm.body}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, body: event.target.value }))
                }
              />
            </label>
            <label>
              CTA Label
              <input
                type="text"
                value={homeCardForm.ctaLabel}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, ctaLabel: event.target.value }))
                }
              />
            </label>
            <label>
              CTA URL (e.g. /projects)
              <input
                type="text"
                value={homeCardForm.ctaUrl}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, ctaUrl: event.target.value }))
                }
              />
            </label>
            <label>
              Width Units (1-4)
              <select
                value={homeCardForm.widthUnits}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, widthUnits: event.target.value }))
                }
              >
                <option value="1">1</option>
                <option value="2">2</option>
                <option value="3">3</option>
                <option value="4">4</option>
              </select>
            </label>
            <label>
              Height Units (1-4)
              <select
                value={homeCardForm.heightUnits}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, heightUnits: event.target.value }))
                }
              >
                <option value="1">1</option>
                <option value="2">2</option>
                <option value="3">3</option>
                <option value="4">4</option>
              </select>
            </label>
            <label className="admin-inline-checkbox">
              <input
                type="checkbox"
                checked={homeCardForm.isVisible}
                onChange={(event) =>
                  setHomeCardForm((current) => ({ ...current, isVisible: event.target.checked }))
                }
              />
              Visible on homepage
            </label>

            <div className="admin-editor__actions">
              <button className="button button--solid" type="button" onClick={() => void onSaveHomeCard()}>
                Save Home Card
              </button>
              <button className="button" type="button" onClick={() => void onDeleteHomeCard()}>
                Delete Home Card
              </button>
              <button className="button" type="button" onClick={() => void onMoveHomeCard(-1)}>
                Move Up
              </button>
              <button className="button" type="button" onClick={() => void onMoveHomeCard(1)}>
                Move Down
              </button>
            </div>
          </div>
        </div>
      )}

      {activeTab === 'projects' && (
        <div className="admin-grid">
          <aside className="admin-list">
            <button
              className="button button--solid"
              type="button"
              onClick={() => {
                setSelectedProjectId(null)
                setProjectForm(blankProjectForm)
              }}
            >
              New Project
            </button>
            <ul>
              {projects.map((project) => (
                <li key={project.id}>
                  <button
                    type="button"
                    className={selectedProjectId === project.id ? 'is-active' : ''}
                    onClick={() => onSelectProject(project)}
                  >
                    <span>{project.title}</span>
                    <small>{project.status}</small>
                  </button>
                </li>
              ))}
            </ul>
          </aside>

          <div className="admin-editor">
            <label>
              Title
              <input
                type="text"
                value={projectForm.title}
                onChange={(event) =>
                  setProjectForm((current) => ({ ...current, title: event.target.value }))
                }
              />
            </label>
            <label>
              Summary
              <textarea
                rows={5}
                value={projectForm.summary}
                onChange={(event) =>
                  setProjectForm((current) => ({ ...current, summary: event.target.value }))
                }
              />
            </label>
            <label>
              Tech (comma-separated)
              <input
                type="text"
                value={projectForm.techCsv}
                onChange={(event) =>
                  setProjectForm((current) => ({ ...current, techCsv: event.target.value }))
                }
              />
            </label>
            <label>
              Demo URL
              <input
                type="url"
                value={projectForm.demoUrl}
                onChange={(event) =>
                  setProjectForm((current) => ({ ...current, demoUrl: event.target.value }))
                }
              />
            </label>
            <label>
              Repo URL
              <input
                type="url"
                value={projectForm.repoUrl}
                onChange={(event) =>
                  setProjectForm((current) => ({ ...current, repoUrl: event.target.value }))
                }
              />
            </label>
            <label>
              Status
              <select
                value={projectForm.status}
                onChange={(event) =>
                  setProjectForm((current) => ({
                    ...current,
                    status: event.target.value as ProjectStatus,
                  }))
                }
              >
                <option value="draft">Draft</option>
                <option value="published">Published</option>
                <option value="scheduled">Scheduled</option>
              </select>
            </label>
            <label>
              Publish At (for scheduled)
              <input
                type="datetime-local"
                value={projectForm.publishAt}
                onChange={(event) =>
                  setProjectForm((current) => ({ ...current, publishAt: event.target.value }))
                }
              />
            </label>

            <div className="admin-editor__actions">
              <button className="button button--solid" type="button" onClick={() => void onSaveProject()}>
                Save Project
              </button>
              <button className="button" type="button" onClick={() => void onDeleteProject()}>
                Delete Project
              </button>
              <button className="button" type="button" onClick={() => void onMoveProject(-1)}>
                Move Up
              </button>
              <button className="button" type="button" onClick={() => void onMoveProject(1)}>
                Move Down
              </button>
            </div>

            {selectedProject && (
              <>
                <label>
                  Upload Images
                  <input
                    type="file"
                    accept="image/*"
                    multiple
                    onChange={(event) => void onUploadImages(event.target.files)}
                  />
                </label>
                <div className="admin-images">
                  {selectedProject.images.map((image) => (
                    <article key={image.id}>
                      <img src={image.url} alt={image.altText || 'Project'} />
                      <button className="button" type="button" onClick={() => void onDeleteImage(image.id)}>
                        Remove
                      </button>
                    </article>
                  ))}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {activeTab === 'contact' && (
        <div className="admin-panel">
          <label>
            Email
            <input
              type="email"
              value={siteSettings.email}
              onChange={(event) =>
                setSiteSettings((current) => ({ ...current, email: event.target.value }))
              }
            />
          </label>
          <label>
            Phone
            <input
              type="text"
              value={siteSettings.phone}
              onChange={(event) =>
                setSiteSettings((current) => ({ ...current, phone: event.target.value }))
              }
            />
          </label>
          <label>
            Location
            <input
              type="text"
              value={siteSettings.location}
              onChange={(event) =>
                setSiteSettings((current) => ({ ...current, location: event.target.value }))
              }
            />
          </label>
          <label>
            Contact Intro
            <textarea
              rows={3}
              value={siteSettings.contactIntro}
              onChange={(event) =>
                setSiteSettings((current) => ({ ...current, contactIntro: event.target.value }))
              }
            />
          </label>
          <label>
            Socials JSON
            <textarea
              rows={6}
              value={socialsText}
              onChange={(event) => setSocialsText(event.target.value)}
            />
          </label>
          <button className="button button--solid" type="button" onClick={() => void onSaveContactSettings()}>
            Save Contact Settings
          </button>
        </div>
      )}

      {activeTab === 'messages' && (
        <div className="admin-panel admin-panel--messages">
          {messages.length === 0 && <p className="notice">No messages yet.</p>}
          {messages.map((message) => (
            <article key={message.id} className="message-card">
              <header>
                <h2>{message.name}</h2>
                <p>{message.email}</p>
                <small>{new Date(message.createdAt).toLocaleString()}</small>
              </header>
              <p>{message.message}</p>
              <label>
                Status
                <select
                  value={message.status}
                  onChange={(event) =>
                    void onUpdateMessageStatus(message.id, event.target.value as MessageStatus)
                  }
                >
                  <option value="new">New</option>
                  <option value="read">Read</option>
                  <option value="archived">Archived</option>
                </select>
              </label>
            </article>
          ))}
        </div>
      )}
    </section>
  )
}
