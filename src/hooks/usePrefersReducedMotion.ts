import { useEffect, useState } from 'react'

const QUERY = '(prefers-reduced-motion: reduce)'

export function usePrefersReducedMotion() {
  const [prefersReducedMotion, setPrefersReducedMotion] = useState<boolean>(() => {
    if (typeof window === 'undefined') {
      return false
    }
    return window.matchMedia(QUERY).matches
  })

  useEffect(() => {
    const media = window.matchMedia(QUERY)
    const onChange = (event: MediaQueryListEvent) => {
      setPrefersReducedMotion(event.matches)
    }

    media.addEventListener('change', onChange)

    return () => {
      media.removeEventListener('change', onChange)
    }
  }, [])

  return prefersReducedMotion
}
