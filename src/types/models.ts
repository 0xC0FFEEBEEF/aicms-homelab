export type ProjectStatus = 'draft' | 'published' | 'scheduled'

export interface ProjectImage {
  id: number
  projectId: number
  url: string
  altText: string
  position: number
  createdAt: string
}

export interface Project {
  id: number
  slug: string
  title: string
  summary: string
  tech: string[]
  demoUrl: string | null
  repoUrl: string | null
  status: ProjectStatus
  publishAt: string | null
  sortOrder: number
  createdAt: string
  updatedAt: string
  images: ProjectImage[]
}

export interface SiteSettings {
  email: string
  phone: string
  location: string
  socials: Record<string, string>
  contactIntro: string
  homeEyebrow: string
  homeTitle: string
  homeIntro: string
}

export interface HomeCard {
  id: number
  title: string
  subtitle: string
  body: string
  ctaLabel: string
  ctaUrl: string
  widthUnits: number
  heightUnits: number
  sortOrder: number
  isVisible: boolean
  createdAt: string
  updatedAt: string
}

export type MessageStatus = 'new' | 'read' | 'archived'

export interface ContactMessage {
  id: number
  name: string
  email: string
  message: string
  status: MessageStatus
  createdAt: string
}

export interface ApiEnvelope<T> {
  success: boolean
  data: T
  error?: string
}

export interface ReorderPayload {
  orderedIds: number[]
}
