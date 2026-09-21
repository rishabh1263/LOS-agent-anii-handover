import os


def tracing_enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "false").lower() == "true"
