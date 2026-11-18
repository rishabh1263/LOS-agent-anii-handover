# Crime Record Verification: Manager ke liye simple explanation

## 1. Ye kya karta hai?
Loan dene se pehle hum check karte hain ki **applicant aur co-applicant** ke naam pe koi court case, tribunal case,
defaulter list entry ya FIR to nahi hai. Ye kaam ye agent karta hai. Ye **Credit Underwriting** ka hissa hai aur
LOS server pe hi chalta hai:
- screen: `/crime-verification/`
- API: `/api/v1/credit/crime-verification`

Abhi ye **demo mode** me hai: data nakli hai, aur vendor bhi ek nakli (mock) "Crime Check" hai. Aapke kehne pe screen
aur PDF pe "demo" kahin nahi likha aata; ye config se on/off hota hai (`ui.show_demo_banner`,
`report.show_demo_notice`). Kisi asli insaan ki
koi search nahi hoti.

## 2. Applicant aur co-applicant ki details kahan se aati hain?
Application kholte hi dono logon ki details application record se aa jaati hain: naam, pita ka naam, DOB, pakka
address aur PAN.
- Har field ke saath likha hota hai **"From application"**. Officer kuch badle to wahan **"Edited"** dikhta hai.
- PAN hamesha chhupa dikhta hai (`XXXXXX001Q`); poora PAN server se bahar nahi jaata.
- Officer chahe to poora PAN type kar sakta hai, lekin wo application wale PAN se match hona chahiye.
- Koi field khaali ya galat ho (jaise DOB future me, PAN galat format me, address chhota) to wahin laal message aata hai.

## 3. Confirmation popup kya dikhata hai, kab aata hai?
"Initiate Verification" dabane pe system pehle sab details check karta hai.
- Kuch galat hai to popup **nahi** khulta; batata hai ki kya theek karna hai.
- Sab sahi hai to popup khulta hai: **"Confirm Applicant Details and Initiate Crime Verification?"**

Popup me dono logon ki details hoti hain (naam, pita ka naam, DOB, address, chhupa PAN) aur ek warning ki confirm
karte hi verification shuru ho jayega. Do button hain: **Cancel** aur **Confirm & Initiate Verification**.
Popup band hone se pehle server pe kuch nahi jaata.

## 4. Confirm dabane ke baad kya hota hai?
1. Button "Initiating Verification…" ban jaata hai aur disable ho jaata hai. Do baar click se do case nahi bante;
   ek unique submission key bhi jaati hai.
2. Server dobara sab check karta hai: details, PAN application ka hi hai na, user ko is branch ki application dekhne
   ki permission hai na, aur us insaan ka koi check pehle se chal to nahi raha.
3. Confirm ki hui details ka snapshot database me save hota hai, aur audit me likha jaata hai ki kisne kab confirm kiya.
4. Har insaan ke liye provider ko request jaati hai, aur uska result alag-alag save hota hai.
5. Popup band hota hai, aur case number ke saath result dikhta hai. Fail hua to popup khula rehta hai, error dikhta hai,
   aur bhari hui details bachi rehti hain.

## 5. Mock CrimeCheck result kaise banata hai?
Mock provider ek fixed "scenario" se jawab deta hai, jaise No Risk, Low Risk, Average Risk, High Risk, Multiple
Findings, Vendor Failure ya Pending. Har application pe scenario config me likha hai. (Config se "Scenario"
dropdown se badal bhi sakte hain. Jawab ka format sample CrimeCheck report jaisa hai:
- risk level;
- har source (Supreme Court, High Courts, Civil/Criminal Courts, Tribunals, Defaulter Lists, FIR) me kitne case mile,
  aur coverage kab se hai;
- matter type ke hisaab se ginti (Civil, Financial, Statutory, Other).

Asli vendor aane pe sirf ek naya "adapter" likhna padega; screen, database aur API waise hi rahenge.

## 6. PostgreSQL me kya store hota hai?
Agent ka **apna alag PostgreSQL database** hai; LOS ka database chhua nahi gaya. Tables ye hain:
- applications aur parties (demo applications);
- **cases**: har verification request;
- **subjects**: har insaan ki confirm ki hui details;
- **results**: provider ka result;
- **reports**: PDF ka reference aur hash;
- **audit**: har action;
- **summaries**: AI summary.

Database khud rokta hai ki:
- koi insaan galat application se na jud jaye;
- ek insaan ke do check ek saath na chalein.

## 7. Screen kaisi dikhti hai?
Bank Verification se alag, apni pehchaan wali "Aurora" theme: halki screen, violet-se-gulabi gradient aur glass jaise
cards. Teen screens hain:
- **Dashboard:** gradient hero me ek ring dikhata hai ki kitne cases ke result aa chuke hain. Uske neeche 6 KPI cards
  hain jinke number gin-ke badhte hain, aur cases ki table. Kisi row pe click karo to us case ka results page khulta hai.
- **Verify (do hisse):**
  - **left:** bada search box "Case or application ID" (APP-… ya CRV-…), filter chips (All / Not started /
    In progress / With results) aur applications ki list.
  - **right:** chuni hui application ka form. Applicant aur Co-Applicant ke tabs hain, har tab pe "include" ka switch.
    Form ke do section hain:
    - **Identity:** naam, pita ka naam, DOB (saath me umar), masked PAN;
    - **Permanent address:** line, phir **State** aur **City** ke dropdown, phir PIN.
  - **Fill sample** button ek click me realistic values bhar deta hai (PAN nahi chhoota).
  - Neeche ek sticky bar me **Initiate Verification** button hai.
- **Results (agla page):** Confirm ke baad khud khulta hai.
  - Upar hero me "All clear" / "Review required", stepper, aur kitne log check hue / kitne case mile.
  - Har insaan ka alag card, jisme CrimeCheck report jaisa **risk meter** hai. Meter ke har hisse pe naam likha hai
    (No Risk, Low, Average, High, Very High), sui sahi level pe rukti hai, aur neeche bada likha aata hai, jaise
    "High Risk".
  - Phir AI summary card (typing animation, "Clean result" / "Needs review"), decision aur activity.
- **Celebration:** sab saaf (No/Low Risk) ho to:
  - beech se rangin burst aur "All clear" ka badge;
  - phool aur pankhudiyan;
  - ek chhoti bell jaisi **jeet ki dhun**.

  Upar right me **Sound on/off** switch hai, jo yaad rehta hai. Kam motion chahne walon ko animation nahi dikhta.

Mobile aur dark mode dono pe chalta hai.

## PDF report
- Upar rangin header band, phir applicant ki details, "Result at a glance" (bada risk, 5-step scale, 4 numbers),
  summary, findings, matter type, case ki details, poori coverage, risk guide.
- Har page pe footer aur "Page x of n" hota hai.
- Background me security paper jaisi **lehrati lines (guilloche)**, page ke chaaron taraf patla frame, aur halka
  tircha **"CONFIDENTIAL"** watermark hai. Teeno config se on/off hote hain (`report.background`).

## 8. Alag-alag result pe user kya kare?
- **No Risk / Low Risk:** status Completed. Dhyan rahe: "no synthetic matches" ka matlab ye nahi ki asli duniya me
  record nahi hai.
- **Average / High Risk:** status **Review required**. Officer findings dekhe, PDF le, aur "Refer to credit" ya
  "Decline recommended" remarks ke saath chune.
- **Pending:** status Processing. "Refresh status" dabake dobara poochho.
- **Failed:** vendor ne jawab nahi diya; reason dikhta hai. Dobara Initiate karo, ye retry hai.
- **Waived:** pehle waive hua tha; reason dikhta hai. **ReTrigger** dabao; wahi confirmation popup aayega.

Dono logon ka result **alag-alag** rehta hai; system dono ko milake ek risk nahi banata.

## 9. AI summary
Result aate hi local AI (Qwen) 2-4 seedhi lines me batata hai ki kiska kya result aaya. Ye summary screen pe aur
PDF me dono jagah aati hai. Test me screen pe ~15 second me aa gayi. AI ko naam ya PAN nahi diya jaata, sirf result.
Uska likha text check hota hai:
- har number result me hona chahiye;
- "approved", "criminal record", "clean record" jaise shabd mana hain.

Check fail ho ya AI band ho, to system ki apni likhi summary dikhti hai. AI sirf samjhata hai; faisla officer karta hai.

## 10. Kya automated hai, production ke liye kya chahiye?
**Automated:**
- details bharna;
- validation;
- duplicate rokna;
- provider ko bhejna;
- result save karna;
- risk ke hisaab se Review required lagana;
- summary;
- PDF;
- audit;
- dashboard.

**Production ke liye chahiye:**
- asli, authorised CrimeCheck API ka adapter;
- asli LOS applications se link;
- risk policy ki sign-off (kaun sa risk review maange, kaun se decisions);
- `demo_mode: false`;
- database ka DSN `.env` me.

## 11. Ek poora example (APP-2026-008)
1. Dashboard → Applications → **APP-2026-008** (Arjun Nair, Car Loan ₹9,00,000).
2. Dono log ticked hain, aur details "From application" se bhari hain.
3. **Initiate Verification** dabaya, to popup khula. Dono ki details aur chhupa PAN check kiye, phir **Confirm** dabaya.
4. Case `CRV-…` bana:
   - Applicant: **No Risk**, Completed;
   - Co-Applicant: **Average Risk** (3 nakli cases), **Review required**.
5. ~15 sec me AI summary aayi: "The Applicant has no matches found in the covered sources. The Co-Applicant has
   3 cases found…"
6. Co-Applicant ka **Download report (PDF)** liya. Usme demo notice, summary, details, risk, findings ki table,
   coverage aur limitations hain.
7. Officer ne "Refer to credit" chuna, remarks likhe aur **Submit decision** dabaya.
8. Activity me poori kahani dikhti hai: confirm → provider request → result → report → decision.

## Test results (2026-10-27)
- Backend: **45/45 pass**, **asli PostgreSQL** pe, asli signed LOS tokens ke saath.
- Frontend: **10/10 pass**.
- Browser me poora flow chala ke dekha: popup, confirm, results, validation, asli AI summary, PDF, mobile; console me
  koi error nahi.

## 12. Naya (2026-10-27): 10 co-applicants tak, sab config me, summary upar

- **Ek application pe 1 applicant + 10 co-applicants tak** ho sakte hain. Ye ginti config me hai:
  `subjects.max_co_applicants`.
  - Form me har insaan ka apna tab hai, aur tab pe include ka switch. Jitne chaho utne chuno.
  - Popup me sab chune hue log dikhte hain, aur results me har insaan ka alag card.
  - Naam number ke saath aate hain: "Co-Applicant 1", "Co-Applicant 2"…
  - Database me har insaan (role, number) se pehchana jaata hai. Purana data apne aap "Co-Applicant 1" ban jaata hai.
  - Demo me **APP-2026-012** pe 4 co-applicants hain.
- **Sab config me:** screen ka har shabd (buttons, titles, steps, filters, popup ka text) YAML me hai. Ek test
  pakadta hai agar screen koi aisa text use kare jo config me nahi hai.
- **AI summary sabse upar:** results page pe hero ke theek neeche. Pehle "At a glance" chips me har insaan ka risk
  rang ke saath, phir AI ki summary.
  - Summary ki lambai logon ki ginti ke hisaab se badhti hai, taaki sabka naam aaye.
  - Galat baat, jaise jiska result aa chuka hai use "not available" bolna, check me pakdi jaati hai. Tab system ki
    likhi summary dikhti hai.

## 13. Naya (2026-10-28): kharab result pe alarm, aur production ke liye taiyar

**Alarm:** jaise saaf result pe celebration aata hai, waise hi jab kisi ka risk Average / High / Very High ho
(config `ui.alarm_risks`) to screen pe laal alarm aata hai:
- laal rings;
- hilta hua warning icon;
- "Review required — Findings for Co-Applicant 1 (High Risk)". Isme naam se batata hai ki findings kis pe hain.

Saath me ek halki do-tone awaaz bajti hai (`ui.alarm_sound`, aur screen ka Sound on/off button). Har case pe ek hi baar
aata hai, 4 second me chala jaata hai, aur kuch block nahi karta.

**Production ke liye kya karna hai (bas ye):**
1. `demo_mode: false` (crime_verification.yaml).
2. `CRIME_VERIFICATION_DSN` = asli PostgreSQL. Iske bina agent start nahi hota.
3. Asli CrimeCheck adapter `providers/` me register karna, aur config `vendors.CRIME_CHECK.provider` me uska naam
   daalna. Mock production me chalta hi nahi: 503 aata hai aur koi case nahi banta.
4. LOS har application aur uske log `PUT /applications/{id}` se bhejega (`los.write` token).

**Production me apne aap kya hota hai:**
- demo data, reset, scenario aur sample nahi;
- demo users ki extra permission nahi milti;
- vendor down ho to us person pe "Failed, retry kar sakte ho" aata hai, crash nahi;
- vendor galat risk bheje to "rejected" hota hai, galat result save nahi hota;
- jis person ki verification ho chuki hai, uska PAN nahi badalta aur use hataya nahi ja sakta (history sach rehti hai).

**Tests:** 95 pass, real PostgreSQL pe: 54 purane + 41 naye production tests (positive aur negative dono).
Frontend ke 18 tests pass.
