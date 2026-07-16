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


class SequencedTestProvider:
    """Pops scripted responses in order; records every call it receives so
    tests can assert on prompts and on whether native tools were passed."""

    __test__ = False
    route_type = "test"

    def __init__(
        self,
        responses: list[str],
        *,
        model: str = "test-model",
        supports_native_tools: bool = False,
    ) -> None:
        self.responses = list(responses)
        self.model = model
        self.supports_native_tools = supports_native_tools
        self.calls: list[dict] = []

    def complete(self, prompt: str, tools: list | None = None) -> ModelResponse:
        self.calls.append({"prompt": prompt, "tools": tools})
        if not self.responses:
            raise RuntimeError("SequencedTestProvider ran out of scripted responses")
        text = self.responses.pop(0)
        return ModelResponse(
            text=text,
            input_tokens=len(prompt.split()),
            output_tokens=len(text.split()),
            provider="test",
            model=self.model,
            route_type=self.route_type,
        )
