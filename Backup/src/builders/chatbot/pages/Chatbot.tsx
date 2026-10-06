import { useEffect } from 'react'
import { useChatbot } from '../../../runtime/chatbot'
import { RobotButton, ChatPanel } from '../components'

export interface ChatbotProps {
  /** Optional URL for the 3D robot GLB */
  modelUrl?: string
  /** Case context for the query API */
  caseId?: string
  applicantId?: string
  partyId?: string
  stage?: string
  accessToken?: string
  /**
   * API base for the chat backend.
   * Default: `/api/v1/copilot` → POST `{base}/query`
   * In another project, set e.g. `https://api.example.com/v1/assistant`
   */
  apiBaseUrl?: string
  /** Path under baseUrl. Default: `/query` */
  apiQueryPath?: string
}

/**
 * Self-contained AI Chatbot.
 *
 * Porting to another project:
 * 1. Copy `builders/chatbot` + `runtime/chatbot`
 * 2. Change only `apiBaseUrl` / `apiQueryPath` (or edit `runtime/chatbot/api/client.ts`)
 * 3. Pass case context + bearer token as props
 */
export function Chatbot({
  modelUrl,
  caseId,
  applicantId,
  partyId,
  stage,
  accessToken,
  apiBaseUrl,
  apiQueryPath,
}: ChatbotProps = {}) {
  const api = useChatbot({
    caseId,
    applicantId,
    partyId,
    stage,
    accessToken,
    apiBaseUrl,
    apiQueryPath,
  })

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
      <RobotButton visible={api.mode === 'closed'} onClick={api.open} modelUrl={modelUrl} />
      <ChatPanel api={api} />
    </>
  )
}
