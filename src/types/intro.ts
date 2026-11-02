export interface IntroConfig {
  startWord: string
  targetWord: string
  scrambleDurationMs: number
  tickMs: number
  exitDurationMs: number
  charset: string
  sessionKey: string
  reducedMotionSkipsScramble: boolean
  rain: {
    density: number
    minSpeed: number
    maxSpeed: number
    opacity: number
    fontSize: number
  }
}

export const defaultIntroConfig: IntroConfig = {
  startWord: 'hgdeklac',
  targetWord: 'nbdevlab',
  scrambleDurationMs: 4400,
  tickMs: 55,
  exitDurationMs: 650,
  charset: 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
  sessionKey: 'nbdevlab:intro:seen',
  reducedMotionSkipsScramble: true,
  rain: {
    density: 0.33,
    minSpeed: 24,
    maxSpeed: 74,
    opacity: 0.2,
    fontSize: 15,
  },
}
