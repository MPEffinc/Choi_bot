from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
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
    timeout: float | None = None  # Optional per-attempt cap, never extends policy.

    def __post_init__(self):
        object.__setattr__(self, 'messages', tuple(self.messages))
        for name in ('generation_options', 'metadata'):
            object.__setattr__(self, name, MappingProxyType(deepcopy(dict(getattr(self, name)))))


@dataclass(frozen=True)
class LLMResponse:
    text: str | None
    provider: str
    model: str
    usage: Mapping[str, int] = field(default_factory=dict)
    latency: float = 0
    finish_reason: str | None = None
    request_id: str = ''
    attempt_count: int = 0
    model_version: str | None = None


class LLMError(Exception):
    def __init__(self, message: str, *, error_type: str, retryable: bool,
                 provider: str, status_code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable
        self.provider = provider
        self.status_code = status_code
        self.retry_after = retry_after
        self.attempt_count = 0
        self.request_id = ''


@dataclass(frozen=True)
class TaskPolicy:
    provider: str
    model: str
    attempt_timeout: float = 20
    deadline: float = 45
    max_attempts: int = 2
    backoff: float = 0.5
    max_backoff: float = 8

    def __post_init__(self):
        import math
        if (self.max_attempts < 1 or self.attempt_timeout <= 0 or self.deadline <= 0
                or self.backoff < 0 or self.max_backoff < 0
                or not all(math.isfinite(v) for v in (self.attempt_timeout, self.deadline, self.backoff, self.max_backoff))):
            raise ValueError('Invalid LLM execution policy')


class BoundProvider(Protocol):
    key_id: str
    quota_group: str

    def release(self) -> None: ...
    def ready_delay(self) -> float: ...
    async def generate(self, request: LLMRequest, policy: TaskPolicy, timeout: float) -> LLMResponse: ...


class Provider(Protocol):
    def bind(self, request: LLMRequest, policy: TaskPolicy) -> BoundProvider: ...
    async def aclose(self) -> None: ...
