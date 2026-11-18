# Legal Collateral Verification — Manager Guide (Hinglish)

Property jo loan ke against girvi rakhi ja rahi hai, uska legal check. CPA, legal agency aur hamara agent teeno mil ke
kaam karte hain. Agent kabhi khud "title clear" declare nahi karta — wo saboot dikhata hai, CPA faisla karta hai.

Screen: `/legal-verification/` (LOS server 8010) · API: `/api/v1/credit/legal-verification` · Apna Postgres DB
(`LEGAL_VERIFICATION_DSN`, database `los_legal_verification`) · MCP tool: `legal_verification.get` (credit MCP server).

## Poora workflow: CPA → Agency → Agent → CPA

1. **Application aati hai** — LOS application bhejta hai (applicant, loan, property type, address, owners).
2. **Application Details (Leviosa jaisa)** — Reference ID, External ID, product, loan, stage, status, assigned to,
   collateral; property type / usage / nature / document; owner ka first / middle / last name; owner aur applicant ka
   PAN (sirf masked, poora PAN kabhi save nahi hota); address ke saare hisse; aur **Legal vendor** ka dropdown.
   Neeche teen button: **Waive** (wajah ke saath — legal check nahi chahiye), **Save** (vendor save, case nahi
   jaata), **Initiate** (confirmation ke baad agency ko bhejo).
3. **CPA documents upload karta hai** — Sale Deed aur supporting documents (EC, tax receipt, plan, NOC...).
   **OCR nahi hota**: file sirf save hoti hai. CPA Sale Deed ki details dobara type **nahi** karta — Sale Deed ka
   owner, deed number, date, Sub-Registrar office aur location agency apni Verification Report me bharti hai, aur
   rules wahi values application se milate hain.
4. **Agency apni Verification Report bharti hai — Leviosa form ke fields**: property owners (first / middle / last),
   property belongs to, documents provided, title / property history, document type, registered mortgage (Yes/No),
   signing authority, documents required prior / post disbursal, **title docs checked for last 13 years** (Yes/No),
   conclusion / observation. Saath me rules ke liye: Sale Deed number/date, Sub-Registrar, transfers ki list, EC,
   **CERSAI search** (records found + har search ka type, time, reference, result), legal checks, title opinion.
5. **Agent 10 rules chalata hai** — source alag-alag rakh ke milata hai: Application (LOS), Agency report (Sale Deed
   ki details bhi isi me), aur uploaded file ki jaankari (Agent).
   Report submit hote hi rules apne aap chalte hain; CPA "Run validation" se dobara bhi chala sakta hai.
6. **Recommendation + summary** — CLEAR_FOR_REVIEW / REVIEW_REQUIRED / ESCALATE, key findings, missing evidence, ek
   next action. Upar hamesha summary points me (AI likhta hai, check hoke; AI na ho to rules wali summary).
7. **CPA faisla karta hai** — Proceed / Agency ko wapas / Legal team ko escalate / Collateral reject. Escalate wale
   case me Proceed band hai; kuch faislon me remarks zaroori. "Agency ko wapas" se case dobara agency ke paas.
8. **PDF report** — poora case ek PDF me (recommendation, summary, rules, agency report, documents, decision).

Sab kuch audit me: upload, details, correction, agency ko bhejna, report, validation, faisla, report download.

## Das rules — aasaan bhasha me

| # | Rule | Kya check hota hai |
|---|------|--------------------|
| 1 | **Owner Details Match** | Agency ne Sale Deed pe jo owner bataya, wahi application me hai? Naam alag = laal (critical). |
| 2 | **Sale Deed Looks Consistent** | File khulti hai, pages hain, editing tool ya baad me badlav ke nishaan to nahi? Ye sirf "dekho" ka signal hai — kabhi "fake" ka saboot nahi. |
| 3 | **Property Address Matches** | Agency ka bataya District / State application wale se milta hai? Alag = laal. |
| 4 | **Ownership History** | Purane owner se aaj ke owner tak chain bina toote judti hai? Chain toote ya aaj ke owner pe khatam na ho = laal; chhoti chain = amber. |
| 5 | **Registration Details** | Agency ne Sale Deed number, registration date aur Sub-Registrar office diye? Date galat / future me = laal; koi missing = amber. |
| 6 | **Loan, Mortgage and Other Claims** | EC aur CERSAI me koi purana loan/charge to nahi? Charge mila = laal (critical). EC/CERSAI search hi nahi = amber — "saboot nahi" ka matlab "charge nahi" kabhi nahi. |
| 7 | **Required Documents and Search Period** | Property type ke zaroori documents file pe hain, aur title search pichhle 13 saal ke hain (Leviosa ka "title docs checked for last 13 years")? |
| 8 | **Property-Specific Legal Checks** | Is property type ke legal checks (plan approval, OC, society NOC, mutation...) agency ne kiye? Adverse = laal, baaki = amber. |
| 9 | **Agency Report Cross-Check** | Agency ka property type application se milta hai? Agency "title clear" bole par baaki rules me laal ho = laal. |
| 10 | **Overall Risk and Next Action** | Sab rules milake: kya dikkat hai, kya saboot missing hai, CPA aage kya kare. Recommendation deta hai, faisla CPA ka. |

Rang: **laal** = mismatch (pehle dikhte hain), **amber** = review / saboot missing, **hara** = match.

## Recommendation kaise banti hai

- **ESCALATE** — koi critical rule (1 ya 6) laal, ya 2 ya zyada rules laal.
- **REVIEW_REQUIRED** — koi bhi laal ya amber.
- **CLEAR_FOR_REVIEW** — sab hara. Tab bhi CPA review karke hi faisla karta hai.

## Config me kya hai (code me kuch hardcoded nahi)

`app/config/credit_underwriting/legal_verification.yaml` — agencies, property types (zaroori documents + legal checks),
title search period (13 saal, Leviosa form se), EC period (13 saal), agency report ke sections/fields (CERSAI search samet), rule
severity, recommendation ke niyam, CPA ke faisle, permissions. `legal_verification_wording.yaml` — screen, rules, PDF ke
saare shabd. `legal_verification_demo.yaml` — demo applications (sab synthetic) aur "Fill sample" ke pools.

**Status: DEMO_DUMMY** — periods, zaroori documents aur legal checks shuruaati values hain. Credit / legal owner inhe
confirm kare (config me badlav, code nahi). Agency report aur Application Details ke fields Leviosa ke screenshots se
liye gaye hain; "Property belongs to" ke dropdown ke options abhi andaze ke hain (Leviosa ki asli list chahiye).
