"""FIFO execution for the existing ONE shared group context, not all commands."""
import asyncio
from dataclasses import dataclass
import time


@dataclass
class Pending:
    message: object
    epoch: int
    called: bool
    received: float
    done: asyncio.Future


class ConversationQueue:
    def __init__(self, process, *, max_pending=32, max_age=120, clock=time.monotonic):
        self.process = process
        self.queue = asyncio.Queue(maxsize=max_pending)
        self.max_age, self.clock = max_age, clock
        self.epoch = 0
        self.reset_kind = None
        self.worker = None
        self.processing = None
        self.closed = False

    def reset(self, kind):
        self.epoch += 1
        self.reset_kind = kind
        # /stop and expiry don't wait for generation. A natural end is called by
        # the processing task itself and must not cancel its own final send.
        if self.processing and self.processing is not asyncio.current_task():
            self.processing.cancel()

    def valid(self, epoch):
        return not self.closed and epoch == self.epoch

    async def submit(self, message, called):
        if self.closed:
            return
        done = asyncio.get_running_loop().create_future()
        self.queue.put_nowait(Pending(message, self.epoch, called, self.clock(), done))
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._run())
        await asyncio.shield(done)

    async def _run(self):
        while not self.queue.empty() and not self.closed:
            item = self.queue.get_nowait()
            try:
                if item.epoch != self.epoch:
                    # Only explicit calls already waiting at natural termination
                    # may start the next conversation. /stop invalidates all old input.
                    if not (self.reset_kind == 'natural' and item.called and item.epoch == self.epoch - 1):
                        continue
                if self.clock() - item.received >= self.max_age:
                    continue
                self.processing = asyncio.create_task(self.process(item.message, self.epoch))
                try:
                    await self.processing
                except asyncio.CancelledError:
                    if self.closed:
                        return
                except Exception as error:
                    if not item.done.done():
                        item.done.set_exception(error)
            finally:
                self.processing = None
                if not item.done.done():
                    item.done.set_result(None)
                self.queue.task_done()

    async def aclose(self):
        self.closed = True
        self.reset('shutdown')
        if self.worker:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            item = self.queue.get_nowait()
            if not item.done.done():
                item.done.set_result(None)
            self.queue.task_done()
