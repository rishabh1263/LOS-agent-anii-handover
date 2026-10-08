from tests.integration.master_env import make_case, prod  # noqa
from tests.integration.test_reupload_supersedes import _store, client  # noqa
def test_dump(client, prod):
    for n in ("Rahul Sharma", "Priya Verma"):
        make_case(client, n)
    for m in ["what can you help with?", "ok", "mera cases kya hai", "what is CPA", "how to move cpa",
              "mandatory documents for Home loan", "process to verify documents", "what is my role",
              "what is my stage", "WHAT IS NAME", "FNR meaning", "why you are not answering me", "kyc status", "1"]:
        r = client.post("/api/v1/fos/copilot", json={"action": "CUSTOM_QUERY", "message": m, "reply_language": "en", "chat_id": "kg"})
        b = r.json()
        print("\n>>>", m, "\n" + "\n".join(l for l in b.get("markdown", r.text).split("\n") if not l.startswith("|"))[:500])
