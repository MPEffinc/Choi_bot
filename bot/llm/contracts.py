from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class LLMRequest:
    task_type: str
    messages: tuple[Message, ...]
    request_id: str = field(default_factory=lambda: str(uuid4()))
    generation_options: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    timeout: float = 20


@dataclass(frozen=True)
class LLMResponse:
    # None preserves the old handler fallback for an absent text attribute.
    text: str | None
    provider: str
    model: str
    usage: Mapping[str, int] = field(default_factory=dict)
    latency: float = 0
    finish_reason: str | None = None


class LLMError(Exception):
    def __init__(self, message: str, *, error_type: str, retryable: bool,
                 provider: str, status_code: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.provider = provider
        self.status_code = status_code


@dataclass(frozen=True)
class TaskPolicy:
    provider: str
    model: str
    advance_legacy_key: bool = False


class Provider(Protocol):
    async def generate(self, request: LLMRequest, policy: TaskPolicy) -> LLMResponse:
        ...
