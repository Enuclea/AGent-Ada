from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

class FactEntry(BaseModel):
    id: Optional[int] = None
    fact: str
    timestamp: Optional[datetime] = None

class KeyValueEntry(BaseModel):
    key: str
    value: Any

class TelemetryRecord(BaseModel):
    session_id: str
    model_name: str
    input_tokens: int
    output_tokens: int
    cost: float
    timestamp: datetime

class PlanStep(BaseModel):
    id: str
    plan_id: str
    step_order: int
    description: str
    status: str
    assigned_tool: Optional[str] = None
    assigned_args: Optional[str] = None
    error_message: Optional[str] = None

class SessionPlan(BaseModel):
    id: str
    session_id: str
    title: str
    status: str
    created_at: datetime
    steps: List[PlanStep] = []

class ScheduledTask(BaseModel):
    id: str
    name: str
    prompt: str
    cron_expr: str
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    status: str

class AgentConfig(BaseModel):
    model: str = "gemini-3.5-flash"
    system_instructions: Optional[str] = None
    disable_tools: bool = False
    roleplay: bool = False
    session_id: Optional[str] = None
    workspaces: List[str] = Field(default_factory=list)
    auto_approve: bool = False

class ChatRequest(BaseModel):
    prompt: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    system_instructions: Optional[str] = None
    disable_tools: Optional[bool] = False
    roleplay: Optional[bool] = False

class SkillInfo(BaseModel):
    name: str
    description: str
    path: str
    instructions: Optional[str] = None
    author: Optional[str] = None
    version: Optional[str] = None
