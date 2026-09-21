import { useCallback, useMemo, useState } from 'react'
import { processDocuments, LosApiError } from '../api'
import type { LosProcessResponse, Operation, UploadFileItem } from '../types'
import { validateFiles } from '../utils'
import { useAuth } from '../../auth'

export function useLosProcess() {
  const { accessToken } = useAuth()
  const [items, setItems] = useState<UploadFileItem[]>([])
  const [operation, setOperation] = useState<Operation>('PROCESS')
  const [applicantId, setApplicantId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [customToken, setCustomToken] = useState<string | null>(null)
  const token = customToken !== null ? customToken : (accessToken || '')
  const setToken = useCallback((val: string) => {
    setCustomToken(val)
  }, [])
  const [baseUrl, setBaseUrl] = useState('/api/v1/los')
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<LosProcessResponse | null>(null)

  const issues = useMemo(() => {
    if (items.length === 0) return []
    return validateFiles(items)
  }, [items])

  const canSubmit =
    items.length > 0 && issues.length === 0 && !loading && token.trim().length > 0

  const submit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault()
      const currentIssues = validateFiles(items)
      if (currentIssues.length > 0) {
        setError(currentIssues[0].message)
        return
      }
      if (!token.trim()) {
        setError('Authentication required. Please sign in again.')
        return
      }
      setError(null)
      setLoading(true)
      setResult(null)

      try {
        const res = await processDocuments({
          files: items.map((i) => i.file),
          expectedTypes: items.map((i) => i.expectedType),
          operation,
          applicantId: applicantId.trim() || undefined,
          caseId: caseId.trim() || undefined,
          token: token.trim(),
          baseUrl: baseUrl.trim() || undefined,
        })
        setResult(res)
        if (!caseId.trim() && res.case_id) setCaseId(res.case_id)
      } catch (err) {
        if (err instanceof LosApiError) {
          setError(
            err.body.message || err.body.detail || err.body.error || `Error ${err.status}`,
          )
        } else if (err instanceof Error) {
          setError(err.message)
        } else {
          setError('Unexpected error')
        }
      } finally {
        setLoading(false)
      }
    },
    [items, operation, applicantId, caseId, token, baseUrl],
  )

  const reset = useCallback(() => {
    setResult(null)
    setError(null)
    setItems([])
  }, [])

  const clearFiles = useCallback(() => setItems([]), [])

  return {
    items,
    setItems,
    operation,
    setOperation,
    applicantId,
    setApplicantId,
    caseId,
    setCaseId,
    token,
    setToken,
    baseUrl,
    setBaseUrl,
    showAdvanced,
    setShowAdvanced,
    loading,
    error,
    result,
    issues,
    canSubmit,
    submit,
    reset,
    clearFiles,
  }
}
