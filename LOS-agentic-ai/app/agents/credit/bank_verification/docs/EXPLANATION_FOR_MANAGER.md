# Bank Verification Agent: simple explanation (Sahil Sir ke liye)

## Ek line me

Customer ka bank account aur bank statement sahi hai ya nahi, ye system khud check karta hai. CPA ki bhari details ko
statement PDF ke **andar likhe asli data** se milata hai, har farak exact values ke saath dikhata hai, aur case tabhi
**Positive** hota hai jab agency branch se confirm karke report laga de.

Link: `http://127.0.0.1:8010/bank-verification/` (8010 wala LOS server). Case ID ya applicant ID daal ke search karo.

## Kaun kya karta hai

| Kaun | Kya karta hai |
|---|---|
| **Application** | Bank name aur account number deta hai (jisme loan ka paisa jayega) |
| **AA (Account Aggregator)** | Bank ka asli data deta hai, jab AA success ho |
| **CPA** | Statement upload karta hai, account type + last 3 transactions bharta hai, "Run verification" dabata hai |
| **System** | Statement khud padhta hai (PDF ka text, scan ho to OCR), 8 checks chalata hai, AI summary likhta hai |
| **Agency** | Branch jaake confirm karti hai ki account customer ka hai, report upload karti hai, Positive/Negative deti hai |

## AA fail hone pe (5 step)

1. **Case kholo**: Bank Name aur Account Number application se pehle se bhare aate hain (tag: "From application").
   **Kyun?** Ye customer ka **claim** hai (jis account me loan jayega); statement uska **saboot** hai. Number bhi
   statement se bharte to statement ko statement se milate, aur kisi aur account ka statement kabhi pakda nahi jata.
   Application me account na ho (abhi LOS ke asli cases), tab ye bhi statement se bharte hain, CPA ke Check ke saath.
2. **Statement upload karo**: PDF turant side me khulta hai **aur system use usi waqt padh leta hai** (scan ho to OCR).
   Account Type aur last 3 transactions apne aap bhar jaate hain (tag: "From statement").
3. **CPA check kare**: side me khule PDF se har bhari value milao aur **"Check"** dabao (✔ Checked). Galat ho to
   theek karo (tag "Entered"). **Jo value check nahi ki, wo "Needs a person" aayegi, kabhi "Match" nahi** — kyunki
   statement ko statement se milana tab tak kuch saabit nahi karta jab tak insaan ne dekha na ho.
4. **Run verification**: wahi padhai dobara use hoti hai (file do baar nahi padhi jati). Account number application
   se statement ke against milta hai, baaki checks bhi chalte hain. Upar result: Problem / Needs a person.
5. **Agency**: branch confirmation, checklist, report upload, decision.

## AA success hone pe

Form AA ke data se bhar jata hai (tag "From AA"). Statement bhi aata hai. Extra check: **AA ka data vs statement**:
agar customer ne edit kiya hua PDF diya ho, to AA (bank ka asli data) se farak pakda jata hai.

## Compare kaise hota hai (example: case BV-2026-004)

System pehle statement padhta hai: Bank = Axis Bank, Account = ...7881, Type = Savings, aur saari transactions.
Phir form se milata hai:

| Kya | Form | Statement | Result |
|---|---|---|---|
| Bank | AXIS BANK LTD | Axis Bank | Match ("Ltd", SBI jaise chhote naam, IFSC code bhi samajhta hai) |
| Account number | ...7881 (application) | ...7881 | Match (har digit exact) |
| Type | Savings | Savings | Match |
| Transaction 1 | 21-09 / ₹1,240.50 / Debit | 21-09 / ₹1,240.50 / Debit | Match |
| Transaction 2 | 21-09 / **₹5,500** / Debit | 21-09 / **₹5,000** / Debit | **Problem: amount alag** |
| Transaction 3 | 05-09 / ₹12,500 / Debit | 05-09 / ₹12,500 / Debit | Match |

Har transaction ki **teen cheezein ek saath** milni chahiye: date + amount + Credit/Debit, statement ki **sabse nayi
3** rows me.

## 8 checks

| # | Check | Kya dekhta hai |
|---|---|---|
| 1 | Bank details | Bank, account number, type: form vs statement |
| 2 | Last 3 transactions | Date + amount + Credit/Debit: form vs statement ki latest 3 |
| 3 | Statement looks genuine | Edit tool se bana? Baad me badla? Poora padha gaya? Dates period ke andar? |
| 4 | Account belongs to customer | Branch confirmation (sirf agency deti hai) |
| 5 | Balances add up | Har row: pichla balance ± amount = naya balance |
| 6 | AA vs statement | Sirf AA success me; AA fail me "Not needed" |
| 7 | Not reused elsewhere | Yahi file / yahi transactions kisi aur case me to nahi? |
| 8 | Overall result | Upar ka verdict: koi Problem → Problem; koi "Needs a person" → Needs a person |

## System kab "OK" nahi bolta (sabse important)

* Statement padha hi nahi gaya (password wala, blur scan, kharab file) → **Needs a person**, "readable statement mangao".
* Statement pe account number chhupa hai (XXXX7881) → sirf last digits mile, isliye **Needs a person**, OK nahi.
* Statement ka aakhri page nahi padha gaya → "latest 3" pakke nahi, isliye **Needs a person**.
* Form me kuch khaali chhoda → **Needs a person**.
* Kabhi bhi andaze se ya nakli data se "OK" nahi.

## Agency report ka check

Agency jo report upload karti hai (PDF ya photo), system use padhta hai (photo ho to OCR) aur screen ke data se
milata hai:

| Check | Kya dekhta hai |
|---|---|
| Report padhi gayi? | Text mila ya nahi |
| Isi case ki hai? | Case ID / application number report me hai; kisi aur case ka number ho to pakda jata hai |
| Date | Report ki date hai, future ki nahi, 30 din se purani nahi |
| Customer naam | Applicant se (order / middle initial chalta hai) |
| Account number | Verify hue account se (chhupa ho to last 4 digit) |
| Bank | SBI = State Bank of India jaise naam chalte hain |
| Ownership result | Report me likha "Confirmed / Not confirmed" vs jo agency ne screen pe chuna |
| Agency naam | Assigned agency se |

Report chunte hi screen pe table dikhti hai. **Report check pass na ho to Positive nahi hota.** Sab labels, shabd,
din ki limit config me (`agency_report`). Sample reports: `runtime/credit_bank_verification/sample_reports/`.

## Laal (Problem) checks ka popup

Run ke baad koi check **Problem** (laal) ho to ek popup aata hai: kaun se checks, kya alag hai (jaise "₹5,500 vs
₹5,000"), aur "Show" se wahi check khul jata hai. Sirf laal ke liye; ek result pe ek baar band kiya to dobara nahi.

## Positive kab hota hai

Agency ke bina kabhi nahi. Positive ke liye sab chahiye:
* account ownership **CONFIRMED** + branch visit **COMPLETED**;
* agency checklist ke saare points **Verified** + reason likha ho;
* **verification report upload** (report ke bina submit hi reject hota hai);
* statement padha gaya ho; bank details / transactions / genuine / balances / AA wale checks me koi Problem na ho.

Kuch bhi kam ho to case "Needs review" me rehta hai aur screen batati hai kya kami hai.

## AI summary

Local Qwen model (data bahar nahi jata) result ko 2-5 aasaan lines me likhta hai, har problem exact values ke saath
("Transaction 2 ₹5,500 bhara, statement me ₹5,000"). Wo sirf **samjhata** hai, decide nahi karta. Uska text check hota
hai: koi number facts me nahi, ya "approved / fraud" jaisa shabd → nahi dikhta, rules wala summary dikhta hai. AI ke
aane tak (7-13 sec) rules wala summary turant dikhta hai.

## Security

* Account number screen, API aur report me hamesha chhupa (XXXX7881); matching poore number se hoti hai.
* Audit history aur logs me poora account number nahi jata.
* Roles: CPA process karta hai, agency finalize karti hai, viewer sirf dekhta hai; doosri agency ka case finalize
  nahi kar sakti.
* Error me sirf saaf message, koi technical detail nahi.
* Production me role LOS login ke token se aata hai: `los.bank.agency` = agency, `los.credit.underwrite` / `los.write`
  = CPA, `los.read` = viewer. Agency ka id bhi token se aata hai, aur woh sirf apne liye decision de sakti hai.
* Production me data asli PostgreSQL me rehta hai (`BANK_VERIFICATION_DSN`). DSN na ho to agent start hi nahi hota,
  SQLite pe chupke se nahi chalta.

## Demo ke liye cases

| Case | Kya dikhata hai |
|---|---|
| BV-2026-001 | AA success, sab theek → agency ke baad Positive |
| BV-2026-002 | AA fail, saaf statement |
| BV-2026-004 | AA fail, transaction galat bhari → Problem |
| BV-2026-005 | Blur scan → padh nahi paya → Needs a person |
| BV-2026-006 | Balance ka hisaab toota → Problem |
| BV-2026-008 | Same statement doosre case me → Needs a person |
| BV-2026-012 | AA aur statement me farak → Problem |

AA fail cases ke statements: `runtime/credit_bank_verification/sample_statements/`.

## Abhi ki seemayein (honestly)

* LOS ke asli cases me application ka account number abhi nahi aata: LOS application me bank account column hi nahi
  hai. Migration 0013 (do nullable columns) approval ke liye ruki hai.
* Demo mode me role header se aata hai; production me LOS login se aana chahiye (abhi wired nahi).
* Matching ke liye poora account number agent ke apne store (SQLite) me rakha jata hai; production me encryption at
  rest chahiye.
* AI summary CPU pe 7-13 sec (bahut problems ho to ~30 sec) leta hai.

## Sir ko 3 line me

1. "AA fail me account number application se aata hai; CPA bas type aur last 3 transactions bharta hai."
2. "System statement PDF khud padh ke bank, account, aur har transaction ki date-amount-Credit/Debit milata hai; farak
   ho to exact values ke saath dikhata hai."
3. "Statement na padhe, adhoora ho, ya kuch pakka na ho to system kabhi OK nahi bolta; Positive sirf agency ki branch
   confirmation aur report ke baad."

## AA aaya + statement bhi upload (2026-10-28, Sahil Sir ki baat)

AA success me customer kabhi bank statement bhi deta hai. Tab data **do jagah se** aata hai: AA se aur statement se.
Ab system **sab milata hai**, aur har check ka ek hi kaam hai, taaki kuch messy na ho:

1. **Form ↔ AA data:**
   - R1 bank, account number, type;
   - R2 latest 3 transactions;
   - R4 account holder ka naam.

   AA bank se seedha aata hai, isliye wahi "reference" hai. Ye config me hai: `reference_source`.
2. **AA data ↔ Statement (R6):** bank, account number, type, IFSC, holder, opening/closing balance, aur dono ke common
   period ki har transaction. Jo alag ho uska naam aata hai, jaise "sirf AA me: 21-09-2026 ₹1,240.50 debit | sirf
   statement me: ₹2,240.50".
3. **Statement khud:** integrity/tampering (R3), kisi aur case me reuse (R7), balance continuity (R5).

Teen halaat:
- **Sirf AA aaya, statement nahi:** run sirf AA se chalta hai. Statement wale checks "not applicable" aate hain, aur
  "statement nahi hai" ka error ab nahi aata. Pehle aata tha; wo bug tha.
- **Statement upload kiya:** wo turant case pe save ho jaata hai aur screen pe saath me dikhta hai. Form AA wala hi
  rehta hai. Run verification pe R6 dono ko milata hai.
- **Results table me** har value ke saath likha hota hai ki wo kisse milayi gayi: "AA data" ya "Statement".
