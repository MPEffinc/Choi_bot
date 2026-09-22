"""Temporary legacy SDK adapter. Global configure/worker races are NOT solved.

The original RR, executor timeout and key-count retries intentionally remain.
Phase 1B will replace this execution policy and the SDK together.
"""
import asyncio
import time

from .contracts import LLMError, LLMRequest, LLMResponse, TaskPolicy


class GeminiAdapter:
    def __init__(self, api_keys: tuple[str, ...], model: str, *, sdk=None):
        if not api_keys:
            raise ValueError("Google Generative AI API KEY ERROR!")
        if sdk is None:
            import google.generativeai as sdk
        from google.api_core.exceptions import ResourceExhausted
        self.sdk = sdk
        self.quota_exception = ResourceExhausted
        self.api_keys = api_keys
        self.model_name = model
        self.current_api_index = 0
        self.call_count = 0
        self._configure()

    def _configure(self):
        self.sdk.configure(api_key=self.api_keys[self.current_api_index])
        self.model = self.sdk.GenerativeModel(self.model_name)

    def rotate_api_key(self):
        self.current_api_index = (self.current_api_index + 1) % len(self.api_keys)
        self._configure()
        print(f"[DEBUG] API 키 회전됨: {self.current_api_index}번")

    def conf_next(self):
        if self.call_count >= 3:
            self.rotate_api_key()
            self.call_count = 0
            return
        self.call_count += 1

    def is_quota_error(self, error):
        message = str(error).lower()
        return isinstance(error, self.quota_exception) or any(
            text in message for text in
            ("429", "quota", "rate limit", "resource has been exhausted")
        )

    def _error(self, error):
        code = getattr(error, "code", None)
        code = int(code) if isinstance(code, int) else None
        if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
            kind, retryable = "timeout", True
        elif isinstance(error, self.quota_exception):
            kind, retryable = "quota", True
        elif self.is_quota_error(error):
            # Same retry eligibility, but retain the legacy summary UI's
            # distinction between ResourceExhausted and string-matched errors.
            kind, retryable = "rate_limit", True
        elif code in (401, 403):
            kind, retryable = "authentication", False
        elif code == 400:
            kind, retryable = "invalid_request", False
        else:
            kind, retryable = "provider_error", False
        return LLMError(str(error), error_type=kind, retryable=retryable,
                        provider="gemini", status_code=code)

    async def generate(self, request: LLMRequest, policy: TaskPolicy) -> LLMResponse:
        # Phase 1A deliberately sends the identical single prompt string.
        if len(request.messages) != 1 or request.messages[0].role != "user":
            raise LLMError("Legacy adapter requires one user prompt", error_type="invalid_request",
                           retryable=False, provider="gemini")
        if policy.model != self.model_name:
            raise LLMError("Unsupported model", error_type="invalid_request",
                           retryable=False, provider="gemini")
        started = time.monotonic()
        try:
            if policy.advance_legacy_key:
                self.conf_next()
            return await self._generate(request, started)
        except Exception as error:
            raise self._error(error) from error

    async def _generate(self, request, started):
        loop = asyncio.get_running_loop()
        max_retry = max(1, len(self.api_keys))
        for attempt in range(max_retry):
            try:
                kwargs = ({"generation_config": dict(request.generation_options)}
                          if request.generation_options else {})
                response = await asyncio.wait_for(
                    loop.run_in_executor(None, lambda: self.model.generate_content(
                        request.messages[0].content, **kwargs)),
                    timeout=request.timeout,
                )
                text = getattr(response, "text", None)
                print(f"[DEBUG] 모델 응답 수신: {text if text is not None else '(text 속성 없음)'}")
                usage_metadata = getattr(response, "usage_metadata", None)
                usage = {name: getattr(usage_metadata, name) for name in
                         ("prompt_token_count", "candidates_token_count", "total_token_count")
                         if isinstance(getattr(usage_metadata, name, None), int)}
                candidates = getattr(response, "candidates", ())
                reason = getattr(candidates[0], "finish_reason", None) if candidates else None
                return LLMResponse(text, "gemini", self.model_name, usage,
                                   time.monotonic() - started,
                                   getattr(reason, "name", str(reason)) if reason is not None else None)
            except asyncio.TimeoutError as error:
                last_error = error
                print(f"[경고] 요청 타임아웃({request.timeout}s). 키 회전 후 재시도: {attempt + 1}/{max_retry}")
            except Exception as error:
                last_error = error
                if not self.is_quota_error(error):
                    raise
                print(f"[경고] 쿼터/레이트 제한 감지. 키 회전 후 재시도: {attempt + 1}/{max_retry} - {error}")
            if attempt < max_retry - 1 and len(self.api_keys) > 1:
                self.rotate_api_key()
                await asyncio.sleep(0.5)
        raise last_error
