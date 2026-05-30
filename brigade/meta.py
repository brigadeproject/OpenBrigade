from __future__ import annotations

from dataclasses import dataclass

from brigade.schemas import Assignment, Goal


@dataclass(frozen=True)
class MetaDecision:
    action: str
    goal_alignment: str
    confidence: float
    rationale: str

    def to_dict(self) -> dict[str, str | float]:
        return self.__dict__.copy()


def evaluate_assignment_alignment(
    assignment: Assignment,
    goals: list[Goal],
    confidence_threshold: float = 0.5,
) -> MetaDecision:
    if not goals:
        return MetaDecision(
            action="interrupt",
            goal_alignment="misaligned",
            confidence=0.0,
            rationale="No goal exists for assigned agent.",
        )

    assignment_text = assignment.assignment.lower()
    for goal in goals:
        forbidden = [item.lower() for item in goal.explicitly_not]
        if any(item and item in assignment_text for item in forbidden):
            return MetaDecision(
                action="interrupt",
                goal_alignment="misaligned",
                confidence=1.0,
                rationale="Assignment overlaps a goal explicitly_not boundary.",
            )

    confidence = 0.75 if any(goal.human_confirmed for goal in goals) else 0.45
    action = "work" if confidence >= confidence_threshold else "interrupt"
    alignment = "aligned" if action == "work" else "drifting"
    return MetaDecision(
        action=action,
        goal_alignment=alignment,
        confidence=confidence,
        rationale="Deterministic goal check completed.",
    )
