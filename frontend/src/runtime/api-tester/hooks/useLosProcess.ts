import { useCallback, useMemo, useState } from 'react'
import { processDocuments, LosApiError } from '../api'
import type { LosProcessResponse, Operation, UploadFileItem, PartyRole } from '../types'
import { validateFiles } from '../utils'
import { useAuth } from '../../auth'

export function useLosProcess() {
  const { accessToken, logout } = useAuth()
  const [items, setItems] = useState<UploadFileItem[]>([])
  const [operation, setOperation] = useState<Operation>('PROCESS')
  const [applicantId, setApplicantId] = useState('')
  const [coApplicantId, setCoApplicantId] = useState('')
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

  const primaryItems = useMemo(
    () => items.filter((i) => i.partyRole === 'PRIMARY_APPLICANT'),
    [items],
  )
  const coItems = useMemo(
    () => items.filter((i) => i.partyRole === 'CO_APPLICANT'),
    [items],
  )

  const issues = useMemo(() => {
    if (items.length === 0) return []
    return validateFiles(items)
  }, [items])

  // At least one party must have docs; if co docs present, co_applicant_id required (Swagger)
  const canSubmit =
    (primaryItems.length > 0 || coItems.length > 0) &&
    issues.length === 0 &&
    !loading &&
    token.trim().length > 0 &&
    (coItems.length === 0 || coApplicantId.trim().length > 0)

  const setItemsForParty = useCallback(
    (partyRole: PartyRole, nextForParty: UploadFileItem[]) => {
      setItems((prev) => {
        const other = prev.filter((i) => i.partyRole !== partyRole)
        // Ensure partyRole is stamped on every item
        const stamped = nextForParty.map((i) => ({ ...i, partyRole }))
        return [...other, ...stamped]
      })
    },
    [],
  )

  const submit = useCallback(
    async (e?: React.FormEvent) => {
      e?.preventDefault()
      const currentIssues = validateFiles(items)
      if (currentIssues.length > 0) {
        setError(currentIssues[0].message)
        return
      }
      if (!token.trim()) {
        // Empty token — clear session so RequireAuth shows the login page
        await logout()
        return
      }
      setError(null)
      setLoading(true)
      setResult(null)

      try {
        const primary = items.filter((i) => i.partyRole === 'PRIMARY_APPLICANT')
        const co = items.filter((i) => i.partyRole === 'CO_APPLICANT')

        if (co.length > 0 && !coApplicantId.trim()) {
          setError('Co-applicant ID is required when uploading co-applicant documents.')
          setLoading(false)
          return
        }

        const res = await processDocuments({
          // Swagger: files = primary only
          files: primary.map((i) => i.file),
          expectedTypes: primary.map((i) => i.expectedType),
          // Swagger: co_applicant_files = co only (never mixed into files)
          coApplicantFiles: co.map((i) => i.file),
          coApplicantExpectedTypes: co.map((i) => i.expectedType),
          operation,
          applicantId: applicantId.trim() || undefined,
          coApplicantId: coApplicantId.trim() || undefined,
          caseId: caseId.trim() || undefined,
          token: token.trim(),
          baseUrl: baseUrl.trim() || undefined,
        })
        setResult(res)
        if (!caseId.trim() && res.case_id) setCaseId(res.case_id)
        if (!applicantId.trim() && res.applicant_id) setApplicantId(res.applicant_id)
        if (!coApplicantId.trim() && res.co_applicant_id) setCoApplicantId(res.co_applicant_id)
      } catch (err) {
        if (err instanceof LosApiError) {
          // 401 Unauthorized — token empty/expired/invalid → clear session → login
          if (err.status === 401) {
            await logout()
            return
          }
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
    [items, operation, applicantId, coApplicantId, caseId, token, baseUrl, logout],
  )

  const reset = useCallback(() => {
    setResult(null)
    setError(null)
    setItems([])
  }, [])

  /** Keep case IDs, token, and files — return to form to add more docs and re-run */
  const addMoreDocuments = useCallback(() => {
    setResult(null)
    setError(null)
  }, [])

  const clearFiles = useCallback(() => setItems([]), [])

  return {
    items,
    setItems,
    primaryItems,
    coItems,
    setItemsForParty,
    operation,
    setOperation,
    applicantId,
    setApplicantId,
    coApplicantId,
    setCoApplicantId,
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
    addMoreDocuments,
    clearFiles,
  }
}
