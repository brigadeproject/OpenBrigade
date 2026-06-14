from __future__ import annotations

from brigade.schemas import Role, User

ROLE_PERMISSIONS: dict[Role, set[str]] = {
    Role.OWNER: {
        "admin",
        "agent:write",
        "auth:write",
        "chat:read",
        "chat:write",
        "export:read",
        "goal:read",
        "goal:write",
        "health:read",
        "knowledge:read",
        "knowledge:write",
        "memory:write",
        "mission:read",
        "mission:write",
        "orchestrator:write",
        "proposal:read",
        "proposal:write",
        "status:read",
        "task:read",
        "task:write",
        "team:read",
        "team:write",
        "user:read",
        "user:write",
    },
    Role.OPERATOR: {
        "agent:read",
        "chat:read",
        "chat:write",
        "export:read",
        "goal:read",
        "health:read",
        "knowledge:read",
        "knowledge:write",
        "memory:write",
        "mission:read",
        "proposal:read",
        "proposal:write",
        "status:read",
        "task:read",
        "task:write",
        "team:read",
        "user:read",
    },
    Role.OBSERVER: {
        "chat:read",
        "goal:read",
        "health:read",
        "knowledge:read",
        "mission:read",
        "proposal:read",
        "status:read",
        "task:read",
        "team:read",
        "user:read",
    },
}


def can(user: User, permission: str) -> bool:
    permissions = ROLE_PERMISSIONS[user.role]
    return permission in permissions or "admin" in permissions
