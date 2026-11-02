import { act, fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { IntroOverlay } from './IntroOverlay'
import type { IntroConfig } from '../types/intro'

const config: IntroConfig = {
  startWord: 'hgdeklac',
  targetWord: 'nbdevlab',
  scrambleDurationMs: 120,
  tickMs: 20,
  exitDurationMs: 80,
  charset: 'ABCDEF',
  sessionKey: 'intro-test-key',
  reducedMotionSkipsScramble: true,
  rain: {
    density: 0.2,
    minSpeed: 20,
    maxSpeed: 30,
    opacity: 0.2,
    fontSize: 12,
  },
}

describe('IntroOverlay', () => {
  beforeEach(() => {
    sessionStorage.clear()
  })

  it('shows gate only after scramble completion and activates digital rain', () => {
    vi.useFakeTimers()
    const complete = vi.fn()

    render(<IntroOverlay config={config} reducedMotion={false} onComplete={complete} />)

    expect(screen.queryByRole('button', { name: 'Enter Website' })).not.toBeInTheDocument()
    expect(screen.getByText('hgdeklac')).toBeInTheDocument()
    expect(document.querySelector('.digital-rain')?.getAttribute('data-active')).toBe('false')

    act(() => {
      vi.advanceTimersByTime(180)
    })

    expect(screen.getByRole('button', { name: 'Enter Website' })).toBeInTheDocument()
    expect(screen.getByText('nbdevlab')).toBeInTheDocument()
    expect(document.querySelector('.digital-rain')?.getAttribute('data-active')).toBe('true')

    vi.useRealTimers()
  })

  it('exits on button click and stores session flag', () => {
    vi.useFakeTimers()
    const complete = vi.fn()

    render(<IntroOverlay config={config} reducedMotion={false} onComplete={complete} />)

    act(() => {
      vi.advanceTimersByTime(180)
    })

    fireEvent.click(screen.getByRole('button', { name: 'Enter Website' }))

    expect(document.querySelector('.intro-overlay')?.getAttribute('data-phase')).toBe('exiting')

    act(() => {
      vi.advanceTimersByTime(90)
    })

    expect(complete).toHaveBeenCalledTimes(1)
    expect(sessionStorage.getItem(config.sessionKey)).toBe('1')

    vi.useRealTimers()
  })

  it('exits when Enter is pressed in gate state', () => {
    vi.useFakeTimers()
    const complete = vi.fn()

    render(<IntroOverlay config={config} reducedMotion={false} onComplete={complete} />)

    act(() => {
      vi.advanceTimersByTime(180)
    })

    fireEvent.keyDown(window, { key: 'Enter' })

    act(() => {
      vi.advanceTimersByTime(90)
    })

    expect(complete).toHaveBeenCalledTimes(1)

    vi.useRealTimers()
  })

  it('skips scramble when reduced motion is enabled and focuses CTA', () => {
    const complete = vi.fn()

    render(<IntroOverlay config={config} reducedMotion onComplete={complete} />)

    const button = screen.getByRole('button', { name: 'Enter Website' })
    expect(button).toBeInTheDocument()
    expect(button).toHaveFocus()
    expect(screen.getByText('nbdevlab')).toBeInTheDocument()
  })
})
