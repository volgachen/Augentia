from fastapi import APIRouter
from pydantic import BaseModel

import app.adapters.tools  # noqa: F401 — triggers tool registration
from app.adapters.tools.registry import list_entries

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolInfo(BaseModel):
    name: str
    description: str
    source: str  # "builtin" | "plugin"
    plugin_id: str | None = None
    plugin_name: str | None = None
    requires_approval: bool


@router.get("", response_model=list[ToolInfo])
async def list_tools():
    """Every tool an agent can select: built-in tools, then tools of loaded plugins.

    Tools of disabled or broken plugins are not listed; GET /plugins says why.
    The agent panel shows tools an agent selected that are missing here as
    unavailable.
    """
    return [
        ToolInfo(
            name=e.name,
            description=e.description,
            source=e.source,
            plugin_id=e.plugin_id,
            plugin_name=e.plugin_name,
            requires_approval=e.requires_approval,
        )
        for e in list_entries()
    ]
