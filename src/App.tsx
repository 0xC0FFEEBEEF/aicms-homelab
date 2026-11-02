import { useEffect, useRef, useState } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router-dom'
import { IntroOverlay } from './components/IntroOverlay'
import { PublicLayout } from './components/PublicLayout'
import { usePrefersReducedMotion } from './hooks/usePrefersReducedMotion'
import { AdminPage } from './pages/AdminPage'
import { AboutPage } from './pages/AboutPage'
import { ContactPage } from './pages/ContactPage'
import { HomePage } from './pages/HomePage'
import { NotFoundPage } from './pages/NotFoundPage'
import { ProjectsPage } from './pages/ProjectsPage'
import { SkillsPage } from './pages/SkillsPage'
import { defaultIntroConfig } from './types/intro'

function getInitialIntroSeen(sessionKey: string) {
  if (typeof window === 'undefined') {
    return false
  }

  return sessionStorage.getItem(sessionKey) === '1'
}

function App() {
  const loadInDurationMs = 2800
  const reducedMotion = usePrefersReducedMotion()
  const [isIntroComplete, setIsIntroComplete] = useState(() =>
    getInitialIntroSeen(defaultIntroConfig.sessionKey),
  )
  const [isUnloadingToRain, setIsUnloadingToRain] = useState(false)
  const [isLoadingFromRain, setIsLoadingFromRain] = useState(false)
  const [skipDefaultReveal, setSkipDefaultReveal] = useState(false)
  const [introStartsAtGate, setIntroStartsAtGate] = useState(false)
  const unloadTimeoutRef = useRef<number | null>(null)
  const loadTimeoutRef = useRef<number | null>(null)

  const returnToRainGate = () => {
    if (isUnloadingToRain || !isIntroComplete) {
      return
    }

    setIsUnloadingToRain(true)
    setSkipDefaultReveal(false)
    unloadTimeoutRef.current = window.setTimeout(() => {
      setIsIntroComplete(false)
      setIntroStartsAtGate(true)
      setIsUnloadingToRain(false)
    }, 5000)
  }

  useEffect(() => {
    return () => {
      if (unloadTimeoutRef.current !== null) {
        window.clearTimeout(unloadTimeoutRef.current)
      }
      if (loadTimeoutRef.current !== null) {
        window.clearTimeout(loadTimeoutRef.current)
      }
    }
  }, [])

  return (
    <BrowserRouter>
      {!isIntroComplete && (
        <IntroOverlay
          config={defaultIntroConfig}
          reducedMotion={reducedMotion}
          startAtGate={introStartsAtGate}
          onComplete={() => {
            setIsIntroComplete(true)
            setIntroStartsAtGate(false)
            setIsLoadingFromRain(true)
            setSkipDefaultReveal(true)
            if (loadTimeoutRef.current !== null) {
              window.clearTimeout(loadTimeoutRef.current)
            }
            loadTimeoutRef.current = window.setTimeout(() => {
              setIsLoadingFromRain(false)
            }, loadInDurationMs)
          }}
        />
      )}

      <div
        className={`site-shell ${isIntroComplete ? 'site-shell--visible' : ''} ${
          isUnloadingToRain ? 'site-shell--unloading' : ''
        } ${isLoadingFromRain ? 'site-shell--loading-in' : ''} ${
          skipDefaultReveal ? 'site-shell--no-reveal' : ''
        }`}
      >
        <Routes>
          <Route
            element={
              <PublicLayout
                siteVisible={isIntroComplete}
                isUnloading={isUnloadingToRain}
                onReturnToRain={returnToRainGate}
              />
            }
          >
            <Route path="/" element={<HomePage />} />
            <Route path="/about" element={<AboutPage />} />
            <Route path="/projects" element={<ProjectsPage />} />
            <Route path="/skills" element={<SkillsPage />} />
            <Route path="/contact" element={<ContactPage />} />
          </Route>
          <Route path="/admin" element={<AdminPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </div>
    </BrowserRouter>
  )
}

export default App
