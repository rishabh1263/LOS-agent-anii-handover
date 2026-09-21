"""
LOS flow: Document Agent -> Financial Agent -> KYC -> one response.

Intentionally free of imports. The flow module imports the Document Agent
workflow, and that workflow imports this package's summary module, so anything
eagerly imported here would close that cycle at import time.
"""
