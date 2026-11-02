import { useEffect, useRef } from 'react'

interface DigitalRainProps {
  active: boolean
  reducedMotion: boolean
  density: number
  minSpeed: number
  maxSpeed: number
  opacity: number
  fontSize: number
  mode?: 'idle' | 'burst-exit'
}

const CHARSET = '01NBDEVLAB[]{}<>/\\*#'
const IDLE_MAX_PIXEL_RATIO = 1.5
const EXIT_MAX_PIXEL_RATIO = 1.2
const EXIT_SPEED_MULTIPLIER = 10.4
const EXIT_SPEED_RAMP_MS = 180
const EXIT_CLEARANCE_MARGIN_PX = 24
const EXIT_SUBSTEP_PX = 1.8
const EXIT_STAMP_ALPHA = 0.54
const EXIT_CLEAR_ALPHA = 0.19
const EXIT_OPACITY_MULTIPLIER = 0.78
const EXIT_MAX_TAIL_LENGTH_RATIO = 1.15
const GLYPH_SPRITE_HEIGHT_RATIO = 1.45

function createGlyphSprites(fontSize: number) {
  return Array.from(CHARSET, (char) => {
    const canvas = document.createElement('canvas')
    canvas.width = Math.ceil(fontSize)
    canvas.height = Math.ceil(fontSize * GLYPH_SPRITE_HEIGHT_RATIO)

    const ctx = canvas.getContext('2d')
    if (!ctx) {
      return canvas
    }

    ctx.fillStyle = '#ffffff'
    ctx.font = `${fontSize}px "IBM Plex Mono", monospace`
    ctx.textBaseline = 'top'
    ctx.filter = 'blur(0.18px)'
    ctx.fillText(char, 0, 0)

    return canvas
  })
}

export function DigitalRain({
  active,
  reducedMotion,
  density,
  minSpeed,
  maxSpeed,
  opacity,
  fontSize,
  mode = 'idle',
}: DigitalRainProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const modeRef = useRef<'idle' | 'burst-exit'>(mode)
  const exitStartedAtRef = useRef<number | null>(null)

  useEffect(() => {
    modeRef.current = mode
    exitStartedAtRef.current = mode === 'burst-exit' ? performance.now() : null
  }, [mode])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !active || reducedMotion) {
      return
    }

    const ctx = canvas.getContext('2d')
    if (!ctx) {
      return
    }

    let frameId = 0
    let lastTimestamp = performance.now()
    let lastSizedMode = modeRef.current

    const sizeCanvas = () => {
      const pixelRatioCap = modeRef.current === 'burst-exit' ? EXIT_MAX_PIXEL_RATIO : IDLE_MAX_PIXEL_RATIO
      const ratio = Math.min(window.devicePixelRatio || 1, pixelRatioCap)
      const { width, height } = canvas.getBoundingClientRect()
      canvas.width = Math.floor(width * ratio)
      canvas.height = Math.floor(height * ratio)
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0)
    }

    sizeCanvas()

    const resizeObserver = new ResizeObserver(() => {
      sizeCanvas()
    })
    resizeObserver.observe(canvas)
    const glyphSprites = createGlyphSprites(fontSize)

    let columns = Math.max(1, Math.floor(canvas.clientWidth / fontSize))
    let offsets = Array.from({ length: columns }, () => Math.random() * -canvas.clientHeight)
    let speeds = Array.from(
      { length: columns },
      () => minSpeed + Math.random() * (maxSpeed - minSpeed),
    )
    let enabledColumns = Array.from({ length: columns }, () => Math.random() < density)

    const rebuildColumnsIfNeeded = () => {
      const nextColumns = Math.max(1, Math.floor(canvas.clientWidth / fontSize))
      if (columns === nextColumns) {
        return
      }

      columns = nextColumns
      offsets = Array.from({ length: columns }, () => Math.random() * -canvas.clientHeight)
      speeds = Array.from(
        { length: columns },
        () => minSpeed + Math.random() * (maxSpeed - minSpeed),
      )
      enabledColumns = Array.from({ length: columns }, () => Math.random() < density)
    }

    const draw = (timestamp: number) => {
      if (lastSizedMode !== modeRef.current) {
        sizeCanvas()
        lastSizedMode = modeRef.current
      }

      rebuildColumnsIfNeeded()

      const delta = Math.max(0, (timestamp - lastTimestamp) / 1000)
      lastTimestamp = timestamp

      const width = canvas.clientWidth
      const height = canvas.clientHeight
      const isBurstExit = modeRef.current === 'burst-exit'

      ctx.fillStyle = isBurstExit ? `rgba(0, 0, 0, ${EXIT_CLEAR_ALPHA})` : 'rgba(0, 0, 0, 0.12)'
      ctx.fillRect(0, 0, width, height)

      const baseGlyphAlpha = isBurstExit ? opacity * EXIT_OPACITY_MULTIPLIER : opacity
      ctx.imageSmoothingEnabled = true
      ctx.filter = 'none'

      let activeColumns = 0

      for (let index = 0; index < columns; index += 1) {
        const exitElapsedMs =
          isBurstExit && exitStartedAtRef.current !== null
            ? timestamp - exitStartedAtRef.current
            : 0
        const exitRampProgress = Math.min(1, exitElapsedMs / EXIT_SPEED_RAMP_MS)
        const easedExitRamp = 1 - (1 - exitRampProgress) * (1 - exitRampProgress)
        const speedMultiplier = isBurstExit
          ? 1 + (EXIT_SPEED_MULTIPLIER - 1) * easedExitRamp
          : 1

        if (!enabledColumns[index]) {
          if (!isBurstExit && Math.random() < 0.0025) {
            enabledColumns[index] = true
            offsets[index] = -Math.random() * height * 0.5
          }
          continue
        }

        const previousY = offsets[index]
        offsets[index] += speeds[index] * speedMultiplier * delta
        const x = index * fontSize
        const y = offsets[index]
        const glyph = glyphSprites[Math.floor(Math.random() * glyphSprites.length)]
        ctx.globalAlpha = baseGlyphAlpha

        if (isBurstExit) {
          ctx.globalAlpha = baseGlyphAlpha * EXIT_STAMP_ALPHA
          const trailStartY = Math.max(
            previousY + EXIT_SUBSTEP_PX,
            y - fontSize * EXIT_MAX_TAIL_LENGTH_RATIO,
          )
          for (let trailY = trailStartY; trailY <= y; trailY += EXIT_SUBSTEP_PX) {
            ctx.drawImage(glyph, x, trailY)
          }
          ctx.globalAlpha = baseGlyphAlpha
        } else {
          ctx.drawImage(glyph, x, y)
        }

        const clearanceThreshold = isBurstExit
          ? height + EXIT_CLEARANCE_MARGIN_PX
          : height + Math.random() * 120

        if (y > clearanceThreshold) {
          if (isBurstExit) {
            enabledColumns[index] = false
            continue
          }

          offsets[index] = -Math.random() * height * 0.25
          speeds[index] = minSpeed + Math.random() * (maxSpeed - minSpeed)
          // Keep an active column active so the rain never starves out over time.
          enabledColumns[index] = true
        }

        if (enabledColumns[index]) {
          activeColumns += 1
        }
      }

      if (isBurstExit && activeColumns === 0) {
        ctx.clearRect(0, 0, width, height)
        return
      }

      frameId = window.requestAnimationFrame(draw)
    }

    frameId = window.requestAnimationFrame(draw)

    return () => {
      window.cancelAnimationFrame(frameId)
      resizeObserver.disconnect()
      ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight)
    }
  }, [active, density, fontSize, maxSpeed, minSpeed, opacity, reducedMotion])

  return (
    <canvas
      ref={canvasRef}
      className="digital-rain"
      data-active={active ? 'true' : 'false'}
      data-mode={mode}
      aria-hidden="true"
    />
  )
}
