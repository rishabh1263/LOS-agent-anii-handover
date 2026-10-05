import { AnimatePresence, motion } from 'motion/react'
import { useKycWizard } from '../../../runtime/api-tester'
import { useAuth } from '../../../runtime/auth'
import { RequireAuth } from '../../auth'
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

/**
 * Login (stage) → Basic details → Application → POST/GET FOS applicant →
 * Party → Documents → Report. Chatbot uses copilot query with case context.
 */
export function ProcessPage() {
  return (
    <RequireAuth>
      <ProcessPageContent />
    </RequireAuth>
  )
}

function ProcessPageContent() {
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
  } = useKycWizard()

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
          {(applicantId || caseId) && (
            <p className="mt-2 font-mono text-[12px] text-content-secondary">
              {applicantId && <span>Applicant: {applicantId}</span>}
              {applicantId && caseId && <span className="mx-2 text-content-disabled">·</span>}
              {caseId && <span>Case: {caseId}</span>}
            </p>
          )}
        </div>
        <WizardProgress current={step === 'report' ? 'report' : step} />
      </div>

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
