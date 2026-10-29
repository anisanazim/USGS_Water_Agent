from dataclasses import dataclass, field
from typing import Any

from .store import ResultStore

@dataclass
class RequestContext:
    request_id: str
    store: ResultStore = field(init=False)
    ichatbio_process: Any = None
    tool_errors: list[dict] = field(default_factory=list, init=False)
    trace_id: str = field(init=False)
    spans: list[Any] = field(default_factory=list, init=False)

    def __post_init__(self):
        self.store = ResultStore(request_id=self.request_id)
        self.trace_id = self.request_id

