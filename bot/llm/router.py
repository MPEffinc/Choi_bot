"""Static task routing. No Discord, output control, or retry policy here."""
from .contracts import LLMRequest, LLMResponse, Provider, TaskPolicy


def legacy_policies(model: str) -> dict[str, TaskPolicy]:
    # Retain which original call sites used conf_next(), not a new key policy.
    return {
        task: TaskPolicy("gemini", model, advance)
        for task, advance in {
            "chat": True, "question": False, "info": False, "detail": False,
            "translation": True, "search_map": True, "search_reduce": True,
            "summary_map": True, "summary_reduce": True,
            "menu_candidates": False, "menu_select": False,
        }.items()
    }


class LLMRouter:
    def __init__(self, providers: dict[str, Provider], policies: dict[str, TaskPolicy]):
        self.providers = dict(providers)
        self.policies = dict(policies)

    async def generate(self, request: LLMRequest) -> LLMResponse:
        policy = self.policies[request.task_type]
        return await self.providers[policy.provider].generate(request, policy)
