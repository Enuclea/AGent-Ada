"""Module containing Pydantic models for agent-related data structures and types.

This module defines the core data contracts used throughout the agent's lifetime,
including telemetry, planning, memory entries, scheduling, and configurations.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class FactEntry(BaseModel):
    """Represents a facts/notes record stored in the agent's memory repository.

    Attributes:
        id: Optional unique identifier for the fact entry.
        fact: The textual fact content to remember.
        timestamp: Optional timestamp indicating when the fact was recorded.
    """
    id: Optional[int] = None
    fact: str
    timestamp: Optional[datetime] = None


class KeyValueEntry(BaseModel):
    """Represents a key-value mapping stored in the agent's persistent metadata/settings memory.

    Attributes:
        key: The lookup key name.
        value: The associated value, of any serializable type.
    """
    key: str
    value: Any


class TelemetryRecord(BaseModel):
    """Represents telemetry, token usage, and cost metrics for a session call.

    Attributes:
        session_id: Unique identifier for the agent session.
        model_name: The name of the LLM used.
        input_tokens: Number of input tokens consumed.
        output_tokens: Number of output tokens generated.
        cost: Calculated financial cost of the execution.
        timestamp: Point in time when the record was captured.
    """
    session_id: str
    model_name: str
    input_tokens: int
    output_tokens: int
    cost: float
    timestamp: datetime


class PlanStep(BaseModel):
    """Represents a single step in a structured session execution plan.

    Attributes:
        id: Unique identifier for the plan step.
        plan_id: Reference identifier to the parent SessionPlan.
        step_order: The ordering sequence number of this step within the plan.
        description: Description of what this step is intended to accomplish.
        status: The current progress status (e.g. pending, completed, failed).
        assigned_tool: The tool assigned to execute this step, if any.
        assigned_args: The arguments passed to the assigned tool, serialized.
        error_message: Optional error message if the step execution failed.
    """
    id: str
    plan_id: str
    step_order: int
    description: str
    status: str
    assigned_tool: Optional[str] = None
    assigned_args: Optional[str] = None
    error_message: Optional[str] = None


class SessionPlan(BaseModel):
    """Represents the complete plan for a conversation session, containing ordered steps.

    Attributes:
        id: Unique identifier for the plan.
        session_id: Reference identifier to the active chat session.
        title: Short title or goal describing the plan.
        status: Overall status of the plan.
        created_at: Creation timestamp.
        steps: Ordered list of PlanStep structures. Defaults to an empty list.
    """
    id: str
    session_id: str
    title: str
    status: str
    created_at: datetime
    steps: List[PlanStep] = []


class ScheduledTask(BaseModel):
    """Represents a scheduled background or recurring task defined via cron expressions.

    Attributes:
        id: Unique identifier for the scheduled task.
        name: A human-readable name for the task.
        prompt: The command or task prompt text to run.
        cron_expr: The 5-field cron expression representing the schedule.
        next_run: Optional string representation of the next execution time.
        last_run: Optional string representation of the last execution time.
        status: Current status of the task schedule (e.g., active, paused).
    """
    id: str
    name: str
    prompt: str
    cron_expr: str
    next_run: Optional[str] = None
    last_run: Optional[str] = None
    status: str


class AgentConfig(BaseModel):
    """Configuration options for setting up and running an agent instance.

    Attributes:
        model: The model name to use, defaults to 'gemini-3.5-flash'.
        system_instructions: Optional instructions to prepend to system prompts.
        disable_tools: Flag to turn off tool executions for safety.
        roleplay: Flag enabling specific roleplay system prompting behavior.
        session_id: Optional session identifier to bind execution to.
        workspaces: List of allowed root workspace directories.
        auto_approve: Flag indicating whether command executions are auto-approved.
    """
    model: str = "gemini-3.5-flash"
    system_instructions: Optional[str] = None
    disable_tools: bool = False
    roleplay: bool = False
    session_id: Optional[str] = None
    workspaces: List[str] = Field(default_factory=list)
    auto_approve: bool = False


class ChatRequest(BaseModel):
    """Payload structure for initiating a chat request with the agent.

    Attributes:
        prompt: The user prompt input.
        session_id: Optional session identifier to continue an existing chat.
        model: Optional LLM model override.
        system_instructions: Optional system instructions override.
        disable_tools: Optional flag to disable tool usage.
        roleplay: Optional flag to toggle roleplay mode.
    """
    prompt: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    system_instructions: Optional[str] = None
    disable_tools: Optional[bool] = False
    roleplay: Optional[bool] = False


class SkillInfo(BaseModel):
    """Metadata and structural details about an installed agent skill.

    Attributes:
        name: Name of the skill.
        description: Description of what the skill does.
        path: Absolute filesystem path to the skill directory or main file.
        instructions: Optional instructions or usage manual for the skill.
        author: Optional author information.
        version: Optional version number.
    """
    name: str
    description: str
    path: str
    instructions: Optional[str] = None
    author: Optional[str] = None
    version: Optional[str] = None
