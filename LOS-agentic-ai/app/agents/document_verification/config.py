import os


def enabled() -> bool:
    return os.getenv("DOCUMENT_AGENT_ENABLED", "true").lower() == "true"


def version() -> str:
    return os.getenv("DOCUMENT_AGENT_VERSION", "1.0.0")
