"""google-genai adapter: one native async attempt, explicit client per key."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import asyncio
import math
import time

from .contracts import LLMError, LLMResponse


@dataclass
class KeyState:
    key_id: str
    client: object = field(repr=False)
    group: str = 'unknown'
    disabled: bool = False
    attempts: int = 0
    inflight: int = 0
    reservations: int = 0
    usage: dict = field(default_factory=dict)


@dataclass
class QuotaGroup:
    cooldown_until: float = 0
    blocked: bool = False
    reason: str = 'quota_unknown'


def _error(kind, retryable=False, code=None, retry_after=None):
    # Never include SDK exception strings, URLs, response bodies or API keys.
    return LLMError(f'Gemini 요청 실패: {kind}', error_type=kind, retryable=retryable,
                    provider='gemini', status_code=code, retry_after=retry_after)


def _delay(value):
    try:
        result = float(str(value).removesuffix('s'))
        return max(0, result) if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def normalize_error(error):
    import httpx
    from google.genai import errors
    if isinstance(error, LLMError):
        return error
    if isinstance(error, (asyncio.TimeoutError, httpx.TimeoutException)):
        return _error('timeout', True)
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError, ConnectionError)):
        return _error('network', True)
    if not isinstance(error, errors.APIError):
        # aiohttp may be selected by the SDK when installed (discord depends on it).
        try:
            import aiohttp
            if isinstance(error, aiohttp.ClientConnectionError):
                return _error('network', True)
        except ImportError:
            pass
        return _error('invalid_request' if isinstance(error, ValueError) else 'provider_error')
    code = error.code
    data = error.details if isinstance(error.details, dict) else {}
    data = data.get('error', data)
    details = data.get('details', []) if isinstance(data, dict) else []
    details = details if isinstance(details, list) else []
    reasons = {d.get('reason') for d in details if isinstance(d, dict) and isinstance(d.get('reason'), str)}
    delays = []
    headers = getattr(getattr(error, 'response', None), 'headers', {}) or {}
    header = headers.get('retry-after') or headers.get('Retry-After')
    if header:
        seconds = _delay(header)
        if seconds is None:
            try:
                seconds = max(0, (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
        if seconds is not None:
            delays.append(seconds)
    quotas = []
    for detail in details:
        if not isinstance(detail, dict):
            continue
        seconds = _delay(detail.get('retryDelay'))
        if seconds is not None:
            delays.append(seconds)
        for violation in detail.get('violations', []) or []:
            if isinstance(violation, dict):
                quotas.extend(str(violation.get(n, '')).lower() for n in ('quotaId', 'quotaMetric'))
    after = max(delays) if delays else None
    if code == 401 or reasons & {'API_KEY_INVALID', 'API_KEY_EXPIRED'}:
        return _error('authentication', code=code)
    if code == 403:
        return _error('permission', code=code)
    if code == 429:
        if any(any(s in q for s in ('perday', 'per_day', 'daily')) for q in quotas):
            return _error('quota_long', code=code, retry_after=after)
        if after is not None or any(any(s in q for s in ('perminute', 'per_minute')) for q in quotas):
            return _error('rate_limit', True, code, after)
        return _error('quota_unknown', code=code)
    if code in (408, 500, 502, 503, 504):
        return _error('unavailable', True, code, after)
    return _error('invalid_request', code=code)


class GeminiAdapter:
    def __init__(self, api_keys, model, *, key_ids=None, quota_groups=None,
                 client_factory=None, clock=time.monotonic):
        from google import genai
        from google.genai import types
        self.types = types
        self.model = model
        self.clock = clock
        self.keys = []
        self.groups = {}
        self.closed = False
        self._cursor = 0
        factory = client_factory or genai.Client
        key_ids = key_ids or tuple(f'key{i+1}' for i in range(len(api_keys)))
        if len(key_ids) != len(api_keys):
            raise ValueError('Key label count mismatch')
        try:
            for key_id, key in zip(key_ids, api_keys):
                if not key:
                    continue
                client = factory(api_key=key, vertexai=False, http_options=types.HttpOptions(
                    api_version='v1beta', retry_options=types.HttpRetryOptions(attempts=1)))
                group = (quota_groups or {}).get(key_id, 'unknown')
                self.keys.append(KeyState(key_id, client, group))
                self.groups.setdefault((group, model), QuotaGroup())
        except Exception:
            for state in self.keys:
                state.client.close()
            raise _error('configuration') from None
        if not self.keys:
            raise ValueError('Google Generative AI API KEY ERROR!')

    def bind(self, request, policy):
        if self.closed:
            raise _error('closed')
        if (policy.model != self.model or not request.messages
                or any(m.role not in ('user', 'assistant') or not isinstance(m.content, str)
                       for m in request.messages)
                or request.messages[-1].role != 'user'
                or (request.system_instruction is not None
                    and not isinstance(request.system_instruction, str))):
            raise _error('invalid_request')
        try:
            # Preserve sampling/thinking defaults; transport and system have explicit owners.
            options = dict(request.generation_options)
            if any(k in options for k in ('http_options', 'automatic_function_calling',
                                         'system_instruction', 'httpOptions',
                                         'automaticFunctionCalling', 'systemInstruction')):
                raise ValueError('Reserved transport option')
            config = self.types.GenerateContentConfig(
                **options, system_instruction=request.system_instruction)
        except Exception:
            raise _error('invalid_request') from None
        healthy = [k for k in self.keys if not k.disabled and not self.groups[(k.group, self.model)].blocked]
        if not healthy:
            if any(not k.disabled for k in self.keys):
                raise _error('quota_long')
            raise _error('authentication')
        # Select once per logical request. Actual attempts/usage are counted below.
        ordered = self.keys[self._cursor:] + self.keys[:self._cursor]
        key = min(healthy, key=lambda k: (max(0, self.groups[(k.group, self.model)].cooldown_until - self.clock()),
                                         k.attempts + k.reservations, ordered.index(k)))
        self._cursor = (self.keys.index(key) + 1) % len(self.keys)
        key.reservations += 1
        return GeminiRequest(self, key, config)

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        async def close(key):
            try:
                await key.client.aio.aclose()
            finally:
                key.client.close()
        results = await asyncio.gather(*(close(key) for key in self.keys), return_exceptions=True)
        if any(isinstance(result, Exception) for result in results):
            raise _error('close_failed') from None


class GeminiRequest:
    def __init__(self, adapter, key, config):
        self.adapter, self.key, self.config = adapter, key, config
        self.key_id, self.quota_group = key.key_id, key.group
        self.released = False

    def release(self):
        if not self.released:
            self.key.reservations -= 1
            self.released = True

    def ready_delay(self):
        if self.adapter.closed:
            raise _error('closed')
        if self.key.disabled:
            raise _error('authentication')
        group = self.adapter.groups[(self.quota_group, self.adapter.model)]
        if group.blocked:
            raise _error(group.reason)
        return max(0, group.cooldown_until - self.adapter.clock())

    async def generate(self, request, policy, timeout):
        types = self.adapter.types
        config = self.config.model_copy(deep=True)
        config.http_options = types.HttpOptions(timeout=max(1, int(timeout * 1000)),
                                                retry_options=types.HttpRetryOptions(attempts=1))
        config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        self.key.attempts += 1
        self.key.inflight += 1
        start = self.adapter.clock()
        try:
            # Legacy non-character tasks keep their exact single-string contents.
            contents = (request.messages[0].content
                        if len(request.messages) == 1 and request.system_instruction is None
                        else [types.Content(role='model' if m.role == 'assistant' else 'user',
                                            parts=[types.Part(text=m.content)])
                              for m in request.messages])
            response = await self.key.client.aio.models.generate_content(
                model=policy.model, contents=contents, config=config)
            usage = {}
            metadata = response.usage_metadata
            for name in ('prompt_token_count', 'candidates_token_count', 'total_token_count',
                         'cached_content_token_count', 'thoughts_token_count'):
                value = getattr(metadata, name, None)
                if isinstance(value, int):
                    usage[name] = value
                    self.key.usage[name] = self.key.usage.get(name, 0) + value
            feedback = response.prompt_feedback
            block = getattr(feedback, 'block_reason', None)
            if block and str(getattr(block, 'value', block)) not in ('BLOCKED_REASON_UNSPECIFIED', '0'):
                raise _error('blocked')
            candidates = response.candidates or []
            if not candidates:
                raise _error('empty_response')
            reason = getattr(candidates[0].finish_reason, 'value', candidates[0].finish_reason)
            if reason in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'IMAGE_SAFETY', 'IMAGE_PROHIBITED_CONTENT'):
                raise _error('blocked')
            if reason not in (None, 'STOP', 'MAX_TOKENS'):
                raise _error('invalid_response')
            content = candidates[0].content
            # Only visible text; never disclose thought parts or stringified tool calls.
            parts = content.parts if content else []
            text = ''.join(part.text for part in parts or [] if part.text and not part.thought)
            if not text:
                raise _error('empty_response')
            return LLMResponse(text, 'gemini', policy.model, usage, self.adapter.clock() - start,
                               reason, model_version=response.model_version)
        except asyncio.CancelledError:
            raise
        except Exception as raw:
            error = normalize_error(raw)
            group = self.adapter.groups[(self.quota_group, policy.model)]
            if error.error_type == 'authentication':
                self.key.disabled = True
            elif error.error_type == 'quota_long':
                group.reason = error.error_type
                if error.retry_after is None:
                    group.blocked = True  # Unknown reset time: explicit restart/reconfiguration.
                else:
                    group.cooldown_until = max(group.cooldown_until, self.adapter.clock() + error.retry_after)
            elif error.error_type in ('rate_limit', 'quota_unknown'):
                # Unknown 429 is not retried; 60s is an operational cooldown, NOT an API limit.
                cooldown = error.retry_after if error.retry_after is not None else (
                    60 if error.error_type == 'quota_unknown' else policy.backoff)
                group.cooldown_until = max(group.cooldown_until, self.adapter.clock() + cooldown)
            raise error from None
        finally:
            self.key.inflight -= 1
