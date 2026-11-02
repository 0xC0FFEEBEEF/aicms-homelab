import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'

interface PublicLayoutProps {
  siteVisible: boolean
  isUnloading: boolean
  onReturnToRain: () => void
}

export function PublicLayout({ siteVisible, isUnloading, onReturnToRain }: PublicLayoutProps) {
  const location = useLocation()

  const routeKey = (() => {
    if (location.pathname.startsWith('/projects')) {
      return 'projects'
    }
    if (location.pathname.startsWith('/skills')) {
      return 'skills'
    }
    if (location.pathname.startsWith('/contact')) {
      return 'contact'
    }
    if (location.pathname.startsWith('/about')) {
      return 'about'
    }
    return 'home'
  })()

  return (
    <div className="public-shell">
      <div className="scene-backdrop" aria-hidden="true">
        <div className="scene-backdrop__mesh" />
      </div>

      <header className={`site-nav ${siteVisible ? 'site-nav--visible' : ''}`}>
        <Link to="/" className="site-brand">
          nbdevlab
        </Link>
        <div className="site-nav__right">
          <button
            type="button"
            className="button site-nav__rain-button"
            onClick={onReturnToRain}
            disabled={isUnloading}
          >
            {isUnloading ? 'Loading Rain…' : 'Rain Screen'}
          </button>
          <nav aria-label="Main">
            <ul className="site-nav__list">
              <li>
                <NavLink to="/about">About</NavLink>
              </li>
              <li>
                <NavLink to="/projects">Projects</NavLink>
              </li>
              <li>
                <NavLink to="/skills">Skills</NavLink>
              </li>
              <li>
                <NavLink to="/contact">Contact</NavLink>
              </li>
              <li>
                <NavLink to="/admin">Admin</NavLink>
              </li>
            </ul>
          </nav>
        </div>
      </header>

      <main className={`public-main public-main--${routeKey} ${siteVisible ? 'public-main--visible' : ''}`}>
        <Outlet />
      </main>
    </div>
  )
}
