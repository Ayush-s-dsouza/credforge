"""--explain event plumbing.

Every stage emits ExplainEvent objects through whatever ExplainSink it's
given. By default that's NullExplainSink (a no-op) -- the CLI wiring that
turns --explain into a real, rich-printing sink is Stage 9's job. Defining
the mechanism now, and having every stage call it from the moment that
stage is built, is what makes --explain actually cover every stage instead
of being bolted on retroactively at the end.
"""

from typing import Protocol

from pydantic import BaseModel

from ..enums import PipelineStage


class ExplainEvent(BaseModel):
    stage: PipelineStage
    identity_key: str
    message: str
    detail: dict = {}


class ExplainSink(Protocol):
    def emit(self, event: ExplainEvent) -> None: ...


class NullExplainSink:
    def emit(self, event: ExplainEvent) -> None:
        pass


NULL_EXPLAIN = NullExplainSink()
