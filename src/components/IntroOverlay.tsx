import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { computeScrambleFrame } from '../lib/introMachine'
import type { IntroConfig } from '../types/intro'
import { DigitalRain } from './DigitalRain'

type IntroPhase = 'scramble' | 'gate' | 'exiting' | 'done'

interface IntroOverlayProps {
  config: IntroConfig
  reducedMotion: boolean
  startAtGate?: boolean
  onComplete: () => void
}

export function IntroOverlay({ config, reducedMotion, startAtGate = false, onComplete }: IntroOverlayProps) {
  const skipScramble = startAtGate || (reducedMotion && config.reducedMotionSkipsScramble)
  const exitDurationMs = reducedMotion ? Math.min(config.exitDurationMs, 320) : config.exitDurationMs
  const [phase, setPhase] = useState<IntroPhase>(skipScramble ? 'gate' : 'scramble')
  const [displayWord, setDisplayWord] = useState(
    skipScramble ? config.targetWord : config.startWord,
  )
  const [glitchVariant, setGlitchVariant] = useState<0 | 1>(0)
  const buttonRef = useRef<HTMLButtonElement | null>(null)
  const exitTimeoutRef = useRef<number | null>(null)
  const glitchTimeoutRef = useRef<number | null>(null)

  const rainActive = phase === 'gate'
  const rainMode = 'idle'

  const phaseLabel = useMemo(() => {
    if (phase === 'scramble') {
      return 'Bootstrapping interface'
    }
    if (phase === 'gate') {
      return 'Interface ready'
    }
    if (phase === 'exiting') {
      return 'Entering site'
    }
    return 'Done'
  }, [phase])

  useEffect(() => {
    if (phase !== 'scramble') {
      return
    }

    const startTime = performance.now()

    const intervalId = window.setInterval(() => {
      const elapsedMs = performance.now() - startTime
      const frame = computeScrambleFrame({
        target: config.targetWord,
        elapsedMs,
        durationMs: config.scrambleDurationMs,
        charset: config.charset,
      })

      setDisplayWord(frame.word)

      if (frame.complete) {
        setDisplayWord(config.targetWord)
        setPhase('gate')
      }
    }, config.tickMs)

    return () => {
      window.clearInterval(intervalId)
    }
  }, [config.charset, config.scrambleDurationMs, config.targetWord, config.tickMs, phase])

  useEffect(() => {
    if (phase !== 'gate') {
      return
    }

    buttonRef.current?.focus()
  }, [phase])

  useEffect(() => {
    if (phase !== 'gate' || reducedMotion) {
      return
    }

    const triggerGlitch = () => {
      setGlitchVariant((current) => (current === 0 ? 1 : 0))
    }

    const scheduleNext = () => {
      const nextDelay = 3000 + Math.random() * 3000
      glitchTimeoutRef.current = window.setTimeout(() => {
        triggerGlitch()
        scheduleNext()
      }, nextDelay)
    }

    triggerGlitch()
    scheduleNext()

    return () => {
      if (glitchTimeoutRef.current !== null) {
        window.clearTimeout(glitchTimeoutRef.current)
      }
    }
  }, [phase, reducedMotion])

  const beginExit = useCallback(() => {
    if (phase !== 'gate') {
      return
    }

    setPhase('exiting')

    exitTimeoutRef.current = window.setTimeout(() => {
      sessionStorage.setItem(config.sessionKey, '1')
      setPhase('done')
      onComplete()
    }, exitDurationMs)
  }, [config.sessionKey, exitDurationMs, onComplete, phase])

  useEffect(() => {
    if (phase !== 'gate') {
      return
    }

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        beginExit()
      }
    }

    window.addEventListener('keydown', onKeyDown)

    return () => {
      window.removeEventListener('keydown', onKeyDown)
    }
  }, [beginExit, phase])

  useEffect(() => {
    return () => {
      if (exitTimeoutRef.current !== null) {
        window.clearTimeout(exitTimeoutRef.current)
      }
      if (glitchTimeoutRef.current !== null) {
        window.clearTimeout(glitchTimeoutRef.current)
      }
    }
  }, [])

  if (phase === 'done') {
    return null
  }

  return (
    <div
      className={`intro-overlay intro-overlay--${phase}`}
      data-phase={phase}
      style={{ '--intro-exit-duration': `${exitDurationMs}ms` } as CSSProperties}
    >
      <DigitalRain
        active={rainActive}
        reducedMotion={reducedMotion}
        density={config.rain.density}
        minSpeed={config.rain.minSpeed}
        maxSpeed={config.rain.maxSpeed}
        opacity={config.rain.opacity}
        fontSize={config.rain.fontSize}
        mode={rainMode}
      />
      <div className="intro-overlay__grain" aria-hidden="true" />
      <div className="intro-overlay__content" aria-live="polite">
        <p className="intro-overlay__status">{phaseLabel}</p>
        <h1 className="intro-overlay__word">{phase === 'gate' ? config.targetWord : displayWord}</h1>
        {phase === 'gate' && (
          <button
            ref={buttonRef}
            type="button"
            className={`intro-overlay__enter intro-overlay__enter--glitch-${glitchVariant}`}
            data-label="Enter Website"
            onClick={beginExit}
          >
            <span>Enter Website</span>
          </button>
        )}
      </div>
    </div>
  )
}
