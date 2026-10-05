/**
 * Persist KYC wizard draft to localStorage so page reload keeps form details.
 * File blobs (uploads) cannot be stored — only text fields, IDs, and API JSON.
 */

import type { FosChecklistItem, FosResponse } from '../api'
import type { LosProcessResponse } from '../types'
import type {
  ActiveParty,
  ApplicationDetails,
  PartySelection,
  ProfileField,
  WizardStep,
} from '../types/wizard'
import { DEFAULT_APPLICATION, DEFAULT_PROFILE_FIELDS } from '../types/wizard'

const STORAGE_KEY = 'los.kyc.wizard.v1'
/** Pending resume after login — consumed once by the wizard */
const RESUME_KEY = 'los.kyc.resume.v1'

export interface PendingCaseResume {
  applicantId: string
  caseId: string
  savedAt: number
}

export function savePendingCaseResume(applicantId: string, caseId: string): void {
  if (!canUseStorage()) return
  try {
    const payload: PendingCaseResume = {
      applicantId: applicantId.trim(),
      caseId: caseId.trim(),
      savedAt: Date.now(),
    }
    window.localStorage.setItem(RESUME_KEY, JSON.stringify(payload))
  } catch {
    /* quota */
  }
}

export function consumePendingCaseResume(): PendingCaseResume | null {
  if (!canUseStorage()) return null
  try {
    const raw = window.localStorage.getItem(RESUME_KEY)
    window.localStorage.removeItem(RESUME_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<PendingCaseResume>
    const applicantId = String(parsed.applicantId || '').trim()
    const caseId = String(parsed.caseId || '').trim()
    if (!applicantId || !caseId) return null
    return { applicantId, caseId, savedAt: Number(parsed.savedAt) || Date.now() }
  } catch {
    return null
  }
}

export interface WizardDraft {
  step: WizardStep
  profileFields: ProfileField[]
  application: ApplicationDetails
  partySelection: PartySelection
  activeParty: ActiveParty
  applicantId: string
  coApplicantId: string
  caseId: string
  fosChecklist: FosChecklistItem[]
  requiredDocuments: string[]
  fosStage: string | null
  lastFosResponse: FosResponse | null
  /** PROCESS result when on report step (no File blobs) */
  result: LosProcessResponse | null
  savedAt: number
}

function canUseStorage(): boolean {
  try {
    if (typeof window === 'undefined' || !window.localStorage) return false
    const k = '__los_wiz_test__'
    window.localStorage.setItem(k, '1')
    window.localStorage.removeItem(k)
    return true
  } catch {
    return false
  }
}

function isWizardStep(v: unknown): v is WizardStep {
  return (
    v === 'details' ||
    v === 'application' ||
    v === 'party' ||
    v === 'documents' ||
    v === 'report'
  )
}

export function loadWizardDraft(): WizardDraft | null {
  if (!canUseStorage()) return null
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as Partial<WizardDraft>
    if (!parsed || typeof parsed !== 'object') return null

    const profileFields = Array.isArray(parsed.profileFields)
      ? (parsed.profileFields as ProfileField[])
      : DEFAULT_PROFILE_FIELDS.map((f) => ({ ...f }))

    const application =
      parsed.application && typeof parsed.application === 'object'
        ? { ...DEFAULT_APPLICATION, ...parsed.application }
        : { ...DEFAULT_APPLICATION }

    const partySelection =
      parsed.partySelection && typeof parsed.partySelection === 'object'
        ? {
            applicant: Boolean(
              (parsed.partySelection as PartySelection).applicant ?? true,
            ),
            coApplicant: Boolean(
              (parsed.partySelection as PartySelection).coApplicant ?? false,
            ),
          }
        : { applicant: true, coApplicant: false }

    const activeParty: ActiveParty =
      parsed.activeParty === 'CO_APPLICANT' ? 'CO_APPLICANT' : 'PRIMARY_APPLICANT'

    // Uploaded files are lost on reload — don't restore report without docs unless result exists
    let step: WizardStep = isWizardStep(parsed.step) ? parsed.step : 'details'
    if (step === 'report' && !parsed.result) step = 'documents'
    if (step === 'documents') {
      // stay on documents; user re-uploads if needed
    }

    return {
      step,
      profileFields,
      application,
      partySelection,
      activeParty,
      applicantId: typeof parsed.applicantId === 'string' ? parsed.applicantId : '',
      coApplicantId: typeof parsed.coApplicantId === 'string' ? parsed.coApplicantId : '',
      caseId: typeof parsed.caseId === 'string' ? parsed.caseId : '',
      fosChecklist: Array.isArray(parsed.fosChecklist) ? parsed.fosChecklist : [],
      requiredDocuments: Array.isArray(parsed.requiredDocuments)
        ? parsed.requiredDocuments
        : [],
      fosStage: typeof parsed.fosStage === 'string' ? parsed.fosStage : null,
      lastFosResponse:
        parsed.lastFosResponse && typeof parsed.lastFosResponse === 'object'
          ? (parsed.lastFosResponse as FosResponse)
          : null,
      result:
        parsed.result && typeof parsed.result === 'object'
          ? (parsed.result as LosProcessResponse)
          : null,
      savedAt: typeof parsed.savedAt === 'number' ? parsed.savedAt : Date.now(),
    }
  } catch {
    return null
  }
}

export function saveWizardDraft(draft: Omit<WizardDraft, 'savedAt'>): void {
  if (!canUseStorage()) return
  try {
    const payload: WizardDraft = { ...draft, savedAt: Date.now() }
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    /* quota / private mode */
  }
}

export function clearWizardDraft(): void {
  if (!canUseStorage()) return
  try {
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    /* ignore */
  }
}
