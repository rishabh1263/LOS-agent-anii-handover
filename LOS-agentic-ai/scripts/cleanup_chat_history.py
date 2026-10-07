"""
Chat history retention and forget-me (migration 0006; app/store/chat_history.py).

    python -m scripts.cleanup_chat_history --dry-run            # counts only
    python -m scripts.cleanup_chat_history                      # delete expired turns + old rollback backups
    python -m scripts.cleanup_chat_history --forget-subject SUB # FORGET ME: every stored turn of one subject

The same cleanup runs automatically at startup and every `chatbot.memory.cleanup_interval_hours`
when COPILOT_SESSION_MEMORY is on. Prints counts only -- never any turn text.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--forget-subject", default=None)
    args = p.parse_args()
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.store import chat_history

    if args.forget_subject:
        print(json.dumps({"forgotten_rows": chat_history.forget(args.forget_subject)}))
        return 0
    print(json.dumps(chat_history.cleanup(dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
