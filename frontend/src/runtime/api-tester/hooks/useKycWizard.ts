import { useCallback, useMemo, useRef, useState } from 'react'
import { processDocuments, LosApiError } from '../api'
import type {
  DocumentTypeHint,
  LosProcessResponse,
  PartyRole,
  UploadFileItem,
} from '../types'
import type {
  ActiveParty,
  PartySelection,
  ProfileField,
  VerifiedDoc,
  WizardStep,
} from '../types/wizard'
import {
  DEFAULT_PROFILE_FIELDS,
  buildTypeMismatchError,
  isDocTypeMatch,
} from '../types/wizard'
import { matchProfileToExtraction } from '../utils/profileMatch'
import { useAuth } from '../../auth'

function makeId() {
  return `f_${Math.random().toString(36).slice(2, 10)}`
}

function makePartyId(role: PartyRole) {
  const prefix = role === 'PRIMARY_APPLICANT' ? 'APP' : 'COAPP'
  return `${prefix}-${Date.now().toString(36).toUpperCase().slice(-6)}`
}

function errorMessage(err: unknown): string {
  if (err instanceof LosApiError) {
    return err.body.message || err.body.detail || err.body.error || `Error ${err.status}`
  }
  if (err instanceof Error) return err.message
  return 'Request failed'
}

/** Find the document result that best matches this upload (by type / latest). */
function findDocResult(
  res: LosProcessResponse,
  expectedType: DocumentTypeHint,
  partyRole: PartyRole,
) {
  const partyDocs = (res.documents ?? []).filter((d) => {
    const role = String(d.party_role || '').toUpperCase()
    if (!role) return true
    if (partyRole === 'PRIMARY_APPLICANT') {
      return role.includes('PRIMARY') || role.includes('APPLICANT')
    }
    return role.includes('CO')
  })

  const pool = partyDocs.length > 0 ? partyDocs : res.documents ?? []
  if (expectedType !== 'AUTO') {
    const byType = pool.find(
      (d) => isDocTypeMatch(expectedType, d.type) || isDocTypeMatch(expectedType, d.expected_type),
    )
    if (byType) return byType
  }
  return pool[pool.length - 1] ?? null
}

/**
 * Guided flow:
 * details → party → documents (per-upload verify, parallel) → Run verification → report
 */
export function useKycWizard() {
  const { accessToken, logout } = useAuth()
  const token = accessToken || ''

  const [step, setStep] = useState<WizardStep>('details')
  const [profileFields, setProfileFields] = useState<ProfileField[]>(() =>
    DEFAULT_PROFILE_FIELDS.map((f) => ({ ...f })),
  )
  const [partySelection, setPartySelection] = useState<PartySelection>({
    applicant: true,
    coApplicant: false,
  })
  const [activeParty, setActiveParty] = useState<ActiveParty>('PRIMARY_APPLICANT')
  const [applicantId, setApplicantId] = useState('')
  const [coApplicantId, setCoApplicantId] = useState('')
  const [caseId, setCaseId] = useState('')
  const [verifiedDocs, setVerifiedDocs] = useState<VerifiedDoc[]>([])
  const [inFlightCount, setInFlightCount] = useState(0)
  const [verifying, setVerifying] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<LosProcessResponse | null>(null)
  const [showOtherPartyPrompt, setShowOtherPartyPrompt] = useState(false)

  // Latest refs for concurrent uploads (avoid stale closures)
  const docsRef = useRef<VerifiedDoc[]>([])
  docsRef.current = verifiedDocs
  const profileRef = useRef(profileFields)
  profileRef.current = profileFields

  const primaryDocs = useMemo(
    () => verifiedDocs.filter((d) => d.item.partyRole === 'PRIMARY_APPLICANT'),
    [verifiedDocs],
  )
  const coDocs = useMemo(
    () => verifiedDocs.filter((d) => d.item.partyRole === 'CO_APPLICANT'),
    [verifiedDocs],
  )
  const successDocs = useMemo(
    () => verifiedDocs.filter((d) => d.status === 'success'),
    [verifiedDocs],
  )

  const updateProfileField = useCallback((key: string, value: string) => {
    setProfileFields((prev) => prev.map((f) => (f.key === key ? { ...f, value } : f)))
  }, [])

  const addCustomField = useCallback((label: string) => {
    const trimmed = label.trim()
    if (!trimmed) return
    const key = `custom_${trimmed.toLowerCase().replace(/\s+/g, '_')}_${Date.now().toString(36)}`
    setProfileFields((prev) => [...prev, { key, label: trimmed, value: '' }])
  }, [])

  const removeProfileField = useCallback((key: string) => {
    setProfileFields((prev) => prev.filter((f) => f.key !== key || f.builtin))
  }, [])

  const canProceedFromDetails = useMemo(() => {
    const name = profileFields.find((f) => f.key === 'name')?.value.trim()
    return Boolean(name)
  }, [profileFields])

  const canProceedFromParty = partySelection.applicant || partySelection.coApplicant

  const goToParty = useCallback(() => {
    if (!canProceedFromDetails) {
      setError('Enter full name to continue.')
      return
    }
    setError(null)
    if (!applicantId) setApplicantId(makePartyId('PRIMARY_APPLICANT'))
    if (!coApplicantId) setCoApplicantId(makePartyId('CO_APPLICANT'))
    setStep('party')
  }, [canProceedFromDetails, applicantId, coApplicantId])

  const goToDocuments = useCallback(() => {
    if (!canProceedFromParty) {
      setError('Select Applicant, Co-applicant, or both.')
      return
    }
    setError(null)
    setActiveParty(partySelection.applicant ? 'PRIMARY_APPLICANT' : 'CO_APPLICANT')
    setStep('documents')
  }, [canProceedFromParty, partySelection.applicant])

  // Stable ids for concurrent uploads without stale closures
  const idsRef = useRef({ applicantId, coApplicantId, caseId, token })
  idsRef.current = { applicantId, coApplicantId, caseId, token }

  const applyIdsFromResponse = useCallback((res: LosProcessResponse) => {
    if (res.case_id) setCaseId((prev) => prev || res.case_id || '')
    if (res.applicant_id) setApplicantId((prev) => prev || res.applicant_id || '')
    if (res.co_applicant_id) setCoApplicantId((prev) => prev || res.co_applicant_id || '')
  }, [])

  const singleFileParams = useCallback(
    (item: UploadFileItem, operation: 'VERIFY' | 'EXTRACT' | 'PROCESS') => {
      const { applicantId: aid, coApplicantId: cid, caseId: csid, token: tok } = idsRef.current
      const isCo = item.partyRole === 'CO_APPLICANT'
      return {
        files: isCo ? [] : [item.file],
        expectedTypes: isCo ? [] : [item.expectedType],
        coApplicantFiles: isCo ? [item.file] : [],
        coApplicantExpectedTypes: isCo ? [item.expectedType] : [],
        operation,
        applicantId: aid.trim() || undefined,
        coApplicantId: isCo ? cid.trim() || undefined : undefined,
        caseId: csid.trim() || undefined,
        token: tok.trim(),
      }
    },
    [],
  )

  // VERIFY → EXTRACT per file (2 network RTTs). Parallel uploads share no lock.
  const uploadAndVerify = useCallback(
    async (file: File, expectedType: DocumentTypeHint, partyRole: PartyRole) => {
      if (!idsRef.current.token.trim()) {
        await logout()
        return
      }
      if (partyRole === 'CO_APPLICANT' && !idsRef.current.coApplicantId.trim()) {
        setError('Co-applicant ID is required when co-applicant documents are uploaded.')
        return
      }

      const item: UploadFileItem = { id: makeId(), file, expectedType, partyRole }
      const patch = (id: string, next: Partial<VerifiedDoc>) =>
        setVerifiedDocs((prev) => {
          const i = prev.findIndex((d) => d.item.id === id)
          if (i < 0) return prev
          const copy = prev.slice()
          copy[i] = { ...copy[i], ...next }
          return copy
        })

      // One paint: start VERIFY (skip separate "uploading" tick)
      setVerifiedDocs((prev) => [...prev, { item, status: 'verifying', progress: 15 }])
      setInFlightCount((n) => n + 1)
      setError(null)

      try {
        const verifyRes = await processDocuments(singleFileParams(item, 'VERIFY'))
        applyIdsFromResponse(verifyRes)

        const verifyDoc = findDocResult(verifyRes, expectedType, partyRole)
        const detectedType = verifyDoc?.type ?? verifyDoc?.expected_type ?? null
        const verifyStatus = String(
          verifyDoc?.verification ?? verifyDoc?.status ?? verifyRes.status,
        ).toUpperCase()

        if (expectedType !== 'AUTO' && detectedType && !isDocTypeMatch(expectedType, detectedType)) {
          const msg = buildTypeMismatchError('VERIFY', expectedType, detectedType)
          patch(item.id, {
            status: 'type_mismatch',
            progress: 100,
            detectedType,
            response: verifyRes,
            error: msg,
          })
          setError(msg)
          return
        }

        if (verifyStatus === 'FAIL' || verifyStatus === 'FAILED' || verifyStatus === 'REJECTED') {
          const reason =
            verifyDoc?.reasons?.[0] ||
            verifyDoc?.reason_codes?.[0] ||
            verifyRes.summary ||
            'Document verification failed.'
          patch(item.id, {
            status: 'error',
            progress: 100,
            detectedType,
            response: verifyRes,
            error: reason,
          })
          setError(reason)
          return
        }

        // EXTRACT — second RTT; one paint at start of extract
        patch(item.id, { status: 'extracting', progress: 55 })
        const extractRes = await processDocuments(singleFileParams(item, 'EXTRACT'))
        applyIdsFromResponse(extractRes)

        const extractDoc = findDocResult(extractRes, expectedType, partyRole)
        const extractDetected = extractDoc?.type ?? extractDoc?.expected_type ?? detectedType

        if (
          expectedType !== 'AUTO' &&
          extractDetected &&
          !isDocTypeMatch(expectedType, extractDetected)
        ) {
          const msg = buildTypeMismatchError('EXTRACT', expectedType, extractDetected)
          patch(item.id, {
            status: 'type_mismatch',
            progress: 100,
            detectedType: extractDetected,
            extractResponse: extractRes,
            response: extractRes,
            error: msg,
          })
          setError(msg)
          return
        }

        // System profile vs this document EXTRACT — right after extraction
        const extraction =
          (extractDoc?.extraction as Record<string, unknown> | null | undefined) ?? null
        const profileMatches = matchProfileToExtraction(profileRef.current, extraction)

        patch(item.id, {
          status: 'success',
          progress: 100,
          detectedType: extractDetected,
          profileMatches,
          extractResponse: extractRes,
          response: extractRes,
        })

        const ok = (role: PartyRole) =>
          docsRef.current.some(
            (d) =>
              d.item.partyRole === role &&
              (d.status === 'success' || d.item.id === item.id),
          )
        if (
          partySelection.applicant &&
          partySelection.coApplicant &&
          ((ok('PRIMARY_APPLICANT') && !ok('CO_APPLICANT')) ||
            (!ok('PRIMARY_APPLICANT') && ok('CO_APPLICANT')))
        ) {
          setShowOtherPartyPrompt(true)
        }
      } catch (err) {
        if (err instanceof LosApiError && err.status === 401) {
          await logout()
          return
        }
        const message = errorMessage(err)
        setError(message)
        patch(item.id, { status: 'error', progress: 100, error: message })
      } finally {
        setInFlightCount((n) => Math.max(0, n - 1))
      }
    },
    [logout, singleFileParams, partySelection.applicant, partySelection.coApplicant, applyIdsFromResponse],
  )

  const removeDoc = useCallback((id: string) => {
    setVerifiedDocs((prev) => prev.filter((d) => d.item.id !== id))
  }, [])

  const switchToOtherParty = useCallback(() => {
    setShowOtherPartyPrompt(false)
    setActiveParty((prev) =>
      prev === 'PRIMARY_APPLICANT' ? 'CO_APPLICANT' : 'PRIMARY_APPLICANT',
    )
  }, [])

  const dismissOtherPartyPrompt = useCallback(() => {
    setShowOtherPartyPrompt(false)
  }, [])

  /**
   * Final verification: PROCESS all accepted docs, then open report.
   * Response drives profile match and cross-document checks.
   */
  const runVerification = useCallback(async () => {
    const docs = docsRef.current.filter((d) => d.status === 'success')
    if (docs.length === 0) {
      setError('Upload at least one accepted document before running verification.')
      return
    }
    if (!token.trim()) {
      await logout()
      return
    }

    setVerifying(true)
    setError(null)
    setShowOtherPartyPrompt(false)

    try {
      const primary = docs
        .filter((d) => d.item.partyRole === 'PRIMARY_APPLICANT')
        .map((d) => d.item)
      const co = docs
        .filter((d) => d.item.partyRole === 'CO_APPLICANT')
        .map((d) => d.item)

      if (co.length > 0 && !coApplicantId.trim()) {
        throw new Error('Co-applicant ID is required when co-applicant documents are present.')
      }

      const res = await processDocuments({
        files: primary.map((i) => i.file),
        expectedTypes: primary.map((i) => i.expectedType),
        coApplicantFiles: co.map((i) => i.file),
        coApplicantExpectedTypes: co.map((i) => i.expectedType),
        operation: 'PROCESS',
        applicantId: applicantId.trim() || undefined,
        coApplicantId: co.length > 0 ? coApplicantId.trim() || undefined : undefined,
        caseId: caseId.trim() || undefined,
        token: token.trim(),
      })

      setResult(res)
      applyIdsFromResponse(res)
      setStep('report')
    } catch (err) {
      if (err instanceof LosApiError && err.status === 401) {
        await logout()
        return
      }
      setError(errorMessage(err))
    } finally {
      setVerifying(false)
    }
  }, [token, logout, applicantId, coApplicantId, caseId, applyIdsFromResponse])

  const reset = useCallback(() => {
    setStep('details')
    setProfileFields(DEFAULT_PROFILE_FIELDS.map((f) => ({ ...f })))
    setPartySelection({ applicant: true, coApplicant: false })
    setActiveParty('PRIMARY_APPLICANT')
    setApplicantId('')
    setCoApplicantId('')
    setCaseId('')
    setVerifiedDocs([])
    setInFlightCount(0)
    setVerifying(false)
    setError(null)
    setResult(null)
    setShowOtherPartyPrompt(false)
  }, [])

  const addMoreDocuments = useCallback(() => {
    setStep('documents')
    setError(null)
  }, [])

  const profileSnapshot = useMemo(() => {
    const map: Record<string, string> = {}
    for (const f of profileFields) {
      if (f.value.trim()) map[f.key] = f.value.trim()
    }
    return map
  }, [profileFields])

  return {
    step,
    setStep,
    profileFields,
    updateProfileField,
    addCustomField,
    removeProfileField,
    canProceedFromDetails,
    partySelection,
    setPartySelection,
    canProceedFromParty,
    activeParty,
    setActiveParty,
    applicantId,
    setApplicantId,
    coApplicantId,
    setCoApplicantId,
    caseId,
    verifiedDocs,
    primaryDocs,
    coDocs,
    successDocs,
    /** True while any single-doc upload is in flight (UI stays interactive) */
    loading: inFlightCount > 0,
    inFlightCount,
    verifying,
    error,
    setError,
    result,
    showOtherPartyPrompt,
    profileSnapshot,
    goToParty,
    goToDocuments,
    uploadAndVerify,
    removeDoc,
    switchToOtherParty,
    dismissOtherPartyPrompt,
    runVerification,
    reset,
    addMoreDocuments,
  }
}
