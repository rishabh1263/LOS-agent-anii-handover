from dataclasses import dataclass, field
from typing import Any


@dataclass
class DocumentWorkflowState:
    request_id: str
    document_type: str
    file_path: str
    user_request: str
    result: dict[str, Any] = field(default_factory=dict)
