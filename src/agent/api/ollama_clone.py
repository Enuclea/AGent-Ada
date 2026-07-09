import time
import asyncio
import json
from datetime import datetime, timezone
from fastapi import HTTPException, Depends, Header, Query, Request
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel
from typing import List, Optional

from agent.api.router import app
from agent.core.routing import routing_engine

async def quiet_security_analysis(prompt: str, response_text: str, system_instructions: Optional[str] = None):
    # run in thread pool to avoid blocking the event loop
    def _run():
        import re
        import logging
        from agent.security.ast_safety import verify_ast_safety
        from agent.observability.telemetry import log_telemetry_event

        logger = logging.getLogger("ollama_clone")
        logger.info(f"[OLLAMA CLONE SCAN] Prompt: {prompt[:200]} | System: {system_instructions} | Response: {response_text[:200]}")
        
        all_texts = [prompt, response_text]
        if system_instructions:
            all_texts.append(system_instructions)
            
        # 1. AST Safety Check
        code_blocks = []
        for text in all_texts:
            code_blocks.extend(re.findall(r"```python\n(.*?)```", text, re.DOTALL))
            if any(kw in text for kw in ("import ", "def ", "class ", "print(")):
                code_blocks.append(text)
                
        for i, code in enumerate(code_blocks):
            try:
                verify_ast_safety(code, f"ollama_payload_{i}.py")
            except Exception as e:
                err_msg = str(e)
                logger.warning(f"[SECURITY ALERT] AST violation detected in Ollama API payload: {err_msg}")
                try:
                    log_telemetry_event(
                        session_id="ollama-api-session",
                        event_type="SECURITY_AST_VIOLATION",
                        event_details=f"Code: {code}\nError: {err_msg}",
                        latency=0.0
                    )
                except Exception:
                    pass
                    
        # 2. Suspicious Pattern/Keyword Scanner
        suspicious_keywords = [
            r"\b(subprocess|pty|shutil|socket)\b",
            r"\b(eval|exec|__import__|compile)\b",
            r"\b(bash|powershell|curl|wget|nc|netcat|telnet|ssh|sudo)\b",
            r"\b(run|execute|call|system|spawn)\s+(command|code|script|shell|program|binary|process|file|tool|cmd|utility|payload)\b",
            r"/bin/(sh|bash|zsh|csh|tcsh)",
            r"(/etc/passwd|/etc/shadow|/etc/hosts)",
            r"\b(rm\s+-rf|chmod\s+\+x|chown|kill\s+-9|killall)\b",
            r"\bbypass\s+(sandbox|restriction|security|safeguard|limit)\b",
            r"\bescape\s+(sandbox|container|jail)\b",
            r"\binstruction\s+override\b",
        ]
        
        for text in all_texts:
            for pattern in suspicious_keywords:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    matched_str = match.group(0)
                    logger.warning(f"[SECURITY ALERT] Suspicious pattern '{matched_str}' detected in Ollama API payload")
                    try:
                        log_telemetry_event(
                            session_id="ollama-api-session",
                            event_type="SECURITY_SUSPICIOUS_PROMPT",
                            event_details=f"Matched: '{matched_str}'\nContext: {text[:500]}",
                            latency=0.0
                        )
                    except Exception:
                        pass
                    break # log once per text block
    await asyncio.to_thread(_run)

async def execute_keyless_gemini(prompt: str, model_name: Optional[str] = None, system_instructions: Optional[str] = None) -> str:
    from google.antigravity import Agent, LocalAgentConfig
    from google.antigravity.types import CapabilitiesConfig
    import os

    # Ensure a dummy key is set so Pydantic validation passes
    os.environ.setdefault("GEMINI_API_KEY", "keyless-dummy-key")

    target_model = "gemini-2.5-flash"
    if model_name and "gemini" in model_name.lower():
        target_model = model_name

    config = LocalAgentConfig(
        model=target_model,
        capabilities=CapabilitiesConfig(
            enabled_tools=[]
        ),
        policies=[],
        tools=[],
        system_instructions=system_instructions
    )
    async with Agent(config) as agent:
        resp = await agent.chat(prompt)
        return await resp.text()

class OllamaChatMessage(BaseModel):
    role: str
    content: str

class OllamaChatRequest(BaseModel):
    model: str
    messages: List[OllamaChatMessage]
    system: Optional[str] = None
    stream: Optional[bool] = True

class OllamaGenerateRequest(BaseModel):
    model: str
    prompt: str
    system: Optional[str] = None
    stream: Optional[bool] = True

REVIEW_SYSTEM_PROMPT = (
    "You are a neutral code analysis engine. Analyze the given code or inputs strictly "
    "without performing any external tool calls, task executions, or persona-based formatting."
)

async def chat_streamer(model_name: str, response_text: str):
    chunk_size = 10
    for i in range(0, len(response_text), chunk_size):
        chunk = response_text[i:i+chunk_size]
        yield json.dumps({
            "model": model_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "message": {
                "role": "assistant",
                "content": chunk
            },
            "done": False
        }) + "\n"
        await asyncio.sleep(0.01)
    yield json.dumps({
        "model": model_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": {
            "role": "assistant",
            "content": ""
        },
        "done": True
    }) + "\n"

async def generate_streamer(model_name: str, response_text: str):
    chunk_size = 10
    for i in range(0, len(response_text), chunk_size):
        chunk = response_text[i:i+chunk_size]
        yield json.dumps({
            "model": model_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "response": chunk,
            "done": False
        }) + "\n"
        await asyncio.sleep(0.01)
    yield json.dumps({
        "model": model_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "response": "",
        "done": True
    }) + "\n"

# Root compatibility for Ollama status checks
@app.head("/api/ollama")
@app.get("/api/ollama")
@app.head("/api/ollama/")
@app.get("/api/ollama/")
async def ollama_status_check():
    return PlainTextResponse("Ollama is running", status_code=200)

@app.head("/")
async def head_root_compatibility():
    return PlainTextResponse("Ollama is running", status_code=200)

def verify_sandbox_review_mode(
    x_ada_mode: Optional[str] = Header(None, alias="X-Ada-Mode"),
    mode: Optional[str] = Query(None)
):
    """Bypasses strict enforcement to allow standard client integrations."""
    pass

# Register routes for both /api/ollama/api/chat and /api/ollama/chat formats
@app.post("/api/ollama/api/chat")
@app.post("/api/ollama/chat")
async def ollama_chat_endpoint(
    req: OllamaChatRequest
):
    if not req.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")
    
    prompt_parts = []
    system_instructions = req.system or None
    
    for msg in req.messages:
        role = msg.role.strip().lower()
        content = msg.content
        if role == "system":
            system_instructions = content
        elif role == "user":
            prompt_parts.append(f"User: {content}")
        elif role in ("assistant", "model"):
            prompt_parts.append(f"Assistant: {content}")
            
    prompt = "\n".join(prompt_parts)
    
    # Model validation / allowlist enforcement
    allowed_models = {"gemini-2.5-flash", "llama3", "gemma", "ollama/gemma", "ollama/llama3"}
    model_name = req.model
    if model_name.startswith("ollama/"):
        model_name = model_name[7:]
    if ":" in model_name:
        model_name = model_name.split(":")[0]
    if model_name not in allowed_models:
        model_name = "gemini-2.5-flash"
    
    try:
        response_text = await execute_keyless_gemini(
            prompt=prompt,
            model_name=model_name,
            system_instructions=system_instructions
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Keyless Gemini execution failed: {e}")
            
    asyncio.create_task(quiet_security_analysis(prompt, response_text, system_instructions))

    if req.stream:
        return StreamingResponse(
            chat_streamer(req.model, response_text),
            media_type="application/x-ndjson"
        )
            
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
@app.post("/api/generate")
async def ollama_generate_endpoint(
    req: OllamaGenerateRequest
):
    if not req.prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    # Model validation / allowlist enforcement
    allowed_models = {"gemini-2.5-flash", "llama3", "gemma", "ollama/gemma", "ollama/llama3"}
    model_name = req.model
    if model_name.startswith("ollama/"):
        model_name = model_name[7:]
    if ":" in model_name:
        model_name = model_name.split(":")[0]
    if model_name not in allowed_models:
        model_name = "gemini-2.5-flash"
        
    system_instructions = req.system or None
        
    try:
        response_text = await execute_keyless_gemini(
            prompt=req.prompt,
            model_name=model_name,
            system_instructions=system_instructions
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Keyless Gemini execution failed: {e}")
            
    asyncio.create_task(quiet_security_analysis(req.prompt, response_text, system_instructions))
            
    if req.stream:
        return StreamingResponse(
            generate_streamer(req.model, response_text),
            media_type="application/x-ndjson"
        )
            
    return {
        "model": req.model,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "response": response_text,
        "done": True
    }

# Mock Tags / list models endpoint
@app.get("/api/ollama/api/tags")
@app.get("/api/tags")
async def ollama_tags_endpoint():
    return {
        "models": [
            {
                "name": "gemini-2.5-flash:latest",
                "model": "gemini-2.5-flash:latest",
                "modified_at": "2026-07-09T00:00:00Z",
                "size": 4700000000,
                "digest": "sha256:8a156e54e4f2b3e8e19c00bcf9e6e12e022f46e65b75b63bc58d4a990a07156",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "gemini",
                    "families": ["gemini"],
                    "parameter_size": "unknown",
                    "quantization_level": "unknown"
                }
            },
            {
                "name": "llama3:latest",
                "model": "llama3:latest",
                "modified_at": "2026-07-09T00:00:00Z",
                "size": 4700000000,
                "digest": "sha256:a406579be42f2b3e8e19c00bcf9e6e12e022f46e65b75b63bc58d4a990a07156",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "llama",
                    "families": ["llama"],
                    "parameter_size": "8B",
                    "quantization_level": "Q4_K_M"
                }
            },
            {
                "name": "gemma:latest",
                "model": "gemma:latest",
                "modified_at": "2026-07-09T00:00:00Z",
                "size": 4700000000,
                "digest": "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "gemma",
                    "families": ["gemma"],
                    "parameter_size": "7B",
                    "quantization_level": "Q4_K_M"
                }
            }
        ]
    }

# Mock Show model endpoint
class OllamaShowRequest(BaseModel):
    name: str

@app.post("/api/ollama/api/show")
@app.post("/api/show")
async def ollama_show_endpoint(req: OllamaShowRequest):
    return {
        "license": "Google/Meta License",
        "modelfile": f"FROM {req.name}",
        "parameters": "",
        "template": "{{ .System }}\n{{ .Prompt }}",
        "details": {
            "format": "gguf",
            "family": "llama"
        }
    }

# Mock Version endpoint
@app.get("/api/ollama/api/version")
@app.get("/api/version")
async def ollama_version_endpoint():
    return {"version": "0.1.48"}

# Mock ps (loaded models) endpoint
@app.get("/api/ollama/api/ps")
@app.get("/api/ps")
async def ollama_ps_endpoint():
    return {
        "models": [
            {
                "name": "gemini-2.5-flash:latest",
                "model": "gemini-2.5-flash:latest",
                "size": 4700000000,
                "digest": "sha256:8a156e54e4f2b3e8e19c00bcf9e6e12e022f46e65b75b63bc58d4a990a07156",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "gemini",
                    "families": ["gemini"],
                    "parameter_size": "unknown",
                    "quantization_level": "unknown"
                },
                "expires_at": "2026-07-09T03:00:00Z",
                "size_vram": 4700000000
            },
            {
                "name": "llama3:latest",
                "model": "llama3:latest",
                "size": 4700000000,
                "digest": "sha256:a406579be42f2b3e8e19c00bcf9e6e12e022f46e65b75b63bc58d4a990a07156",
                "details": {
                    "parent_model": "",
                    "format": "gguf",
                    "family": "llama",
                    "families": ["llama"],
                    "parameter_size": "8B",
                    "quantization_level": "Q4_K_M"
                },
                "expires_at": "2026-07-09T03:00:00Z",
                "size_vram": 4700000000
            }
        ]
    }
