import { useEffect, useState } from 'react'
import type { CSSProperties } from 'react'
import { publicApi } from '../lib/api'
import type { SiteSettings } from '../types/models'

const defaultSettings: SiteSettings = {
  email: 'you@example.com',
  phone: '',
  location: '',
  socials: {},
  contactIntro: 'Use the form below or reach out directly.',
  homeEyebrow: 'Homepage',
  homeTitle: 'nbdevlab',
  homeIntro: 'Homepage cards are managed from Admin so you can build your own layout blocks.',
}

export function ContactPage() {
  const [settings, setSettings] = useState<SiteSettings>(defaultSettings)
  const [notice, setNotice] = useState<string | null>(null)
  const [isSending, setIsSending] = useState(false)

  useEffect(() => {
    void (async () => {
      try {
        setSettings(await publicApi.getSiteSettings())
      } catch (requestError) {
        setNotice(requestError instanceof Error ? requestError.message : 'Failed to load contact data.')
      }
    })()
  }, [])

  const onSubmitContact = async (formData: FormData) => {
    const name = String(formData.get('name') ?? '').trim()
    const email = String(formData.get('email') ?? '').trim()
    const message = String(formData.get('message') ?? '').trim()

    if (!name || !email || !message) {
      setNotice('Please fill out all fields before sending.')
      return
    }

    setIsSending(true)
    setNotice(null)

    try {
      await publicApi.sendContactMessage({ name, email, message })
      setNotice('Message sent. I will get back to you soon.')
    } catch (requestError) {
      setNotice(requestError instanceof Error ? requestError.message : 'Failed to send message.')
    } finally {
      setIsSending(false)
    }
  }

  return (
    <section className="glass-section reveal-card" style={{ '--delay': '120ms' } as CSSProperties}>
      <p className="eyebrow">Contact</p>
      <h1>Get in touch</h1>
      <p>{settings.contactIntro}</p>

      <div className="contact-block">
        <p>
          <strong>Email:</strong> {settings.email}
        </p>
        {settings.phone && (
          <p>
            <strong>Phone:</strong> {settings.phone}
          </p>
        )}
        {settings.location && (
          <p>
            <strong>Location:</strong> {settings.location}
          </p>
        )}
        {Object.keys(settings.socials).length > 0 && (
          <ul className="social-list">
            {Object.entries(settings.socials).map(([name, value]) => (
              <li key={name}>
                <strong>{name}:</strong> {value}
              </li>
            ))}
          </ul>
        )}
      </div>

      <form
        className="contact-form"
        onSubmit={(event) => {
          event.preventDefault()
          const formData = new FormData(event.currentTarget)
          void onSubmitContact(formData)
          event.currentTarget.reset()
        }}
      >
        <label>
          Name
          <input name="name" type="text" required />
        </label>
        <label>
          Email
          <input name="email" type="email" required />
        </label>
        <label>
          Message
          <textarea name="message" rows={5} required />
        </label>
        <button type="submit" className="button button--solid" disabled={isSending}>
          {isSending ? 'Sending...' : 'Send Message'}
        </button>
      </form>

      {notice && <p className="notice">{notice}</p>}
    </section>
  )
}
