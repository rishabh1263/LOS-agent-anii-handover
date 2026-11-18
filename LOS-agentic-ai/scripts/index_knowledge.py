"""
BUILD THE CHATBOT'S KNOWLEDGE INDEX in the configured vector DB (Qdrant: QDRANT_URL, or QDRANT_PATH locally).

    python scripts/index_knowledge.py            # the process knowledge (stage guides) -> los_process_knowledge
    python scripts/index_knowledge.py --check    # where it points and how many points each collection holds

Run it once after a new Qdrant server is set up, and again when the guides change. It is idempotent: a chunk's id is
derived from its content, so a re-run replaces, never doubles. Case chunks are indexed by the LOS server itself as cases
are read (scoped to their applicant). With QDRANT_PATH (embedded) the LOS server must be STOPPED while this runs: the
embedded store allows one process. Never prints the URL's credentials or the API key.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report only")
    args = parser.parse_args(argv)

    from app.knowledge import indexing
    from app.knowledge.vector_store import (VectorStoreError, case_collection, configured_path, configured_url,
                                            get_vector_store, knowledge_collection)

    url, path = configured_url(), configured_path()
    where = ("a Qdrant server (QDRANT_URL)" if url else f"the embedded folder {path} (QDRANT_PATH)" if path
             else "MEMORY ONLY (nothing kept) -- set QDRANT_URL or QDRANT_PATH first")
    print(f"vector DB: {where}")
    if not url and not path:
        return 1
    try:
        store = get_vector_store()
        if not args.check:
            added = indexing.index_process_knowledge(store)
            print(f"process knowledge indexed: {added} chunks into {knowledge_collection()}")
        for name in (knowledge_collection(), case_collection()):
            n = store.count(name)
            empty = ("not created yet (run this script without --check)" if name == knowledge_collection()
                     else "not created yet (filled as cases are read)")
            print(f"  {name:<28} " + (f"{n} points" if n is not None else empty))
    except VectorStoreError as exc:
        print(f"FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - one plain line (a server down, a model down)
        print(f"FAILED: {type(exc).__name__} -- is the Qdrant server running, and the embedding model (Ollama)?")
        return 1
    finally:
        try:                                   # closed here, not at interpreter exit (qdrant-client complains then)
            client = getattr(get_vector_store(), "_client", None)
            if client is not None:
                client.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raise SystemExit(main())
