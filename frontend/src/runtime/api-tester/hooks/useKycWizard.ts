import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  processDocuments,
  LosApiError,
  createApplicant,
  getApplicant,
  getChecklist,
  extractIds,
  FosApiError,
} from '../api'
import type { FosChecklistItem, FosResponse } from '../api'
import type {
  DocumentTypeHint,
  LosProcessResponse,
  PartyRole,
  UploadFileItem,
} from '../types'
import type {
  ActiveParty,
  ApplicationDetails,
  PartySelection,
  ProfileField,
  VerifiedDoc,
  WizardStep,
} from '../types/wizard'
import {
  DEFAULT_APPLICATION,
  DEFAULT_PROFILE_FIELDS,
  isDocTypeMatch,
} from '../types/wizard'
import {
  isRejectDecision,
  isReviewDecision,
  normalizeDecision,
  validateProfileFields,
} from '../utils/validation'
import {
  clearWizardDraft,
  consumePendingCaseResume,
  loadWizardDraft,
  saveWizardDraft,
} from '../utils/wizardStorage'
import { useAuth } from '../../auth'

function makeId() {
  return `f_${Math.random().toString(36).slice(2, 10)}`
}

function makePartyId(role: PartyRole) {
  const prefix = role === 'PRIMARY_APPLICANT' ? 'APP' : 'COAPP'
  return `${prefix}-${Date.now().toString(36).toUpperCase().slice(-6)}`
}

function errorMessage(err: unknown): string {
  if (err instanceof LosApiError || err instanceof FosApiError) {
    return err.body.message || err.body.detail || err.body.error || `Error ${err.status}`
  }
  if (err instanceof Error) return err.message
  return 'Request failed'
}

function fieldValue(fields: ProfileField[], key: string): string {
  return fields.find((f) => f.key === key)?.value.trim() || ''
}

function toNumber(value: string, fallback = 0): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
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

  const draft = useMemo(() => loadWizardDraft(), [])

  const [step, setStep] = useState<WizardStep>(() => draft?.step ?? 'details')
  const [profileFields, setProfileFields] = useState<ProfileField[]>(() =>
    draft?.profileFields?.length
      ? draft.profileFields
      : DEFAULT_PROFILE_FIELDS.map((f) => ({ ...f })),
  )
  const [application, setApplication] = useState<ApplicationDetails>(() => ({
    ...(draft?.application ?? DEFAULT_APPLICATION),
  }))
  const [partySelection, setPartySelection] = useState<PartySelection>(
    () => draft?.partySelection ?? { applicant: true, coApplicant: false },
  )
  const [activeParty, setActiveParty] = useState<ActiveParty>(
    () => draft?.activeParty ?? 'PRIMARY_APPLICANT',
  )
  const [applicantId, setApplicantId] = useState(() => draft?.applicantId ?? '')
  const [coApplicantId, setCoApplicantId] = useState(() => draft?.coApplicantId ?? '')
  const [caseId, setCaseId] = useState(() => draft?.caseId ?? '')
  /** Document checklist from FOS (POST create or GET checklist) */
  const [fosChecklist, setFosChecklist] = useState<FosChecklistItem[]>(
    () => draft?.fosChecklist ?? [],
  )
  const [requiredDocuments, setRequiredDocuments] = useState<string[]>(
    () => draft?.requiredDocuments ?? [],
  )
  const [fosStage, setFosStage] = useState<string | null>(() => draft?.fosStage ?? null)
  const [lastFosResponse, setLastFosResponse] = useState<FosResponse | null>(
    () => draft?.lastFosResponse ?? null,
  )
  const [verifiedDocs, setVerifiedDocs] = useState<VerifiedDoc[]>([])
  const [inFlightCount, setInFlightCount] = useState(0)
  const [verifying, setVerifying] = useState(false)
  const [submittingApplicant, setSubmittingApplicant] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<LosProcessResponse | null>(
    () => draft?.result ?? null,
  )
  const [showOtherPartyPrompt, setShowOtherPartyPrompt] = useState(false)
  const [resumingCase, setResumingCase] = useState(false)
  const resumeAttempted = useRef(false)

  /** Persist form + IDs across page reload (files cannot be restored). */
  useEffect(() => {
    saveWizardDraft({
      step,
      profileFields,
      application,
      partySelection,
      activeParty,
      applicantId,
      coApplicantId,
      caseId,
      fosChecklist,
      requiredDocuments,
      fosStage,
      lastFosResponse,
      result,
    })
  }, [
    step,
    profileFields,
    application,
    partySelection,
    activeParty,
    applicantId,
    coApplicantId,
    caseId,
    fosChecklist,
    requiredDocuments,
    fosStage,
    lastFosResponse,
    result,
  ])

  // Keep latest docs for concurrent uploads without stale closures
  const docsRef = useRef<VerifiedDoc[]>([])
  docsRef.current = verifiedDocs

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

  const profileIssues = useMemo(
    () => validateProfileFields(profileFields),
    [profileFields],
  )

  const canProceedFromDetails = profileIssues.length === 0

  const canProceedFromApplication = useMemo(() => {
    return (
      Boolean(application.product.trim()) &&
      Boolean(application.employment_type.trim()) &&
      toNumber(application.loan_amount) > 0 &&
      toNumber(application.tenure_months) > 0 &&
      toNumber(application.interest_rate_pct) > 0
    )
  }, [application])

  const canProceedFromParty = partySelection.applicant || partySelection.coApplicant

  const updateApplicationField = useCallback((key: keyof ApplicationDetails, value: string) => {
    setApplication((prev) => ({ ...prev, [key]: value }))
  }, [])

  const goToApplication = useCallback(() => {
    const issues = validateProfileFields(profileFields)
    if (issues.length > 0) {
      setError(issues[0].message)
      return
    }
    setError(null)
    setStep('application')
  }, [profileFields])

  /**
   * Create FOS applicant (POST), then fetch record (GET) to lock applicant_id / case_id.
   */
  const submitApplicantAndContinue = useCallback(async () => {
    if (!canProceedFromApplication) {
      setError('Fill product, loan amount, employment type, tenure, and interest rate.')
      return
    }
    if (!token.trim()) {
      await logout()
      return
    }

    setSubmittingApplicant(true)
    setError(null)

    try {
      const applicantPayload: Record<string, string> = {
        full_name: fieldValue(profileFields, 'full_name'),
        mobile: fieldValue(profileFields, 'mobile'),
        email: fieldValue(profileFields, 'email'),
        date_of_birth: fieldValue(profileFields, 'date_of_birth'),
        address: fieldValue(profileFields, 'address'),
      }
      // Include extra profile fields (e.g. pan, custom)
      for (const f of profileFields) {
        if (applicantPayload[f.key] !== undefined) continue
        if (f.value.trim()) applicantPayload[f.key] = f.value.trim()
      }

      const created = await createApplicant(
        {
          applicant: applicantPayload,
          application: {
            product: application.product.trim(),
            loan_amount: toNumber(application.loan_amount),
            employment_type: application.employment_type.trim(),
            tenure_months: toNumber(application.tenure_months),
            interest_rate_pct: toNumber(application.interest_rate_pct),
            declared_monthly_obligations: toNumber(application.declared_monthly_obligations),
            property_value: toNumber(application.property_value),
          },
        },
        token,
      )

      setLastFosResponse(created)
      let { applicantId: applicantIdFromApi, caseId: caseIdFromApi } = extractIds(created)
      if (!applicantIdFromApi) applicantIdFromApi = applicantId
      if (!caseIdFromApi) caseIdFromApi = caseId

      if (created.checklist?.length) setFosChecklist(created.checklist)
      if (created.required_documents?.length) {
        setRequiredDocuments(created.required_documents)
      }
      if (created.stage) setFosStage(String(created.stage))

      if (applicantIdFromApi) {
        try {
          const record = await getApplicant(applicantIdFromApi, token, {
            case_id: caseIdFromApi || undefined,
          })
          setLastFosResponse(record)
          const ids = extractIds(record)
          if (ids.applicantId) applicantIdFromApi = ids.applicantId
          if (ids.caseId) caseIdFromApi = ids.caseId
        } catch {
          // POST succeeded; GET is best-effort
        }
      }

      // Refresh checklist from dedicated endpoint when we have a case_id
      if (caseIdFromApi) {
        try {
          const checklistRes = await getChecklist(caseIdFromApi, token, {
            applicant_id: applicantIdFromApi || undefined,
          })
          setLastFosResponse(checklistRes)
          if (checklistRes.checklist?.length) setFosChecklist(checklistRes.checklist)
          if (checklistRes.required_documents?.length) {
            setRequiredDocuments(checklistRes.required_documents)
          }
          if (checklistRes.stage) setFosStage(String(checklistRes.stage))
        } catch {
          // Checklist is optional enrichment
        }
      }

      if (!applicantIdFromApi) {
        applicantIdFromApi = makePartyId('PRIMARY_APPLICANT')
      }
      if (!coApplicantId) setCoApplicantId(makePartyId('CO_APPLICANT'))

      setApplicantId(applicantIdFromApi)
      if (caseIdFromApi) setCaseId(caseIdFromApi)
      setStep('party')
    } catch (err) {
      if (err instanceof FosApiError && err.status === 401) {
        await logout()
        return
      }
      setError(errorMessage(err))
    } finally {
      setSubmittingApplicant(false)
    }
  }, [
    canProceedFromApplication,
    token,
    logout,
    profileFields,
    application,
    applicantId,
    caseId,
    coApplicantId,
  ])

  const goToDocuments = useCallback(() => {
    if (!canProceedFromParty) {
      setError('Select Applicant, Co-applicant, or both.')
      return
    }
    setError(null)
    setActiveParty(partySelection.applicant ? 'PRIMARY_APPLICANT' : 'CO_APPLICANT')
    setStep('documents')
  }, [canProceedFromParty, partySelection.applicant])

  /**
   * Load an existing FOS applicant + case (GET applicant, GET checklist),
   * hydrate profile/application when the API returns them, jump to party step.
   */
  const resumeExistingCase = useCallback(
    async (applicantIdInput: string, caseIdInput: string) => {
      const appId = applicantIdInput.trim()
      const cId = caseIdInput.trim()
      if (!appId || !cId) {
        setError('Applicant ID and Case ID are both required.')
        return false
      }
      if (!token) {
        setError('Sign in first, then resume the case.')
        return false
      }

      setResumingCase(true)
      setError(null)

      try {
        const record = await getApplicant(appId, token, { case_id: cId })
        setLastFosResponse(record)
        const ids = extractIds(record)
        const resolvedApplicant = ids.applicantId || appId
        const resolvedCase = ids.caseId || cId

        setApplicantId(resolvedApplicant)
        setCaseId(resolvedCase)
        if (!coApplicantId) setCoApplicantId(makePartyId('CO_APPLICANT'))

        // Hydrate profile fields from API applicant object
        const ap = record.applicant
        if (ap && typeof ap === 'object') {
          setProfileFields((prev) =>
            prev.map((f) => {
              const raw = ap[f.key]
              if (raw == null || String(raw).trim() === '') return f
              return { ...f, value: String(raw) }
            }),
          )
        }

        // Hydrate application fields when present
        const appl = record.application
        if (appl && typeof appl === 'object') {
          setApplication((prev) => ({
            ...prev,
            product:
              appl.product != null && String(appl.product).trim()
                ? String(appl.product)
                : prev.product,
            loan_amount:
              appl.loan_amount != null && String(appl.loan_amount).trim() !== ''
                ? String(appl.loan_amount)
                : prev.loan_amount,
            employment_type:
              appl.employment_type != null && String(appl.employment_type).trim()
                ? String(appl.employment_type)
                : prev.employment_type,
            tenure_months:
              appl.tenure_months != null && String(appl.tenure_months).trim() !== ''
                ? String(appl.tenure_months)
                : prev.tenure_months,
            interest_rate_pct:
              appl.interest_rate_pct != null && String(appl.interest_rate_pct).trim() !== ''
                ? String(appl.interest_rate_pct)
                : prev.interest_rate_pct,
            declared_monthly_obligations:
              appl.declared_monthly_obligations != null &&
              String(appl.declared_monthly_obligations).trim() !== ''
                ? String(appl.declared_monthly_obligations)
                : prev.declared_monthly_obligations,
            property_value:
              appl.property_value != null && String(appl.property_value).trim() !== ''
                ? String(appl.property_value)
                : prev.property_value,
          }))
        }

        if (record.checklist?.length) setFosChecklist(record.checklist)
        if (record.required_documents?.length) {
          setRequiredDocuments(record.required_documents)
        }
        if (record.stage) setFosStage(String(record.stage))

        try {
          const checklistRes = await getChecklist(resolvedCase, token, {
            applicant_id: resolvedApplicant,
          })
          setLastFosResponse(checklistRes)
          if (checklistRes.checklist?.length) setFosChecklist(checklistRes.checklist)
          if (checklistRes.required_documents?.length) {
            setRequiredDocuments(checklistRes.required_documents)
          }
          if (checklistRes.stage) setFosStage(String(checklistRes.stage))
          const cIds = extractIds(checklistRes)
          if (cIds.applicantId) setApplicantId(cIds.applicantId)
          if (cIds.caseId) setCaseId(cIds.caseId)
        } catch {
          // Checklist enrichment is optional
        }

        setPartySelection({ applicant: true, coApplicant: false })
        setActiveParty('PRIMARY_APPLICANT')
        setStep('party')
        return true
      } catch (err) {
        if (err instanceof FosApiError && err.status === 401) {
          await logout()
          return false
        }
        if (err instanceof FosApiError && (err.status === 404 || err.status === 400)) {
          setError(
            'Case or applicant not found. Check the Applicant ID and Case ID, then try again.',
          )
        } else {
          setError(errorMessage(err))
        }
        return false
      } finally {
        setResumingCase(false)
      }
    },
    [token, logout, coApplicantId],
  )

  // After login with "Resume existing case", consume pending IDs once
  useEffect(() => {
    if (!token || resumeAttempted.current) return
    const pending = consumePendingCaseResume()
    if (!pending) return
    resumeAttempted.current = true
    void resumeExistingCase(pending.applicantId, pending.caseId)
  }, [token, resumeExistingCase])

  const applyIdsFromResponse = useCallback((res: LosProcessResponse) => {
    setCaseId((prev) => prev || res.case_id || '')
    setApplicantId((prev) => prev || res.applicant_id || '')
    setCoApplicantId((prev) => prev || res.co_applicant_id || '')
  }, [])

  /** Build LOS params for a single file under the correct party field. */
  const singleFileParams = useCallback(
    (
      item: UploadFileItem,
      operation: 'VERIFY' | 'EXTRACT' | 'PROCESS',
    ) => {
      const isCo = item.partyRole === 'CO_APPLICANT'
      return {
        files: isCo ? [] : [item.file],
        expectedTypes: isCo ? [] : [item.expectedType],
        coApplicantFiles: isCo ? [item.file] : [],
        coApplicantExpectedTypes: isCo ? [item.expectedType] : [],
        operation,
        applicantId: applicantId.trim() || undefined,
        coApplicantId: isCo ? coApplicantId.trim() || undefined : undefined,
        caseId: caseId.trim() || undefined,
        token: token.trim(),
      }
    },
    [applicantId, coApplicantId, caseId, token],
  )

  /**
   * On upload:
   * 1) VERIFY — document authenticity
   * 2) EXTRACT — only when VERIFY is not FAIL / REJECTED / REVIEW
   *    (decision === "REVIEW" skips EXTRACT)
   * Parallel uploads allowed. Final report uses PROCESS (see runVerification).
   */
  const uploadAndVerify = useCallback(
    async (file: File, expectedType: DocumentTypeHint, partyRole: PartyRole) => {
      if (!token.trim()) {
        await logout()
        return
      }

      if (partyRole === 'CO_APPLICANT' && !coApplicantId.trim()) {
        setError('Co-applicant ID is required when co-applicant documents are uploaded.')
        return
      }

      const item: UploadFileItem = {
        id: makeId(),
        file,
        expectedType,
        partyRole,
      }

      const patchDoc = (
        id: string,
        patch: Partial<(typeof verifiedDocs)[number]>,
      ) => {
        setVerifiedDocs((prev) => prev.map((d) => (d.item.id === id ? { ...d, ...patch } : d)))
      }

      setVerifiedDocs((prev) => [
        ...prev,
        { item, status: 'verifying', progress: 15 },
      ])
      setInFlightCount((n) => n + 1)
      setError(null)

      try {
        // --- Step 1: VERIFY ---
        patchDoc(item.id, { status: 'verifying', progress: 35 })
        const verifyRes = await processDocuments(singleFileParams(item, 'VERIFY'))
        applyIdsFromResponse(verifyRes)

        const verifyDoc = findDocResult(verifyRes, expectedType, partyRole)
        const detectedType = verifyDoc?.type ?? verifyDoc?.expected_type ?? null
        const verifyCode = normalizeDecision(
          verifyDoc?.specialist?.decision,
          verifyDoc?.verification,
          verifyDoc?.status,
          verifyRes.decision,
          verifyRes.status,
        )
        const reasonOf = (doc: typeof verifyDoc, res: typeof verifyRes, fallback: string) =>
          doc?.reasons?.[0] || doc?.reason_codes?.[0] || res.summary || fallback

        if (expectedType !== 'AUTO' && detectedType && !isDocTypeMatch(expectedType, detectedType)) {
          patchDoc(item.id, {
            status: 'type_mismatch',
            progress: 100,
            detectedType,
            verifyResponse: verifyRes,
            response: verifyRes,
            error: `Type mismatch: selected ${expectedType}, detected ${detectedType}. File not accepted.`,
          })
          setError(`Type mismatch: selected ${expectedType}, detected ${detectedType}.`)
          return
        }

        if (isRejectDecision(verifyCode)) {
          const reason = reasonOf(verifyDoc, verifyRes, 'Document verification failed.')
          patchDoc(item.id, {
            status: 'error',
            progress: 100,
            detectedType,
            verifyResponse: verifyRes,
            response: verifyRes,
            error: reason,
          })
          setError(reason)
          return
        }

        if (isReviewDecision(verifyCode)) {
          const reviewMsg = reasonOf(
            verifyDoc,
            verifyRes,
            'Document marked for review — extraction skipped.',
          )
          patchDoc(item.id, {
            status: 'review',
            progress: 100,
            detectedType,
            verifyResponse: verifyRes,
            response: verifyRes,
            error: reviewMsg,
          })
          return
        }

        // --- Step 2: EXTRACT ---
        patchDoc(item.id, { status: 'extracting', progress: 65 })
        const extractRes = await processDocuments(singleFileParams(item, 'EXTRACT'))
        applyIdsFromResponse(extractRes)

        const extractDoc = findDocResult(extractRes, expectedType, partyRole)
        const extractDetected = extractDoc?.type ?? extractDoc?.expected_type ?? detectedType
        const extractCode = normalizeDecision(
          extractDoc?.specialist?.decision,
          extractDoc?.verification,
          extractDoc?.status,
          extractRes.decision,
          extractRes.status,
        )

        if (isRejectDecision(extractCode)) {
          const reason = reasonOf(extractDoc, extractRes, 'Extraction rejected this document.')
          patchDoc(item.id, {
            status: 'error',
            progress: 100,
            detectedType: extractDetected,
            verifyResponse: verifyRes,
            extractResponse: extractRes,
            response: extractRes,
            error: reason,
          })
          setError(reason)
          return
        }

        if (isReviewDecision(extractCode)) {
          const reviewMsg = reasonOf(
            extractDoc,
            extractRes,
            'Extraction marked for review — cannot run full process yet.',
          )
          patchDoc(item.id, {
            status: 'review',
            progress: 100,
            detectedType: extractDetected,
            verifyResponse: verifyRes,
            extractResponse: extractRes,
            response: extractRes,
            error: reviewMsg,
          })
          setError(reviewMsg)
          return
        }

        patchDoc(item.id, {
          status: 'success',
          progress: 100,
          detectedType: extractDetected,
          verifyResponse: verifyRes,
          extractResponse: extractRes,
          response: extractRes,
          error: undefined,
        })

        const hasPrimary = docsRef.current.some(
          (d) =>
            d.item.partyRole === 'PRIMARY_APPLICANT' &&
            (d.status === 'success' || d.item.id === item.id),
        )
        const hasCo = docsRef.current.some(
          (d) =>
            d.item.partyRole === 'CO_APPLICANT' &&
            (d.status === 'success' || d.item.id === item.id),
        )
        if (
          partySelection.applicant &&
          partySelection.coApplicant &&
          ((hasPrimary && !hasCo) || (!hasPrimary && hasCo))
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
        setVerifiedDocs((prev) =>
          prev.map((d) =>
            d.item.id === item.id ? { ...d, status: 'error' as const, error: message } : d,
          ),
        )
      } finally {
        setInFlightCount((n) => Math.max(0, n - 1))
      }
    },
    [
      token,
      logout,
      coApplicantId,
      singleFileParams,
      partySelection.applicant,
      partySelection.coApplicant,
      applyIdsFromResponse,
    ],
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
    if (inFlightCount > 0) {
      setError('Wait until all documents finish VERIFY / EXTRACT before running verification.')
      return
    }
    const blocked = docsRef.current.filter(
      (d) => d.status === 'review' || d.status === 'error' || d.status === 'type_mismatch',
    )
    const docs = docsRef.current.filter((d) => d.status === 'success')
    if (docs.length === 0) {
      setError(
        blocked.length > 0
          ? 'No accepted documents. REVIEW / REJECT docs cannot be processed until resolved.'
          : 'Upload at least one accepted document before running verification.',
      )
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
  }, [token, logout, applicantId, coApplicantId, caseId, applyIdsFromResponse, inFlightCount])

  const reset = useCallback(() => {
    clearWizardDraft()
    setStep('details')
    setProfileFields(DEFAULT_PROFILE_FIELDS.map((f) => ({ ...f })))
    setApplication({ ...DEFAULT_APPLICATION })
    setPartySelection({ applicant: true, coApplicant: false })
    setActiveParty('PRIMARY_APPLICANT')
    setApplicantId('')
    setCoApplicantId('')
    setCaseId('')
    setFosChecklist([])
    setRequiredDocuments([])
    setFosStage(null)
    setLastFosResponse(null)
    setVerifiedDocs([])
    setInFlightCount(0)
    setVerifying(false)
    setSubmittingApplicant(false)
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
    application,
    updateApplicationField,
    canProceedFromApplication,
    submittingApplicant,
    submitApplicantAndContinue,
    goToApplication,
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
    fosChecklist,
    requiredDocuments,
    fosStage,
    lastFosResponse,
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
    goToDocuments,
    uploadAndVerify,
    removeDoc,
    switchToOtherParty,
    dismissOtherPartyPrompt,
    runVerification,
    reset,
    addMoreDocuments,
    resumeExistingCase,
    resumingCase,
  }
}
