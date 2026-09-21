"""
Alias entrypoint.

Kept because existing run commands and deployment scripts reference
`risk_main:app`. It now serves exactly the same application as main.py --
having two entrypoints that behaved differently was itself a source of
confusion, including a 60-second timeout traced to a router that was wired
into one file and not the other.

Run:
    uvicorn risk_main:app --host 0.0.0.0 --port 8011
"""

from main import app

__all__ = ["app"]
