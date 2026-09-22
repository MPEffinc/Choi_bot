"""One retry/deadline owner; no Discord or conversation state."""
import asyncio
from collections import deque
from dataclasses import replace
import logging
import math
import time

from .contracts import LLMError, LLMRequest, LLMResponse, Provider, TaskPolicy

logger = logging.getLogger(__name__)


def task_policies(model: str) -> dict[str, TaskPolicy]:
    tasks = ('chat', 'question', 'info', 'detail', 'translation', 'search_map',
             'search_reduce', 'summary_map', 'summary_reduce', 'menu_candidates', 'menu_select')
    return {task: (TaskPolicy('gemini', model, 30, 90, 3) if task.startswith(('search_', 'summary_'))
                   else TaskPolicy('gemini', model)) for task in tasks}


class LLMRouter:
    def __init__(self, providers: dict[str, Provider], policies: dict[str, TaskPolicy],
                 *, max_concurrency=2, clock=time.monotonic, sleep=asyncio.sleep):
        if max_concurrency < 1:
            raise ValueError('max_concurrency must be positive')
        self.providers = dict(providers)
        self.policies = dict(policies)
        self._slots = asyncio.Semaphore(max_concurrency)
        self._clock, self._sleep = clock, sleep
        self._inflight = set()
        self._requests = set()
        self._closed = False
        self.metrics = deque(maxlen=200)  # No prompts, key values or raw exceptions.

    def _error(self, kind, provider):
        return LLMError(f'LLM 요청 {kind}', error_type=kind, retryable=False, provider=provider)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        policy = self.policies[request.task_type]
        if self._closed:
            raise self._error('closed', policy.provider)
        if request.timeout is not None and (request.timeout <= 0 or not math.isfinite(request.timeout)):
            raise self._error('invalid_request', policy.provider)
        current = asyncio.current_task()
        self._requests.add(current)
        start = self._clock()
        end = start + policy.deadline
        attempts = 0
        outcome = 'cancelled'
        bound = None
        usage = {}

        def remaining():
            budget = end - self._clock()
            if budget <= 0:
                raise self._error('deadline', policy.provider)
            return budget

        async def pause(delay):
            if delay >= remaining():
                raise self._error('deadline', policy.provider)
            await self._sleep(delay)
            remaining()

        try:
            bound = self.providers[policy.provider].bind(request, policy)
            while attempts < policy.max_attempts:
                # Also recheck after queueing: another request may have hit quota.
                delay = bound.ready_delay()
                if delay > 0:
                    await pause(delay)
                    continue
                try:
                    await asyncio.wait_for(self._slots.acquire(), remaining())
                except asyncio.TimeoutError:
                    raise self._error('deadline', policy.provider) from None
                transferred = False
                try:
                    delay = bound.ready_delay()
                    if delay > 0:
                        continue
                    budget = min(policy.attempt_timeout, remaining())
                    if request.timeout is not None:
                        budget = min(budget, request.timeout)
                    attempts += 1
                    task = asyncio.create_task(bound.generate(request, policy, budget))
                    self._inflight.add(task)
                    # A cancelled/timed-out transport owns its slot until it really exits.
                    def completed(done):
                        self._inflight.discard(done)
                        self._slots.release()
                        if not done.cancelled():
                            done.exception()  # Consume detached errors, never print secrets.
                    task.add_done_callback(completed)
                    transferred = True
                    try:
                        done, _ = await asyncio.wait({task}, timeout=budget)
                        if not done:
                            task.cancel()
                            await asyncio.sleep(0)
                            raise LLMError('LLM 응답 대기 시간 초과', error_type='timeout',
                                           retryable=task.done(), provider=policy.provider)
                        response = task.result()
                        remaining()  # Never return a result after the logical deadline.
                        usage = dict(response.usage)
                        outcome = 'success'
                        return replace(response, request_id=request.request_id,
                                       attempt_count=attempts, latency=self._clock() - start)
                    except asyncio.CancelledError:
                        task.cancel()
                        raise
                    except LLMError as error:
                        if not error.retryable or attempts >= policy.max_attempts:
                            raise
                        delay = min(policy.max_backoff, policy.backoff * 2 ** (attempts - 1))
                        if error.retry_after is not None:
                            delay = max(delay, error.retry_after)
                finally:
                    if not transferred:
                        self._slots.release()
                # Backoff never owns an execution slot.
                await pause(delay)
            raise self._error('attempts_exhausted', policy.provider)
        except LLMError as error:
            outcome = error.error_type
            error.attempt_count = attempts
            error.request_id = request.request_id
            raise
        finally:
            if bound is not None:
                bound.release()
            self._requests.discard(current)
            metric = dict(request_id=request.request_id, task=request.task_type, model=policy.model,
                          key_id=bound.key_id if bound else None,
                          quota_group=bound.quota_group if bound else None,
                          attempts=attempts, latency=self._clock() - start, outcome=outcome, usage=usage)
            self.metrics.append(metric)
            logger.info('LLM %s', metric)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        tasks = (self._requests | self._inflight) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks, timeout=1)
        await asyncio.gather(*(provider.aclose() for provider in self.providers.values()))
