import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_SAMPLE = 5

class ResultHandle(BaseModel):
    ref: str
    kind: str
    count: int
    sample: list[dict]
    summary: str
    truncated: bool = False

class StoredResult(BaseModel):
    handle: ResultHandle
    items: Any
    provenance: dict
    artifact_id: str | None = None

class UnknownRefError(Exception):
    def __init__(self, ref: str, available_refs: list[dict]):
        super().__init__(f"Unknown ref '{ref}'. Available: {available_refs}")
        self.ref = ref
        self.available_refs = available_refs

class WrongKindError(Exception):
    def __init__(self, ref: str, expected: str, actual: str):
        super().__init__(f"Ref '{ref}' is of kind '{actual}', expected '{expected}'")
        self.ref = ref
        self.expected = expected
        self.actual = actual

class ResultStore:
    def __init__(self, request_id: str):
        self.request_id = request_id
        self._store: dict[str, StoredResult] = {}
        logger.debug(f"Created ResultStore for request {request_id}")

    def put(self, kind: str, items: Any, summary: str, sample: list[dict], provenance: dict, count: int, truncated: bool = False) -> ResultHandle:
        ref = f"{kind}:{uuid.uuid4().hex[:8]}"
        
        handle = ResultHandle(
            ref=ref,
            kind=kind,
            count=count,
            sample=sample,
            summary=summary,
            truncated=truncated
        )
        
        stored_items = items.model_dump() if isinstance(items, BaseModel) and hasattr(items, "model_dump") else items

        stored = StoredResult(
            handle=handle,
            items=stored_items,
            provenance=provenance
        )
        
        self._store[ref] = stored
        logger.debug(f"[{self.request_id}] Stored result {ref} (kind={kind}, count={count})")
        return handle

    def get(self, ref: str, expected_kind: str) -> StoredResult:
        if ref not in self._store:
            logger.warning(f"[{self.request_id}] get() failed: unknown ref {ref}")
            raise UnknownRefError(ref, self.available_refs())
            
        stored = self._store[ref]
        if stored.handle.kind != expected_kind:
            logger.warning(f"[{self.request_id}] get() failed: wrong kind for {ref} (expected {expected_kind}, got {stored.handle.kind})")
            raise WrongKindError(ref, expected_kind, stored.handle.kind)
            
        logger.debug(f"[{self.request_id}] Retrieved result {ref}")
        return stored

    def refs(self) -> list[ResultHandle]:
        return [stored.handle for stored in self._store.values()]

    def available_refs(self) -> list[dict]:
        return [{"ref": h.ref, "kind": h.kind, "count": h.count} for h in self.refs()]

    def set_artifact_id(self, ref: str, artifact_id: str) -> None:
        if ref in self._store:
            self._store[ref].artifact_id = artifact_id
            logger.debug(f"[{self.request_id}] Set artifact_id {artifact_id} for {ref}")
