import type {
  ApiEnvelope,
  ContactMessage,
  HomeCard,
  MessageStatus,
  Project,
  ProjectStatus,
  SiteSettings,
} from '../types/models'

export interface ProjectInput {
  title: string
  summary: string
  tech: string[]
  demoUrl: string | null
  repoUrl: string | null
  status: ProjectStatus
  publishAt: string | null
}

export interface HomeCardInput {
  title: string
  subtitle: string
  body: string
  ctaLabel: string
  ctaUrl: string
  widthUnits: number
  heightUnits: number
  isVisible: boolean
}

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    ...init,
  })
  let body: ApiEnvelope<T>

  try {
    body = (await response.json()) as ApiEnvelope<T>
  } catch {
    throw new Error(`Invalid API response for ${path}`)
  }

  if (!response.ok || !body.success) {
    throw new Error(body.error ?? `Request failed: ${response.status}`)
  }

  return body.data
}

async function sendJson<T, B>(path: string, method: string, payload: B): Promise<T> {
  return requestJson<T>(path, {
    method,
    credentials: 'same-origin',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  })
}

export const publicApi = {
  listHomeCards: () => requestJson<HomeCard[]>('/api/public/home-cards'),
  listProjects: () => requestJson<Project[]>('/api/public/projects'),
  getProject: (slug: string) => requestJson<Project>(`/api/public/projects/${encodeURIComponent(slug)}`),
  getSiteSettings: () => requestJson<SiteSettings>('/api/public/site-settings'),
  sendContactMessage: (payload: { name: string; email: string; message: string }) =>
    sendJson<{ id: number }, typeof payload>('/api/public/contact', 'POST', payload),
}

export const adminApi = {
  login: (payload: { username: string; password: string }) =>
    sendJson<{ authenticated: boolean }, { username: string; password: string }>(
      '/api/admin/auth/login',
      'POST',
      payload,
    ),
  logout: () =>
    requestJson<{ authenticated: boolean }>('/api/admin/auth/logout', {
      method: 'POST',
    }),
  getSession: () => requestJson<{ authenticated: boolean }>('/api/admin/auth/session'),
  listHomeCards: () => requestJson<HomeCard[]>('/api/admin/home-cards'),
  createHomeCard: (payload: HomeCardInput) =>
    sendJson<HomeCard, HomeCardInput>('/api/admin/home-cards', 'POST', payload),
  updateHomeCard: (id: number, payload: HomeCardInput) =>
    sendJson<HomeCard, HomeCardInput>(`/api/admin/home-cards/${id}`, 'PUT', payload),
  deleteHomeCard: (id: number) =>
    requestJson<{ id: number }>(`/api/admin/home-cards/${id}`, {
      method: 'DELETE',
    }),
  reorderHomeCards: (orderedIds: number[]) =>
    sendJson<{ updated: number }, { orderedIds: number[] }>(
      '/api/admin/home-cards/reorder',
      'POST',
      { orderedIds },
    ),
  listProjects: () => requestJson<Project[]>('/api/admin/projects'),
  createProject: (payload: ProjectInput) =>
    sendJson<Project, ProjectInput>('/api/admin/projects', 'POST', payload),
  updateProject: (id: number, payload: ProjectInput) =>
    sendJson<Project, ProjectInput>(`/api/admin/projects/${id}`, 'PUT', payload),
  deleteProject: (id: number) =>
    requestJson<{ id: number }>(`/api/admin/projects/${id}`, {
      method: 'DELETE',
    }),
  reorderProjects: (orderedIds: number[]) =>
    sendJson<{ updated: number }, { orderedIds: number[] }>(
      '/api/admin/projects/reorder',
      'POST',
      { orderedIds },
    ),
  uploadProjectImages: async (projectId: number, files: File[]) => {
    const formData = new FormData()
    files.forEach((file) => formData.append('images', file))

    return requestJson<Project>(`/api/admin/projects/${projectId}/images`, {
      method: 'POST',
      body: formData,
    })
  },
  deleteImage: (imageId: number) =>
    requestJson<{ id: number }>(`/api/admin/images/${imageId}`, {
      method: 'DELETE',
    }),
  getSiteSettings: () => requestJson<SiteSettings>('/api/admin/site-settings'),
  updateSiteSettings: (payload: SiteSettings) =>
    sendJson<SiteSettings, SiteSettings>('/api/admin/site-settings', 'PUT', payload),
  listMessages: () => requestJson<ContactMessage[]>('/api/admin/messages'),
  updateMessageStatus: (id: number, status: MessageStatus) =>
    sendJson<ContactMessage, { status: MessageStatus }>(
      `/api/admin/messages/${id}`,
      'PUT',
      { status },
    ),
}
