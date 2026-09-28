import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { motion, useReducedMotion } from 'motion/react'
import '@google/model-viewer'

interface RobotButtonProps {
  onClick: () => void
  visible: boolean
  /** Optional override URL for the GLB. Defaults to packaged asset. */
  modelUrl?: string
}

const DEFAULT_MODEL_URL = new URL('../assets/sbfc_bot2.glb', import.meta.url).href
const STORAGE_KEY = 'chatbot-fab-pos'
const SIZE = 150
const MARGIN = 12
const DRAG_THRESHOLD = 6

type FabPos = { x: number; y: number }

function clampPos(x: number, y: number): FabPos {
  const maxX = Math.max(MARGIN, window.innerWidth - SIZE - MARGIN)
  const maxY = Math.max(MARGIN, window.innerHeight - SIZE - MARGIN)
  return {
    x: Math.min(maxX, Math.max(MARGIN, x)),
    y: Math.min(maxY, Math.max(MARGIN, y)),
  }
}

function defaultPos(): FabPos {
  if (typeof window === 'undefined') return { x: 24, y: 24 }
  return clampPos(
    window.innerWidth - SIZE - 24,
    window.innerHeight - SIZE - 24,
  )
}

function loadPos(): FabPos {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const p = JSON.parse(raw) as FabPos
      if (typeof p.x === 'number' && typeof p.y === 'number') return clampPos(p.x, p.y)
    }
  } catch {
    /* ignore */
  }
  return defaultPos()
}

function savePos(p: FabPos) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p))
  } catch {
    /* ignore */
  }
}

/**
 * Draggable floating 3D robot launcher.
 * Drag to reposition (saved); click/tap opens chat.
 */
export function RobotButton({ onClick, visible, modelUrl }: RobotButtonProps) {
  const reduceMotion = useReducedMotion()
  const [pos, setPos] = useState<FabPos>(() =>
    typeof window !== 'undefined' ? loadPos() : { x: 24, y: 24 },
  )
  const [hover, setHover] = useState(false)
  const [dragging, setDragging] = useState(false)
  const [modelError, setModelError] = useState(false)
  const viewerRef = useRef<HTMLElement | null>(null)
  const dragRef = useRef<{
    pointerId: number
    startX: number
    startY: number
    originX: number
    originY: number
    moved: boolean
  } | null>(null)

  const src = modelUrl ?? DEFAULT_MODEL_URL

  useEffect(() => {
    const onResize = () => setPos((p) => clampPos(p.x, p.y))
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [])

  useEffect(() => {
    const el = viewerRef.current
    if (!el) return
    const onErr = () => setModelError(true)
    el.addEventListener('error', onErr)
    return () => el.removeEventListener('error', onErr)
  }, [src, modelError])

  const onPointerDown = useCallback(
    (e: ReactPointerEvent<HTMLButtonElement>) => {
      if (e.button !== 0) return
      e.currentTarget.setPointerCapture(e.pointerId)
      dragRef.current = {
        pointerId: e.pointerId,
        startX: e.clientX,
        startY: e.clientY,
        originX: pos.x,
        originY: pos.y,
        moved: false,
      }
    },
    [pos.x, pos.y],
  )

  const onPointerMove = useCallback((e: ReactPointerEvent<HTMLButtonElement>) => {
    const d = dragRef.current
    if (!d || e.pointerId !== d.pointerId) return
    const dx = e.clientX - d.startX
    const dy = e.clientY - d.startY
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return
    d.moved = true
    setDragging(true)
    setPos(clampPos(d.originX + dx, d.originY + dy))
  }, [])

  const endDrag = useCallback(
    (e: ReactPointerEvent<HTMLButtonElement>) => {
      const d = dragRef.current
      if (!d || e.pointerId !== d.pointerId) return
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        /* ignore */
      }
      dragRef.current = null
      setDragging(false)
      if (d.moved) {
        setPos((p) => {
          const next = clampPos(p.x, p.y)
          savePos(next)
          return next
        })
      } else {
        onClick()
      }
    },
    [onClick],
  )

  return (
    <motion.div
      className="fixed z-[60] touch-none"
      style={{ left: pos.x, top: pos.y, width: SIZE, height: SIZE }}
      initial={false}
      animate={{
        opacity: visible ? 1 : 0,
        scale: visible ? 1 : 0.7,
        pointerEvents: visible ? 'auto' : 'none',
      }}
      transition={{
        type: 'spring',
        stiffness: 420,
        damping: 28,
        mass: 0.7,
      }}
      aria-hidden={!visible}
    >
      {/* Soft glow */}
      <div
        className="pointer-events-none absolute inset-2 rounded-full bg-ember/20 blur-xl"
        style={{ opacity: hover || dragging ? 0.9 : 0.45 }}
      />

      {!dragging && hover && (
        <div className="pointer-events-none absolute bottom-full left-1/2 mb-2 -translate-x-1/2">
          <div className="whitespace-nowrap rounded-full border border-line/80 bg-surface/95 px-3 py-1.5 font-sans text-[12px] font-medium text-content shadow-lg backdrop-blur-md">
            Drag to move · Tap to chat
          </div>
        </div>
      )}

      <button
        type="button"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        aria-label="Open AI Assistant. Drag to reposition."
        className={`relative flex h-full w-full cursor-grab items-center justify-center bg-transparent active:cursor-grabbing focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${dragging ? 'scale-105' : hover ? 'scale-110' : 'scale-100'
          } transition-transform duration-200 ease-[cubic-bezier(0.22,1,0.36,1)] ${!reduceMotion && !dragging ? 'animate-[botFloat_3.2s_ease-in-out_infinite]' : ''
          }`}
      >
        {!modelError ? (
          <model-viewer
            ref={(node) => {
              viewerRef.current = node as unknown as HTMLElement | null
            }}
            src={src}
            alt="AI robot assistant"
            loading="eager"
            reveal="auto"
            autoplay
            animation-name="Animation"
            disable-zoom
            disable-pan
            interaction-prompt="none"
            touch-action="none"
            shadow-intensity="0.85"
            exposure="1.1"
            camera-orbit="0deg 75deg 105%"
            camera-target="0m 0.05m 0m"
            field-of-view="30deg"
            auto-rotate={!reduceMotion && !dragging && !hover ? true : undefined}
            auto-rotate-delay={0}
            rotation-per-second={reduceMotion || dragging || hover ? '0deg' : '99deg'}

            style={{
              width: '100%',
              height: '100%',
              background: 'transparent',
              pointerEvents: 'none',
              ['--poster-color' as string]: 'transparent',
            }}
          />
        ) : (
          <svg viewBox="0 0 64 64" className="relative h-12 w-12 text-content" aria-hidden>
            <rect x="12" y="16" width="40" height="34" rx="10" fill="currentColor" opacity="0.95" />
            <line
              x1="32"
              y1="8"
              x2="32"
              y2="16"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
            />
            <circle cx="32" cy="6" r="3" fill="currentColor" />
            <rect x="18" y="26" width="28" height="14" rx="5" fill="#0C0C0D" opacity="0.85" />
            <circle cx="26" cy="33" r="3.2" fill="#4F5AC7" />
            <circle cx="38" cy="33" r="3.2" fill="#4F5AC7" />
            <path
              d="M26 44c2.5 2.5 9.5 2.5 12 0"
              fill="none"
              stroke="#0C0C0D"
              strokeWidth="2"
              strokeLinecap="round"
              opacity="0.5"
            />
            <rect x="6" y="28" width="6" height="12" rx="3" fill="currentColor" opacity="0.7" />
            <rect x="52" y="28" width="6" height="12" rx="3" fill="currentColor" opacity="0.7" />
          </svg>
        )}
      </button>

      <style>{`
        @keyframes botFloat {
          0%, 100% { transform: translateY(0); }
          50% { transform: translateY(-5px); }
        }
        model-viewer {
          --progress-bar-color: transparent;
          --progress-mask: transparent;
        }
      `}</style>
    </motion.div>
  )
}
