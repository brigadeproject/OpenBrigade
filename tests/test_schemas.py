from __future__ import annotations

import pytest

from brigade.schemas import Assignment, AssignmentStatus, Goal


def test_goal_requires_explicitly_not():
    with pytest.raises(ValueError, match="explicitly_not"):
        Goal(
            statement="Make enough money to offset costs",
            success_criteria=["monthly revenue exceeds spend"],
            explicitly_not=None,  # type: ignore[arg-type]
            set_by="human",
        )


def test_assignment_rejects_invalid_transition():
    assignment = Assignment(
        assignment="Research a revenue idea",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )

    with pytest.raises(ValueError, match="invalid assignment transition"):
        assignment.transition_to(AssignmentStatus.COMPLETE)


def test_assignment_abandons_after_ten_incomplete_cycles():
    assignment = Assignment(
        assignment="Long-running task",
        assigned_to="sage",
        created_by="orchestrator",
        source="scheduled_cycle",
    )

    for _ in range(10):
        assignment.mark_cycle_incomplete()

    assert assignment.status == AssignmentStatus.ABANDONED
