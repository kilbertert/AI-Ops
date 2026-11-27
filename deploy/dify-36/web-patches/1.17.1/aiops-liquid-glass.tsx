'use client'

import type { CSSProperties, HTMLAttributes, PointerEvent, ReactNode } from 'react'
import { useCallback, useEffect, useRef } from 'react'

type Vec2 = { x: number; y: number }

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value))
}

function smoothStep(edge0: number, edge1: number, x: number): number {
  const t = clamp((x - edge0) / (edge1 - edge0), 0, 1)
  return t * t * (3 - 2 * t)
}

function roundedRectSDF(
  x: number,
  y: number,
  halfW: number,
  halfH: number,
  radius: number,
): number {
  const qx = Math.abs(x) - halfW + radius
  const qy = Math.abs(y) - halfH + radius
  return Math.min(Math.max(qx, qy), 0) + Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) - radius
}

/**
 * Generates an optical displacement PNG encoded as a data URI using a rounded-rect
 * convex-bezel SDF (adapted from rdev/liquid-glass-react ShaderDisplacementGenerator).
 * Red channel encodes horizontal pixel refraction; green/blue encode vertical refraction.
 */
function buildConvexDisplacementMap(width: number, height: number, bevel = 0.24): string {
  if (typeof document === 'undefined') return ''
  const canvas = document.createElement('canvas')
  canvas.width = width
  canvas.height = height
  const ctx = canvas.getContext('2d')
  if (!ctx) return ''

  const rawDx = new Float32Array(width * height)
  const rawDy = new Float32Array(width * height)
  let maxDelta = 1

  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const u = x / width - 0.5
      const v = y / height - 0.5
      // Convex bezel near perimeter; flat optical center keeps text sharp.
      const dist = roundedRectSDF(u, v, 0.34, 0.26, bevel)
      const lens = smoothStep(-0.16, 0.14, dist)
      const inward = smoothStep(0, 1, lens) * 0.22
      const refractedX = u * (1 - inward) + 0.5
      const refractedY = v * (1 - inward) + 0.5
      const dx = refractedX * width - x
      const dy = refractedY * height - y
      const idx = y * width + x
      rawDx[idx] = dx
      rawDy[idx] = dy
      maxDelta = Math.max(maxDelta, Math.abs(dx), Math.abs(dy))
    }
  }

  const imageData = ctx.createImageData(width, height)
  const pixels = imageData.data
  for (let y = 0; y < height; y++) {
    for (let x = 0; x < width; x++) {
      const idx = y * width + x
      const edgeDist = Math.min(x, y, width - 1 - x, height - 1 - y)
      const edgeFade = Math.min(1, edgeDist / 2)
      const dx = (rawDx[idx] ?? 0) * edgeFade
      const dy = (rawDy[idx] ?? 0) * edgeFade
      const r = clamp(Math.round((dx / maxDelta + 0.5) * 255), 0, 255)
      const g = clamp(Math.round((dy / maxDelta + 0.5) * 255), 0, 255)
      const p = idx * 4
      pixels[p] = r
      pixels[p + 1] = g
      pixels[p + 2] = g
      pixels[p + 3] = 255
    }
  }
  ctx.putImageData(imageData, 0, 0)
  return canvas.toDataURL('image/png')
}

function isChromiumSupportingBackdropSvg(): boolean {
  if (typeof navigator === 'undefined') return false
  const ua = navigator.userAgent.toLowerCase()
  const isFirefox = ua.includes('firefox') || ua.includes('gecko/')
  const isChromium =
    ua.includes('chrome/') || ua.includes('chromium/') || ua.includes('edg/') || ua.includes('arc/')
  return isChromium && !isFirefox
}

let cachedPillMap: string | null = null
let cachedSurfaceMap: string | null = null

/**
 * Mounts shared SVG optical refraction filter definitions once at the document root.
 * Activates `data-liquid-refract="on"` on `<html>` only in Chromium when reduced motion is off,
 * while Safari and Firefox gracefully use hardware-accelerated CSS blur + specular rims.
 */
export function LiquidGlassDefs() {
  const pillImgRef = useRef<SVGFEImageElement>(null)
  const surfaceImgRef = useRef<SVGFEImageElement>(null)

  useEffect(() => {
    if (typeof window === 'undefined') return
    if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    if (!isChromiumSupportingBackdropSvg()) return

    if (!cachedPillMap) cachedPillMap = buildConvexDisplacementMap(128, 48, 0.36)
    if (!cachedSurfaceMap) cachedSurfaceMap = buildConvexDisplacementMap(160, 120, 0.22)
    if (!cachedPillMap || !cachedSurfaceMap) return

    const applyMap = (node: SVGFEImageElement | null, url: string) => {
      if (!node) return
      node.setAttribute('href', url)
      node.setAttributeNS('http://www.w3.org/1999/xlink', 'xlink:href', url)
    }
    applyMap(pillImgRef.current, cachedPillMap)
    applyMap(surfaceImgRef.current, cachedSurfaceMap)
    document.documentElement.dataset.liquidRefract = 'on'

    return () => {
      delete document.documentElement.dataset.liquidRefract
    }
  }, [])

  return (
    <svg
      aria-hidden="true"
      width="0"
      height="0"
      className="pointer-events-none fixed h-0 w-0 overflow-hidden"
    >
      <defs>
        <filter
          id="aiops-liquid-pill"
          x="-8%"
          y="-16%"
          width="116%"
          height="132%"
          colorInterpolationFilters="sRGB"
        >
          <feImage
            ref={pillImgRef}
            href="data:image/gif;base64,R0lGODlhAQABAAAAACwAAAAAAQABAAA="
            preserveAspectRatio="none"
            result="LG_PILL_MAP"
          />
          <feDisplacementMap
            in="SourceGraphic"
            in2="LG_PILL_MAP"
            scale="7"
            xChannelSelector="R"
            yChannelSelector="G"
            result="LG_PILL_DISPLACED"
          />
          <feGaussianBlur in="LG_PILL_DISPLACED" stdDeviation="0.2" />
        </filter>

        <filter
          id="aiops-liquid-surface"
          x="-4%"
          y="-4%"
          width="108%"
          height="108%"
          colorInterpolationFilters="sRGB"
        >
          <feImage
            ref={surfaceImgRef}
            href="data:image/gif;base64,R0lGODlhAQABAAAAACwAAAAAAQABAAA="
            preserveAspectRatio="none"
            result="LG_SURFACE_MAP"
          />
          <feDisplacementMap
            in="SourceGraphic"
            in2="LG_SURFACE_MAP"
            scale="4"
            xChannelSelector="R"
            yChannelSelector="G"
            result="LG_SURFACE_DISPLACED"
          />
          <feGaussianBlur in="LG_SURFACE_DISPLACED" stdDeviation="0.12" />
        </filter>
      </defs>
    </svg>
  )
}

export type LiquidGlassVariant = 'hero' | 'card' | 'elevated' | 'pill' | 'toolbar' | 'rail'

const VARIANT_CLASSES: Record<LiquidGlassVariant, string> = {
  hero: 'aiops-glass-hero',
  card: 'aiops-glass-card',
  elevated: 'aiops-glass-elevated',
  pill: 'aiops-glass-pill',
  toolbar: 'aiops-glass-toolbar',
  rail: 'aiops-glass-rail',
}

type SurfaceTag = 'div' | 'section' | 'header' | 'aside' | 'article'

export type LiquidGlassSurfaceProps = HTMLAttributes<HTMLElement> & {
  as?: SurfaceTag
  variant?: LiquidGlassVariant
  refract?: boolean
  interactive?: boolean
  children: ReactNode
  className?: string
  style?: CSSProperties
} & { [key: `data-${string}`]: string | number | boolean | undefined }

/**
 * Composable Material 3 tonal surface layered with Liquid Glass frost, specular
 * rim lighting, and optional Chromium optical edge refraction + pointer sheen.
 */
export function LiquidGlassSurface({
  as: Tag = 'div',
  variant = 'card',
  refract = false,
  interactive = false,
  className = '',
  style,
  children,
  onPointerMove,
  onPointerLeave,
  ...rest
}: LiquidGlassSurfaceProps) {
  const rafRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    }
  }, [])

  const updatePointerGlow = useCallback((target: HTMLElement, pt: Vec2 | null) => {
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current)
    rafRef.current = requestAnimationFrame(() => {
      if (!pt) {
        target.style.setProperty('--lg-glow', '0')
        return
      }
      target.style.setProperty('--lg-mx', `${pt.x.toFixed(1)}%`)
      target.style.setProperty('--lg-my', `${pt.y.toFixed(1)}%`)
      target.style.setProperty('--lg-glow', '1')
    })
  }, [])

  const handlePointerMove = (e: PointerEvent<HTMLElement>) => {
    if (interactive) {
      const rect = e.currentTarget.getBoundingClientRect()
      if (rect.width > 0 && rect.height > 0) {
        const px = clamp(((e.clientX - rect.left) / rect.width) * 100, 0, 100)
        const py = clamp(((e.clientY - rect.top) / rect.height) * 100, 0, 100)
        updatePointerGlow(e.currentTarget, { x: px, y: py })
      }
    }
    onPointerMove?.(e)
  }

  const handlePointerLeave = (e: PointerEvent<HTMLElement>) => {
    if (interactive) updatePointerGlow(e.currentTarget, null)
    onPointerLeave?.(e)
  }

  const composedClassName = [
    VARIANT_CLASSES[variant],
    interactive ? 'aiops-glass-interactive' : '',
    refract ? 'aiops-glass-refract' : '',
    className,
  ]
    .filter(Boolean)
    .join(' ')

  return (
    <Tag
      className={composedClassName}
      style={style}
      onPointerMove={interactive ? handlePointerMove : onPointerMove}
      onPointerLeave={interactive ? handlePointerLeave : onPointerLeave}
      {...rest}
    >
      {children}
    </Tag>
  )
}
