import { AnimatePresence, motion } from 'motion/react'
import { useKycWizard } from '../../../runtime/api-tester'
import { RequireAuth } from '../../auth'
import {
  BasicDetailsStep,
  DocumentUploadStep,
  PartySelectStep,
  ProfileMatchPanel,
  ResultsPanel,
  WizardProgress,
} from '../components'

const FADE = { duration: 0.22, ease: [0.22, 1, 0.36, 1] as const }

/**
 * Login → Basic details → Party → Documents (verify on upload) → Run verification → Report
 */
export function ProcessPage() {
  return (
    <RequireAuth>
      <ProcessPageContent />
    </RequireAuth>
  )
}

function ProcessPageContent() {
  const {
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
    goToParty,
    goToDocuments,
    uploadAndVerify,
    removeDoc,
    switchToOtherParty,
    dismissOtherPartyPrompt,
    runVerification,
    reset,
    addMoreDocuments,
  } = useKycWizard()

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6">
      <div className="card space-y-4">
        <div>
          <p className="font-display text-[12px] font-semibold uppercase tracking-[0.09em] text-content-secondary">
            Document verification
          </p>
          <h1 className="mt-2 font-display text-[24px] font-bold tracking-tight text-content sm:text-[28px]">
            KYC verification
          </h1>
          <p className="mt-1.5 max-w-2xl text-[14px] leading-relaxed text-content-secondary">
            Enter profile details, select party, upload documents. Each upload runs VERIFY then
            EXTRACT. Run verification executes PROCESS for the full report.
          </p>
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
              onContinue={goToParty}
              canContinue={canProceedFromDetails}
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
                setStep('details')
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
            {/* 1. Profile / system data match */}
            <ProfileMatchPanel profile={profileSnapshot} result={result} />
            {/* 2–3. KYC, cross-document, decision — from API response only */}
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
    </div>
  )
}
