from evals.copilot import party_kyc as pk
from evals.copilot.harness import Harness
h=Harness(live=False)
try:
    pk._seed_co(h)
    for convo in (["jo pending hai kar do"],["mera application check kar aur jo pending hai kar de"],["abhi kya kar sakte ho?","haan"],
                  ["everything okay with my application?"],["what still needs me?"],
                  ["माझं application कुठल्या stage वर आहे?"],["मेरे दस्तावेज़ क्या बाकी है?"]):
        ctx=None
        for q in convo:
            o=pk.post(h,q,ctx,"owner"); r=o["r"]; ctx=r.get("context") or ctx
            print(repr(q),'->',o["status"],r.get("intent"),r.get("response_type"),bool(r.get("clarification_required")),(r.get("pending_work") or {}).get("summary"),'|',str(r.get("answer"))[:400].replace("\n"," / "))
            print('   lang:', {k:(r.get("language_contract") or {}).get(k) for k in ("response_language","reply_language","localized")})
        print('--')
finally:
    h.stop()
