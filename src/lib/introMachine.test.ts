import { describe, expect, it } from 'vitest'
import { computeLockedCount, computeScrambleFrame } from './introMachine'

describe('introMachine', () => {
  it('locks characters left-to-right over time', () => {
    expect(computeLockedCount(0, 2000, 8)).toBe(0)
    expect(computeLockedCount(500, 2000, 8)).toBe(2)
    expect(computeLockedCount(1000, 2000, 8)).toBe(4)
    expect(computeLockedCount(2000, 2000, 8)).toBe(8)
  })

  it('resolves to target when complete', () => {
    const frame = computeScrambleFrame({
      target: 'nbdevlab',
      elapsedMs: 2200,
      durationMs: 2200,
      charset: 'ABC123',
      rng: () => 0,
    })

    expect(frame.complete).toBe(true)
    expect(frame.word).toBe('nbdevlab')
    expect(frame.lockedCount).toBe(8)
  })

  it('scrambles remaining characters while preserving locked prefix', () => {
    const frame = computeScrambleFrame({
      target: 'nbdevlab',
      elapsedMs: 1100,
      durationMs: 2200,
      charset: 'Z',
      rng: () => 0,
    })

    expect(frame.lockedCount).toBe(4)
    expect(frame.word.startsWith('nbde')).toBe(true)
    expect(frame.word).toBe('nbdeZZZZ')
  })
})
