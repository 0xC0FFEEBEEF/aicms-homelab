interface ScrambleFrameInput {
  target: string
  elapsedMs: number
  durationMs: number
  charset: string
  rng?: () => number
}

export interface ScrambleFrame {
  word: string
  lockedCount: number
  complete: boolean
}

export function computeLockedCount(
  elapsedMs: number,
  durationMs: number,
  length: number,
): number {
  if (durationMs <= 0) {
    return length
  }

  const clamped = Math.max(0, Math.min(elapsedMs, durationMs))
  return Math.floor((clamped / durationMs) * length)
}

export function computeScrambleFrame({
  target,
  elapsedMs,
  durationMs,
  charset,
  rng = Math.random,
}: ScrambleFrameInput): ScrambleFrame {
  const lockedCount = computeLockedCount(elapsedMs, durationMs, target.length)
  const complete = elapsedMs >= durationMs

  if (complete) {
    return {
      word: target,
      lockedCount: target.length,
      complete: true,
    }
  }

  const chars = target.split('')
  const scrambled = chars
    .map((char, index) => {
      if (index < lockedCount) {
        return char
      }

      const randomIndex = Math.floor(rng() * charset.length)
      return charset[randomIndex] ?? char
    })
    .join('')

  return {
    word: scrambled,
    lockedCount,
    complete,
  }
}
