import { Link } from 'react-router-dom'
import type { CSSProperties } from 'react'
import { useEffect, useState } from 'react'
import { publicApi } from '../lib/api'
import type { HomeCard, SiteSettings } from '../types/models'

function clampUnits(value: number) {
  return Math.max(1, Math.min(4, value))
}

const defaultHeroSettings: Pick<SiteSettings, 'homeEyebrow' | 'homeTitle' | 'homeIntro'> = {
  homeEyebrow: 'Homepage',
  homeTitle: 'nbdevlab',
  homeIntro: 'Homepage cards are managed from Admin so you can build your own layout blocks.',
}

export function HomePage() {
  const [cards, setCards] = useState<HomeCard[]>([])
  const [hero, setHero] =
    useState<Pick<SiteSettings, 'homeEyebrow' | 'homeTitle' | 'homeIntro'>>(defaultHeroSettings)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      const [cardsResult, settingsResult] = await Promise.allSettled([
        publicApi.listHomeCards(),
        publicApi.getSiteSettings(),
      ])

      if (cardsResult.status === 'fulfilled') {
        setCards(cardsResult.value)
      } else {
        setError(
          cardsResult.reason instanceof Error
            ? cardsResult.reason.message
            : 'Failed to load homepage cards.',
        )
      }

      if (settingsResult.status === 'fulfilled') {
        setHero({
          homeEyebrow: settingsResult.value.homeEyebrow,
          homeTitle: settingsResult.value.homeTitle,
          homeIntro: settingsResult.value.homeIntro,
        })
      }
    })()
  }, [])

  return (
    <section className="glass-section reveal-card" style={{ '--delay': '120ms' } as CSSProperties}>
      <p className="eyebrow">{hero.homeEyebrow}</p>
      <h1>{hero.homeTitle}</h1>
      <p>{hero.homeIntro}</p>

      {error && <p className="notice notice--error">{error}</p>}

      {cards.length > 0 ? (
        <div className="home-card-grid" aria-label="Homepage cards">
          {cards.map((card) => {
            const isExternal = /^https?:\/\//i.test(card.ctaUrl)

            return (
              <article
                key={card.id}
                className="home-card"
                style={{
                  gridColumn: `span ${clampUnits(card.widthUnits)}`,
                  gridRow: `span ${clampUnits(card.heightUnits)}`,
                }}
              >
                {card.subtitle && <p className="eyebrow">{card.subtitle}</p>}
                <h2>{card.title}</h2>
                {card.body && <p>{card.body}</p>}
                {card.ctaLabel && card.ctaUrl && isExternal && (
                  <a className="button" href={card.ctaUrl} target="_blank" rel="noreferrer">
                    {card.ctaLabel}
                  </a>
                )}
                {card.ctaLabel && card.ctaUrl && !isExternal && (
                  <Link className="button" to={card.ctaUrl}>
                    {card.ctaLabel}
                  </Link>
                )}
              </article>
            )
          })}
        </div>
      ) : (
        <div className="skeleton-grid" aria-label="Homepage skeleton layout">
          <article className="skeleton-card">
            <div className="skeleton-line skeleton-line--title" />
            <div className="skeleton-line" />
            <div className="skeleton-line skeleton-line--short" />
            <Link className="button" to="/about">
              Open About
            </Link>
          </article>
          <article className="skeleton-card">
            <div className="skeleton-line skeleton-line--title" />
            <div className="skeleton-line" />
            <div className="skeleton-line skeleton-line--short" />
            <Link className="button" to="/projects">
              Open Projects
            </Link>
          </article>
          <article className="skeleton-card">
            <div className="skeleton-line skeleton-line--title" />
            <div className="skeleton-line" />
            <div className="skeleton-line skeleton-line--short" />
            <Link className="button" to="/skills">
              Open Skills
            </Link>
          </article>
          <article className="skeleton-card">
            <div className="skeleton-line skeleton-line--title" />
            <div className="skeleton-line" />
            <div className="skeleton-line skeleton-line--short" />
            <Link className="button" to="/contact">
              Open Contact
            </Link>
          </article>
        </div>
      )}
    </section>
  )
}
