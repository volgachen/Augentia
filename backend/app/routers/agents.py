from typing import List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app.db.interface import IAgentDatabase
from app.db.deps import get_db
from app.models.domain import AgentTemplate, AgentType
import app.adapters.tools  # noqa: F401 — triggers tool registration
from app.adapters.tools.registry import list_available
from app.adapters.tools.permissions import validate_tool_permissions_config
from app.adapters.model_params import resolve_model_params

router = APIRouter(prefix="/agents", tags=["agents"])


class CreateAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    agent_type: AgentType
    system_prompt: str = ""
    tool_names: list[str] = Field(default_factory=list)
    config: dict = Field(default_factory=dict)
    openai_model: str = "gpt-4o"
    openai_base_url: str | None = None


class UpdateAgentRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    system_prompt: str | None = None
    tool_names: list[str] | None = None
    config: dict | None = None
    openai_model: str | None = None
    openai_base_url: str | None = None


def _validate_tools(names: list[str]) -> None:
    available = list_available()
    unknown = [n for n in names if n not in available]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown tools: {unknown}. Available: {available}",
        )


def _validate_config(config: dict) -> None:
    approvals = config.get("tool_approvals", {})
    if not isinstance(approvals, dict):
        raise HTTPException(
            status_code=400,
            detail="config.tool_approvals must be an object mapping tool name -> bool",
        )
    _validate_tools(list(approvals.keys()))
    bad = [k for k, v in approvals.items() if not isinstance(v, bool)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"config.tool_approvals values must be booleans; got non-bool for: {bad}",
        )
    try:
        validate_tool_permissions_config(config.get("tool_permissions"))
        # model_params / compression_model_params: same check the engines run
        # at session start, surfaced as a 400 when the agent is saved.
        resolve_model_params(config)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("", response_model=List[AgentTemplate])
async def list_agents(db: IAgentDatabase = Depends(get_db)):
    return await db.list_agents()


@router.post("", response_model=AgentTemplate, status_code=201)
async def create_agent(
    body: CreateAgentRequest,
    db: IAgentDatabase = Depends(get_db),
):
    _validate_tools(body.tool_names)
    _validate_config(body.config)
    template = AgentTemplate(**body.model_dump())
    return await db.create_agent(template)


@router.get("/{agent_id}", response_model=AgentTemplate)
async def get_agent(agent_id: str, db: IAgentDatabase = Depends(get_db)):
    try:
        return await db.get_agent(agent_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{agent_id}", response_model=AgentTemplate)
async def update_agent(
    agent_id: str,
    body: UpdateAgentRequest,
    db: IAgentDatabase = Depends(get_db),
):
    try:
        await db.get_agent(agent_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    if body.tool_names is not None:
        _validate_tools(body.tool_names)
    if body.config is not None:
        _validate_config(body.config)

    return await db.update_agent(agent_id, **body.model_dump(exclude_unset=True))


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: str, db: IAgentDatabase = Depends(get_db)):
    try:
        await db.get_agent(agent_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))

    await db.delete_agent(agent_id)
