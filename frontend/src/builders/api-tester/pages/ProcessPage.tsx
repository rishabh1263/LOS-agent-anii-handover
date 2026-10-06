import { useEffect } from 'react'
import { ArrowLeft } from 'lucide-react'
import { AnimatePresence, motion } from 'motion/react'
import { useKycWizard } from '../../../runtime/api-tester'
import type { CaseDataResponse } from '../../../runtime/api-tester'
import { useAuth } from '../../../runtime/auth'
import { Chatbot } from '../../chatbot'
import {
  ApplicationDetailsStep,
  BasicDetailsStep,
  DocumentUploadStep,
  PartySelectStep,
  ProfileMatchPanel,
  ResultsPanel,
  WizardProgress,
} from '../components'

const FADE = { duration: 0.22, ease: [0.22, 1, 0.36, 1] as const }

interface ProcessPageProps {
  /** Pre-loaded case data from CaseSelectPage (null = new case). */
  loadedCaseData?: CaseDataResponse | null
  /** Navigate back to CaseSelectPage to pick a different case. */
  onBackToCaseSelect?: () => void
}

/**
 * KYC wizard: details → application → party → documents → report.
 * Auth is handled by App (RequireAuth). Chatbot receives case context.
 * loadedCaseData is optional — null means a brand-new case is being created.
 */
export function ProcessPage({ loadedCaseData, onBackToCaseSelect }: ProcessPageProps = {}) {
  const { user, accessToken } = useAuth()
  const {
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
    coApplicantId,
    caseId,
    verifiedDocs,
    primaryDocs,
    coDocs,
    successDocs,
    loading,
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
    hydrateCaseData,
  } = useKycWizard()

  // When a case is pre-loaded from CaseSelectPage, seed all wizard state
  // from it so the user immediately sees their existing data.
  useEffect(() => {
    if (!loadedCaseData) return
    hydrateCaseData(loadedCaseData)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedCaseData])

  const partyId =
    activeParty === 'CO_APPLICANT' ? coApplicantId || undefined : applicantId || undefined

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6">
      <div className="card space-y-4">
        <div>
          <p className="font-display text-[12px] font-semibold uppercase tracking-[0.09em] text-content-secondary">
            Document verification
            {user?.stage ? ` · ${user.stage}` : ''}
          </p>
          <h1 className="mt-2 font-display text-[24px] font-bold tracking-tight text-content sm:text-[28px]">
            KYC verification
          </h1>
          <p className="mt-1.5 max-w-2xl text-[14px] leading-relaxed text-content-secondary">
            Enter applicant and application details, create the FOS record, select party, then
            upload documents. Each upload runs VERIFY then EXTRACT. Run verification executes
            PROCESS for the full report.
          </p>
          {/* Show IDs from wizard state or from the pre-loaded case data */}
          {(applicantId || caseId || loadedCaseData) && (
            <p className="mt-2 font-mono text-[12px] text-content-secondary">
              {(applicantId || loadedCaseData?.app_id) && (
                <span>Applicant: {applicantId || loadedCaseData?.app_id}</span>
              )}
              {(applicantId || loadedCaseData?.app_id) && (caseId || loadedCaseData?.case_id) && (
                <span className="mx-2 text-content-disabled">·</span>
              )}
              {(caseId || loadedCaseData?.case_id) && (
                <span>Case: {caseId || loadedCaseData?.case_id}</span>
              )}
            </p>
          )}
          {resumingCase && (
            <p className="mt-2 text-[13px] font-medium text-ember-text">
              Loading existing case…
            </p>
          )}
          {onBackToCaseSelect && (
            <button
              type="button"
              onClick={onBackToCaseSelect}
              className="mt-2 inline-flex items-center gap-1 text-[12px] text-content-secondary underline-offset-2 hover:text-content hover:underline focus:outline-none"
            >
              <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
              Change case
            </button>
          )}
        </div>
        <WizardProgress current={step === 'report' ? 'report' : step} />
      </div>

      {/* loadedCaseData banner — shown when a case was pre-fetched at CaseSelectPage */}
      {loadedCaseData && step === 'details' && (
        <div className="card border border-ember/20 bg-ember/[0.03] p-4 text-[13px] text-content-secondary">
          <p className="font-medium text-content">Case loaded</p>
          <p className="mt-0.5">
            Case <span className="font-mono">{loadedCaseData.case_id}</span> · APP{' '}
            <span className="font-mono">{loadedCaseData.app_id}</span> — fill in the details below
            to continue verification.
          </p>
        </div>
      )}

      <AnimatePresence mode="wait">
        {step === 'details' && (
          <motion.div
            key="details"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <BasicDetailsStep
              fields={profileFields}
              onChange={updateProfileField}
              onAddField={addCustomField}
              onRemoveField={removeProfileField}
              onContinue={goToApplication}
              canContinue={canProceedFromDetails}
              error={error}
            />
          </motion.div>
        )}

        {step === 'application' && (
          <motion.div
            key="application"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <ApplicationDetailsStep
              values={application}
              onChange={updateApplicationField}
              onBack={() => {
                setError(null)
                setStep('details')
              }}
              onContinue={() => void submitApplicantAndContinue()}
              canContinue={canProceedFromApplication}
              submitting={submittingApplicant}
              error={error}
            />
          </motion.div>
        )}

        {step === 'party' && (
          <motion.div
            key="party"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <PartySelectStep
              selection={partySelection}
              onChange={(next) => {
                setPartySelection(next)
                setError(null)
              }}
              onBack={() => {
                setError(null)
                setStep('application')
              }}
              onContinue={goToDocuments}
              canContinue={canProceedFromParty}
              error={error}
            />
          </motion.div>
        )}

        {step === 'documents' && (
          <motion.div
            key="documents"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={FADE}
          >
            <DocumentUploadStep
              partySelection={partySelection}
              activeParty={activeParty}
              onActivePartyChange={setActiveParty}
              primaryDocs={primaryDocs}
              coDocs={coDocs}
              loading={loading}
              verifying={verifying}
              error={error}
              showOtherPartyPrompt={showOtherPartyPrompt}
              onUpload={uploadAndVerify}
              onRemoveDoc={removeDoc}
              onSwitchOtherParty={switchToOtherParty}
              onDismissOtherPartyPrompt={dismissOtherPartyPrompt}
              onBack={() => {
                setError(null)
                setStep('party')
              }}
              onRunVerification={runVerification}
              canRunVerification={successDocs.length > 0}
            />
          </motion.div>
        )}

        {step === 'report' && result && (
          <motion.div
            key="report"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
            className="space-y-4"
          >
            <ProfileMatchPanel profile={profileSnapshot} result={result} />
            <ResultsPanel
              result={result}
              onReset={reset}
              onAddDocuments={addMoreDocuments}
              uploadedFiles={verifiedDocs.map((d) => d.item)}
            />
          </motion.div>
        )}

        {step === 'report' && !result && (
          <motion.div
            key="report-empty"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="card space-y-3 p-6 text-center"
          >
            <p className="text-[14px] text-content-secondary">
              No verification result. Upload documents and run verification.
            </p>
            <button type="button" className="btn btn-primary" onClick={() => setStep('documents')}>
              Back to documents
            </button>
          </motion.div>
        )}
      </AnimatePresence>

      <Chatbot
        caseId={caseId || undefined}
        applicantId={applicantId || undefined}
        partyId={partyId}
        stage={user?.stage}
        accessToken={accessToken || undefined}
      />
    </div>
  )
}

// ResumeCaseCard removed — case/app ID selection is now handled by
// CaseSelectPage (shown before ProcessPage, managed in App.tsx).
