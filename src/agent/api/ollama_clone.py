import time
from datetime import datetime, timezone
from fastapi import HTTPException, Depends, Header, Query
from pydantic import BaseModel
from typing import List, Optional

from agent.api.router import app
from agent.core.routing import routing_engine

class OllamaChatMessage(BaseModel):
    role: str
    content: str

class OllamaChatRequest(BaseModel):
    model: str
    messages: List[OllamaChatMessage]
    stream: Optional[bool] = False

class OllamaGenerateRequest(BaseModel):
    model: str
    prompt: str
    system: Optional[str] = None
    stream: Optional[bool] = False

REVIEW_SYSTEM_PROMPT = (
    "You are a neutral code analysis engine. Analyze the given code or inputs strictly "
    "without performing any external tool calls, task executions, or persona-based formatting."
)

def verify_sandbox_review_mode(
    x_ada_mode: Optional[str] = Header(None, alias="X-Ada-Mode"),
    mode: Optional[str] = Query(None)
):
    """Enforces that the caller explicitly requests review mode."""
    if x_ada_mode != "sandbox-review" and mode != "review":
        raise HTTPException(
            status_code=400,
            detail="Header 'X-Ada-Mode: sandbox-review' or query parameter 'mode=review' is required."
        )

# Register routes for both /api/ollama/api/chat and /api/ollama/chat formats
@app.post("/api/ollama/api/chat")
@app.post("/api/ollama/chat")
async def ollama_chat_endpoint(
    req: OllamaChatRequest,
    _ = Depends(verify_sandbox_review_mode)
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")
    
    # Strictly chat-only, zero tool capability, zero persona
    # Format messages and prepend the neutral analysis system prompt
    # Ignore caller system prompts to prevent prompt injection overriding review instructions
    prompt_parts = []
    
    for msg in req.messages:
        role = msg.role.strip().lower()
        content = msg.content
        if role == "system":
            # Strip system messages to prevent system prompt injection
            continue
        elif role == "user":
            prompt_parts.append(f"User: {content}")
        elif role in ("assistant", "model"):
            prompt_parts.append(f"Assistant: {content}")
            
    system_instructions = REVIEW_SYSTEM_PROMPT
    prompt = "\n".join(prompt_parts)
    
    # Model validation / allowlist enforcement
    allowed_models = {"gemini-2.5-flash", "llama3", "gemma", "ollama/gemma", "ollama/llama3"}
    model_name = req.model
    if model_name not in allowed_models:
        model_name = "gemini-2.5-flash"
    elif model_name.startswith("ollama/"):
        model_name = model_name[7:]
    
    try:
        response_text = await routing_engine.execute(
            prompt=prompt,
            model=model_name,
            system_instructions=system_instructions,
            disable_agy=True
        )
    except Exception:
        try:
            response_text = await routing_engine.execute(
                prompt=prompt,
                model="gemini-2.5-flash",
                system_instructions=system_instructions,
                disable_agy=True
            )
        except Exception as e_fallback:
            raise HTTPException(status_code=500, detail=f"LLM routing failed: {e_fallback}")
            
    return {
        "model": req.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": {
            "role": "assistant",
            "content": response_text
        },
        "done": True
    }

# Register routes for both /api/ollama/api/generate and /api/ollama/generate formats
@app.post("/api/ollama/api/generate")
@app.post("/api/ollama/generate")
async def ollama_generate_endpoint(
    req: OllamaGenerateRequest,
    _ = Depends(verify_sandbox_review_mode)
):
    if not req.prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    # Model validation / allowlist enforcement
    allowed_models = {"gemini-2.5-flash", "llama3", "gemma", "ollama/gemma", "ollama/llama3"}
    model_name = req.model
    if model_name not in allowed_models:
        model_name = "gemini-2.5-flash"
    elif model_name.startswith("ollama/"):
        model_name = model_name[7:]
        
    system_instructions = REVIEW_SYSTEM_PROMPT
        
    try:
        response_text = await routing_engine.execute(
            prompt=req.prompt,
            model=model_name,
            system_instructions=system_instructions,
            disable_agy=True
        )
    except Exception:
        try:
            response_text = await routing_engine.execute(
                prompt=req.prompt,
                model="gemini-2.5-flash",
                system_instructions=system_instructions,
                disable_agy=True
            )
        except Exception as e_fallback:
            raise HTTPException(status_code=500, detail=f"LLM routing failed: {e_fallback}")
            
    return {
        "model": req.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "response": response_text,
        "done": True
    }
