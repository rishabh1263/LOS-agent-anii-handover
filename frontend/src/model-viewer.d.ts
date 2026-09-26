/**
 * Type declarations for @google/model-viewer custom element.
 * Works with React 18/19 JSX (React.JSX.IntrinsicElements).
 */
import type { DetailedHTMLProps, HTMLAttributes, CSSProperties } from 'react'

type ModelViewerProps = DetailedHTMLProps<HTMLAttributes<HTMLElement>, HTMLElement> & {
  src?: string
  alt?: string
  poster?: string
  loading?: 'auto' | 'lazy' | 'eager'
  reveal?: 'auto' | 'manual' | 'interaction'
  autoplay?: boolean | ''
  'animation-name'?: string
  'camera-controls'?: boolean | ''
  'camera-orbit'?: string
  'camera-target'?: string
  'field-of-view'?: string
  'shadow-intensity'?: string | number
  'shadow-softness'?: string | number
  exposure?: string | number
  'environment-image'?: string
  'skybox-image'?: string
  'auto-rotate'?: boolean | ''
  'auto-rotate-delay'?: number | string
  'rotation-per-second'?: string
  'interaction-prompt'?: 'auto' | 'when-focused' | 'none'
  'touch-action'?: string
  'disable-zoom'?: boolean | ''
  'disable-pan'?: boolean | ''
  'disable-tap'?: boolean | ''
  ar?: boolean | ''
  'ar-modes'?: string
  style?: CSSProperties
  class?: string
  className?: string
}

declare module 'react' {
  namespace JSX {
    interface IntrinsicElements {
      'model-viewer': ModelViewerProps
    }
  }
}

export { }
