import json
from pathlib import Path
from typing import Optional
from agent import memory

def schedule_task(
    task_id: str,
    prompt: str,
    cron: Optional[str] = None,
    duration: Optional[int] = None,
) -> str:
    """Schedules a task to run automatically in the background.
    
    You must specify either a cron expression for recurring tasks, or a duration
    in seconds for a one-shot delayed task.
    
    Args:
        task_id: Unique identifier to track the scheduled job.
        prompt: The instruction prompt the background agent should run.
        cron: Standard 5-field cron expression (e.g. '*/5 * * * *' for every 5 mins).
        duration: Delayed execution in seconds (max 900). Mutually exclusive with cron.
    """
    if not cron and duration is None:
        return "Error: You must specify either a cron expression or a duration in seconds."
    if cron and duration is not None:
        return "Error: Cron expression and duration are mutually exclusive."
        
    try:
        if cron:
            t_id = memory.add_scheduled_task(task_id, prompt, cron_expression=cron)
            cron_msg = f"recurring cron schedule '{cron}'"
        else:
            t_id = memory.add_scheduled_task(task_id, prompt, duration_seconds=duration)
            cron_msg = f"one-shot timer in {duration} seconds"
            
        return f"Successfully scheduled task `{t_id}` with {cron_msg}."
    except Exception as e:
        return f"Error scheduling task: {e}"

def list_scheduled_tasks() -> str:
    """Lists all active cron jobs and scheduled timers currently registered in the database."""
    try:
        tasks = memory.get_scheduled_tasks()
        if not tasks:
            return "No scheduled tasks found."
            
        lines = ["Active scheduled tasks:"]
        for t in tasks:
            schedule_type = "cron" if t.get("cron_expr") else "one-shot"
            schedule_val = t.get("cron_expr") or f"{t.get('duration')}s"
            lines.append(
                f"- `{t['id']}` ({schedule_type}: {schedule_val}):\n"
                f"  Prompt: \"{t['name']}\"\n"
                f"  Next execution: {t['next_run']}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Error listing scheduled tasks: {e}"

def delete_scheduled_task(task_id: str) -> str:
    """Deletes/cancels a scheduled task or cron job from the database using its ID.
    
    Args:
        task_id: The ID of the task to delete.
    """
    try:
        memory.delete_scheduled_task(task_id)
        return f"Successfully deleted scheduled task `{task_id}`."
    except Exception as e:
        return f"Error deleting scheduled task: {e}"

def checkpoint_task(
    task_name: str,
    phase: str,
    step_completed: int,
    state: str,
    total_steps: Optional[int] = None
) -> str:
    """Save a progress checkpoint for a long-running task.
    
    Call this after completing each significant step of a multi-step task.
    If your session times out or is interrupted, the next session can resume 
    from this checkpoint instead of starting over.
    
    When the task is fully complete, call with phase="completed" to mark the 
    checkpoint as done.
    
    Args:
        task_name: Descriptive identifier for the task (e.g., "setup_gmail_pubsub", 
                   "refactor_memory_module"). Use consistent names across sessions.
        phase: Current phase label (e.g., "topic_created", "subscription_configured").
               Use "completed" to mark the task as finished.
        step_completed: The step number just completed (1-indexed).
        state: JSON string with any state needed to resume. Include resource names, 
               file paths, API responses, configuration values — anything a future 
               session would need to pick up where you left off.
        total_steps: Total expected steps, if known. Helps estimate remaining work.
    
    Returns:
        JSON confirmation with the checkpoint ID.
    """
    from agent.core.task_manager import save_checkpoint, complete_checkpoint
    
    if phase == "completed":
        success = complete_checkpoint(task_name)
        return json.dumps({
            "status": "completed",
            "task_name": task_name,
            "message": "Task checkpoint marked as completed." if success else "No active checkpoint found for this task."
        })
    
    # Validate state is valid JSON
    try:
        json.loads(state)
    except (json.JSONDecodeError, TypeError):
        state = json.dumps({"raw": state})
    
    checkpoint_id = save_checkpoint(
        task_name=task_name,
        session_id="",  # Will be set by the system if available
        phase=phase,
        step_completed=step_completed,
        state_json=state,
        total_steps=total_steps
    )
    
    return json.dumps({
        "status": "saved",
        "checkpoint_id": checkpoint_id,
        "task_name": task_name,
        "phase": phase,
        "step_completed": step_completed,
        "total_steps": total_steps,
        "message": f"Checkpoint saved. If this session ends, the next session can resume from step {step_completed + 1}."
    })

def get_task_checkpoint(task_name: str) -> str:
    """Check if a previous checkpoint exists for a task.
    
    Call this BEFORE starting a multi-step task to check if a previous
    attempt was interrupted and can be resumed.
    
    Args:
        task_name: The task identifier to look up (e.g., "setup_gmail_pubsub").
    
    Returns:
        JSON with checkpoint data if an in-progress checkpoint exists,
        or {"status": "none"} if no resumable checkpoint is found.
    """
    from agent.core.task_manager import get_checkpoint
    
    checkpoint = get_checkpoint(task_name)
    if not checkpoint:
        return json.dumps({
            "status": "none",
            "task_name": task_name,
            "message": "No resumable checkpoint found. Start the task from the beginning."
        })
    
    return json.dumps({
        "status": "found",
        "task_name": checkpoint["task_name"],
        "phase": checkpoint["phase"],
        "step_completed": checkpoint["step_completed"],
        "total_steps": checkpoint["total_steps"],
        "state": checkpoint["state_json"],
        "created_at": checkpoint["created_at"],
        "updated_at": checkpoint["updated_at"],
        "message": f"Resumable checkpoint found at step {checkpoint['step_completed']}/{checkpoint['total_steps'] or '?'} (phase: {checkpoint['phase']}). Resume from step {checkpoint['step_completed'] + 1}."
    })
