import { useEffect, useRef, useState } from 'react'
import '@google/model-viewer'

interface RobotButtonProps {
  onClick: () => void
  visible: boolean
  /** Optional override URL for the GLB. Defaults to packaged asset. */
  modelUrl?: string
}

const DEFAULT_MODEL_URL = new URL('../assets/mini_bot.glb', import.meta.url).href

/**
 * Floating 3D robot launcher using mini_bot.glb (@google/model-viewer).
 * Transparent chrome — only the mini bot + its GLB animation are shown.
 * Click opens the chat window.
 */
export function RobotButton({ onClick, visible, modelUrl }: RobotButtonProps) {
  const [hover, setHover] = useState(false)
  const [reduceMotion, setReduceMotion] = useState(false)
  const [modelError, setModelError] = useState(false)
  const viewerRef = useRef<HTMLElement | null>(null)

  const src = modelUrl ?? DEFAULT_MODEL_URL

  useEffect(() => {
    const mq = window.matchMedia('(prefers-reduced-motion: reduce)')
    setReduceMotion(mq.matches)
    const fn = () => setReduceMotion(mq.matches)
    mq.addEventListener('change', fn)
    return () => mq.removeEventListener('change', fn)
  }, [])

  // Custom elements fire native events — React onError often doesn't bind.
  useEffect(() => {
    const el = viewerRef.current
    if (!el) return
    const onErr = () => setModelError(true)
    el.addEventListener('error', onErr)
    return () => el.removeEventListener('error', onErr)
  }, [src, modelError])

  if (!visible) return null

  return (
    <div className="fixed z-[60] bottom-4 right-4 md:bottom-6 md:right-6">
      {/* Tooltip */}
      <div
        className={`pointer-events-none absolute bottom-full right-0 mb-2 transition-all duration-150 ${
          hover ? 'translate-y-0 opacity-100' : 'translate-y-1 opacity-0'
        }`}
      >
        <div className="whitespace-nowrap rounded-sm border border-line bg-surface px-3 py-1.5 font-sans text-[13px] font-medium text-content shadow-md">
          Ask AI
        </div>
      </div>

      <button
        type="button"
        onClick={onClick}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        aria-label="Open AI Assistant"
        className={`group relative flex h-20 w-20 items-center justify-center bg-transparent transition-all duration-180 focus:outline-none focus-visible:ring-2 focus-visible:ring-ember focus-visible:ring-offset-2 focus-visible:ring-offset-canvas md:h-24 md:w-24 ${
          hover ? 'scale-110' : ''
        } ${reduceMotion ? '' : 'animate-[botFloat_3.2s_ease-in-out_infinite]'}`}
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
            shadow-intensity="0.8"
            exposure="1.1"
            camera-orbit="0deg 75deg 105%"
            camera-target="0m 0.05m 0m"
            field-of-view="30deg"
            auto-rotate={!reduceMotion || undefined}
            auto-rotate-delay={0}
            rotation-per-second={reduceMotion ? '0deg' : '18deg'}
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
          50% { transform: translateY(-4px); }
        }
        @media (prefers-reduced-motion: reduce) {
          .animate-\\[botFloat_3\\.2s_ease-in-out_infinite\\] {
            animation: none !important;
          }
        }
        model-viewer {
          --progress-bar-color: transparent;
          --progress-mask: transparent;
        }
      `}</style>
    </div>
  )
}
