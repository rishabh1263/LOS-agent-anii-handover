import { motion, useReducedMotion } from 'motion/react'

/**
 * Soft typing indicator for streaming / thinking states.
 * Falls back to static dots when the user prefers reduced motion.
 */
export function TypingDots({ className = 'bg-ember' }: { className?: string }) {
  const reduceMotion = useReducedMotion()

  if (reduceMotion) {
    return (
      <span className="flex items-center gap-1.5 py-0.5" aria-label="Loading">
        <span className={`h-1.5 w-1.5 rounded-full opacity-70 ${className}`} />
        <span className={`h-1.5 w-1.5 rounded-full opacity-50 ${className}`} />
        <span className={`h-1.5 w-1.5 rounded-full opacity-30 ${className}`} />
      </span>
    )
  }

  return (
    <span className="flex items-center gap-1.5 py-0.5" aria-label="Loading">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className={`h-1.5 w-1.5 rounded-full ${className}`}
          animate={{ y: [0, -4, 0], opacity: [0.45, 1, 0.45] }}
          transition={{
            duration: 0.9,
            repeat: Infinity,
            ease: 'easeInOut',
            delay: i * 0.12,
          }}
        />
      ))}
    </span>
  )
}
