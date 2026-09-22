from types import SimpleNamespace
from unittest.mock import AsyncMock

from bot.llm.contracts import LLMResponse


class FakeProvider:
    def __init__(self, *results):
        self.results = list(results)
        self.requests = []

    async def generate(self, request, policy):
        self.requests.append((request, policy))
        result = self.results.pop(0) if self.results else '응답'
        if isinstance(result, Exception):
            raise result
        return LLMResponse(result, policy.provider, policy.model)


class FakeMessage:
    def __init__(self):
        self.edit = AsyncMock()
        self.delete = AsyncMock()
        self.jump_url = 'https://example.invalid/message'
        self.create_thread = AsyncMock(return_value=SimpleNamespace(
            send=AsyncMock(), mention='thread'))


class FakeChannel:
    def __init__(self, channel_id=0):
        self.id = channel_id
        self.sent = []
        self.send = AsyncMock(side_effect=self._send)

    async def _send(self, content, **kwargs):
        message = FakeMessage()
        self.sent.append((content, kwargs, message))
        return message


class FakeResponse:
    def __init__(self, channel):
        self.done = False
        self.channel = channel
        self.send_message = AsyncMock(side_effect=self._send)
        self.defer = AsyncMock(side_effect=self._defer)

    def is_done(self):
        return self.done

    async def _send(self, content, **kwargs):
        self.done = True
        return await self.channel.send(content, **kwargs)

    async def _defer(self, **kwargs):
        self.done = True


class FakeInteraction:
    def __init__(self):
        self.channel = FakeChannel()
        self.response = FakeResponse(self.channel)
        self.edit_original_response = AsyncMock()
        self.original_response = AsyncMock(return_value=FakeMessage())
        self.followup = SimpleNamespace(send=AsyncMock())
        self.user = SimpleNamespace(mention='@tester', add_roles=AsyncMock(), remove_roles=AsyncMock())
        self.guild = SimpleNamespace(get_role=lambda role_id: SimpleNamespace(id=role_id, name='알림'))


class FakeClient:
    def __init__(self, **kwargs):
        self.user = object()
        self.events = {}
        self.announcement = FakeChannel()
        self.run = lambda token: None
        self.intents = kwargs.get('intents')

    def event(self, callback):
        self.events[callback.__name__] = callback

    def get_channel(self, channel_id):
        return self.announcement


class FakeTree:
    def __init__(self, client):
        self.commands = []

    def error(self, callback):
        self.on_error = callback

    def add_command(self, command):
        self.commands.append(command)
