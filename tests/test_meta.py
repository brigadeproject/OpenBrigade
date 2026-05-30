from __future__ import annotations

from brigade.meta import evaluate_assignment_alignment
from brigade.schemas import Assignment, Goal


def test_meta_interrupts_goal_boundary_violation():
    assignment = Assignment(
        assignment="Spam users with unsupported financial claims",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )
    goal = Goal(
        statement="Find sustainable revenue",
        success_criteria=["one validated experiment"],
        explicitly_not=["spam users", "unsupported financial claims"],
        set_by="human",
        human_confirmed=True,
    )

    decision = evaluate_assignment_alignment(assignment, [goal])

    assert decision.action == "interrupt"
    assert decision.goal_alignment == "misaligned"


def test_meta_allows_confirmed_goal_work():
    assignment = Assignment(
        assignment="Estimate operating cost",
        assigned_to="abacus",
        created_by="human",
        source="direct_command",
    )
    goal = Goal(
        statement="Find sustainable revenue",
        success_criteria=["one validated experiment"],
        explicitly_not=["unsupported financial claims"],
        set_by="human",
        human_confirmed=True,
    )

    decision = evaluate_assignment_alignment(assignment, [goal])

    assert decision.action == "work"
