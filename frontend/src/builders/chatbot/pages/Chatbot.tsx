import { useEffect } from 'react'
import { useChatbot } from '../../../runtime/chatbot'
import { RobotButton, ChatPanel } from '../components'

interface ChatbotProps {
  /** Optional URL for the 3D robot GLB. Defaults to packaged mini_bot.glb */
  modelUrl?: string
}

/**
 * Drop-in AI Chatbot.
 * Place anywhere in the app tree — 3D robot floats bottom-right,
 * click opens the chat panel. Dummy backend included.
 *
 * Usage:
 *   import { Chatbot } from '@/builders/chatbot'
 *   <Chatbot />
 *   // or custom model path:
 *   <Chatbot modelUrl="/assets/mini_bot.glb" />
 */
export function Chatbot({ modelUrl }: ChatbotProps = {}) {
  const api = useChatbot()

  // Mobile: force full-sheet mode when open
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 767px)')
    const apply = () => {
      if (api.mode !== 'closed' && mq.matches) {
        api.setMode('mobile')
      } else if (api.mode === 'mobile' && !mq.matches) {
        api.setMode('panel')
      }
    }
    apply()
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [api.mode, api.setMode])

  return (
    <>
      <RobotButton
        visible={api.mode === 'closed'}
        onClick={api.open}
        modelUrl={modelUrl}
      />
      <ChatPanel api={api} />
    </>
  )
}
