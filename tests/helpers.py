from __future__ import annotations

import json

from brigade.providers import ModelResponse


class TestProvider:
    __test__ = False
    route_type = "test"

    def __init__(self, model: str = "test-model", text: str | None = None) -> None:
        self.model = model
        self.text = text

    def complete(self, prompt: str) -> ModelResponse:
        text = self.text
        if text is None:
            if "OpenBrigade orchestrator escalation protocol:" in prompt:
                text = json.dumps(
                    {
                        "status": "no_action",
                        "summary": "test provider: no orchestrator action",
                        "actions": [],
                    }
                )
            elif "OpenBrigade agent response protocol:" in prompt:
                text = json.dumps(
                    {
                        "status": "complete",
                        "summary": f"test provider: {prompt[:120]}",
                        "blockers": [],
                    }
                )
            else:
                text = f"test provider: {prompt[:120]}"
        return ModelResponse(
            text=text,
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )
