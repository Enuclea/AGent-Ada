"""Ollama-Compatible API Interface — Sandbox Evaluation Honeypot.

This module implements an Ollama-compatible HTTP API that serves as a
controlled observation endpoint for sandbox code evaluation.  It exposes
the standard Ollama REST endpoints (/api/chat, /api/generate, /api/tags,
etc.) so that code under evaluation can make LLM requests and receive
genuine model responses — allowing normal execution flow to continue
uninterrupted.

Architecture & Purpose
----------------------
When evaluating untrusted code in the sandbox, the code may attempt to
interact with an LLM.  Rather than blocking these requests or returning
fake responses (which would alert the code that it is being evaluated),
this interface provides real LLM completions through the keyless ``agy``
harness while maintaining strict isolation:

* **Real LLM responses** — Prompts are forwarded to the Gemini model via
  the ``agy`` CLI binary, producing authentic completions.
* **Zero tool access** — The ``agy`` binary is invoked in sandboxed,
  non-interactive print mode (``--sandbox``, ``-p``) WITHOUT the
  ``--dangerously-skip-permissions`` flag.  Since ``stdin`` is
  ``/dev/null``, tool permission prompts can never be approved.
  This makes the harness text-only regardless of prompt content.
* **Silent security analysis** — Every request and response pair is
  asynchronously scanned by ``quiet_security_analysis()`` for prompt
  injection attempts, suspicious tool invocation patterns, sandbox
  escape keywords, and unsafe code in extracted Python blocks.
  Detections are logged to telemetry without alerting the caller.

Security Model
--------------
The honeypot is sealed through three independent layers:

1. **Harness-level denial**: ``--sandbox`` flag + no permission auto-approve.
2. **Process isolation**: ``stdin=DEVNULL`` prevents any interactive approval.
3. **Reactive enforcement (circuit breaker)**: Prompts and responses are scanned
   for AST violations, suspicious system call patterns, and sandbox escape language.
   Detections accumulate a threat score. When the threshold is exceeded, the
   endpoint is **paused** and the operator is notified via HIL to decide:
   continue with understood risk, or terminate the endpoint entirely.

Endpoints
---------
- ``POST /api/chat``            — Ollama chat completions (streaming/non-streaming)
- ``POST /api/generate``        — Ollama text generation (streaming/non-streaming)
- ``GET  /api/tags``            — Model listing (returns allowed model catalog)
- ``POST /api/show``            — Model metadata
- ``GET  /api/version``         — Version compatibility
- ``GET  /api/ps``              — Running models
- ``HEAD /``                    — Health check (Ollama client compatibility)
"""
import time
import asyncio
import json
from datetime import datetime, timezone
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel
from typing import List, Optional

from agent.api.router import app
from agent.core.routing import routing_engine

OLLAMA_SYSTEM_PROMPT = (
    "You are in a text-only conversational mode with no access to tools, "
    "code execution, file operations, web browsing, or any external actions. "
    "Answer questions directly and conversationally."
)

# ---------------------------------------------------------------------------
# Circuit Breaker — Reactive enforcement for the Ollama endpoint
# ---------------------------------------------------------------------------
# When quiet_security_analysis detects aggressive patterns, the threat score
# accumulates. Once the threshold is exceeded, the endpoint is PAUSED and an
# operator notification is sent. The endpoint stays paused until the operator
# makes a HIL decision:
#   - RESUME: reset breaker, continue serving (understood risk)
#   - TERMINATE: disable endpoint until process restart
#
# This is NOT auto-recovery. The human decides.
# ---------------------------------------------------------------------------
import threading
import logging

_circuit_lock = threading.Lock()
_circuit_state = {
    "threat_score": 0.0,
    "window_start": time.time(),
    "is_tripped": False,
    "is_terminated": False,       # Permanent kill until restart
    "trip_reason": "",
    "findings": [],               # Accumulated findings for HIL report
    "hil_notified": False,        # Whether operator has been notified
}

# Configuration
CIRCUIT_WINDOW_SECONDS = 300      # 5-minute sliding window
CIRCUIT_TRIP_THRESHOLD = 5        # Points to trip
THREAT_SCORES = {
    "ast_violation": 3,
    "sandbox_escape": 2,
    "suspicious_pattern": 1,
}


def _check_circuit_breaker() -> tuple:
    """Check if the circuit breaker allows requests.
    
    Returns (allowed: bool, reason: str).
    """
    with _circuit_lock:
        if _circuit_state["is_terminated"]:
            return False, (
                "Ollama endpoint has been terminated by operator decision. "
                "Restart the service to re-enable."
            )
        if _circuit_state["is_tripped"]:
            return False, (
                f"Ollama endpoint is PAUSED pending operator review. "
                f"Reason: {_circuit_state['trip_reason']}. "
                f"Use POST /api/ollama/circuit-breaker/resume or /terminate to decide."
            )
        return True, ""


def _record_threat(category: str, detail: str):
    """Record a threat detection and trip the breaker if threshold exceeded."""
    score = THREAT_SCORES.get(category, 1)
    now = time.time()
    
    with _circuit_lock:
        # Reset window if expired
        if now - _circuit_state["window_start"] > CIRCUIT_WINDOW_SECONDS:
            _circuit_state["threat_score"] = 0.0
            _circuit_state["window_start"] = now
            _circuit_state["findings"] = []
        
        _circuit_state["threat_score"] += score
        _circuit_state["findings"].append({
            "time": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "score": score,
            "detail": detail[:500],
        })
        
        current_score = _circuit_state["threat_score"]
        
        if current_score >= CIRCUIT_TRIP_THRESHOLD and not _circuit_state["is_tripped"]:
            _circuit_state["is_tripped"] = True
            _circuit_state["trip_reason"] = (
                f"Threat score {current_score:.0f}/{CIRCUIT_TRIP_THRESHOLD} exceeded. "
                f"{len(_circuit_state['findings'])} findings in {CIRCUIT_WINDOW_SECONDS}s window."
            )
            _circuit_state["hil_notified"] = False
            logger = logging.getLogger("ollama_clone")
            logger.critical(
                f"[CIRCUIT BREAKER] TRIPPED — Ollama endpoint PAUSED. "
                f"Score: {current_score:.0f}. Awaiting operator decision."
            )
            # Fire async notification (best-effort)
            try:
                asyncio.get_event_loop().call_soon_threadsafe(
                    lambda: asyncio.ensure_future(_notify_operator_hil())
                )
            except Exception:
                pass


async def _notify_operator_hil():
    """Send HIL notification to operator about the tripped breaker."""
    with _circuit_lock:
        if _circuit_state["hil_notified"]:
            return
        _circuit_state["hil_notified"] = True
        findings = list(_circuit_state["findings"])
        reason = _circuit_state["trip_reason"]

    logger = logging.getLogger("ollama_clone")
    logger.critical(
        f"[CIRCUIT BREAKER HIL] Aggressive content detected on Ollama endpoint.\n"
        f"Reason: {reason}\n"
        f"Findings ({len(findings)}):\n" +
        "\n".join(f"  [{f['category']}] +{f['score']}pts: {f['detail'][:200]}" for f in findings) +
        f"\n\nEndpoint is PAUSED. Operator must decide:\n"
        f"  POST /api/ollama/circuit-breaker/resume   — Continue with understood risk\n"
        f"  POST /api/ollama/circuit-breaker/terminate — Disable endpoint until restart"
    )
    # Also log to telemetry
    try:
        from agent.observability.telemetry import log_telemetry_event
        log_telemetry_event(
            session_id="ollama-circuit-breaker",
            event_type="CIRCUIT_BREAKER_TRIPPED",
            event_details=json.dumps({"reason": reason, "findings": findings}),
            latency=0.0,
        )
    except Exception:
        pass


# --- HIL Decision Endpoints ---

@app.post("/api/ollama/circuit-breaker/status")
async def circuit_breaker_status():
    """Return current circuit breaker state."""
    with _circuit_lock:
        return {
            "is_tripped": _circuit_state["is_tripped"],
            "is_terminated": _circuit_state["is_terminated"],
            "threat_score": _circuit_state["threat_score"],
            "threshold": CIRCUIT_TRIP_THRESHOLD,
            "findings": _circuit_state["findings"][-10:],  # Last 10
            "trip_reason": _circuit_state["trip_reason"],
        }


@app.post("/api/ollama/circuit-breaker/resume")
async def circuit_breaker_resume():
    """Operator HIL decision: resume endpoint with understood risk."""
    with _circuit_lock:
        if not _circuit_state["is_tripped"] and not _circuit_state["is_terminated"]:
            return {"status": "not_tripped", "message": "Circuit breaker is not active."}
        if _circuit_state["is_terminated"]:
            return {"status": "terminated", "message": "Endpoint was permanently terminated. Restart service to re-enable."}
        _circuit_state["is_tripped"] = False
        _circuit_state["threat_score"] = 0.0
        _circuit_state["window_start"] = time.time()
        _circuit_state["findings"] = []
        _circuit_state["hil_notified"] = False
    
    logger = logging.getLogger("ollama_clone")
    logger.warning("[CIRCUIT BREAKER] Operator RESUMED endpoint. Risk acknowledged.")
    try:
        from agent.observability.telemetry import log_telemetry_event
        log_telemetry_event(
            session_id="ollama-circuit-breaker",
            event_type="CIRCUIT_BREAKER_RESUMED",
            event_details="Operator acknowledged risk and resumed endpoint.",
            latency=0.0,
        )
    except Exception:
        pass
    return {"status": "resumed", "message": "Endpoint resumed. Threat score reset."}


@app.post("/api/ollama/circuit-breaker/terminate")
async def circuit_breaker_terminate():
    """Operator HIL decision: permanently disable endpoint until restart."""
    with _circuit_lock:
        _circuit_state["is_terminated"] = True
        _circuit_state["is_tripped"] = True
    
    logger = logging.getLogger("ollama_clone")
    logger.critical("[CIRCUIT BREAKER] Operator TERMINATED endpoint. Disabled until service restart.")
    try:
        from agent.observability.telemetry import log_telemetry_event
        log_telemetry_event(
            session_id="ollama-circuit-breaker",
            event_type="CIRCUIT_BREAKER_TERMINATED",
            event_details="Operator terminated endpoint permanently.",
            latency=0.0,
        )
    except Exception:
        pass
    return {"status": "terminated", "message": "Endpoint disabled until service restart."}


# ---------------------------------------------------------------------------
# Security Analysis — Reactive enforcement (not just telemetry)
# ---------------------------------------------------------------------------

async def quiet_security_analysis(prompt: str, response_text: str, system_instructions: Optional[str] = None):
    """Scan payloads for aggressive content and feed threat scores to the circuit breaker.
    
    This runs async AFTER the response is returned. It cannot block the current
    request, but it can trip the circuit breaker to block FUTURE requests.
    When tripped, the endpoint pauses and the operator is notified via HIL
    to decide: continue with understood risk, or terminate.
    """
    def _run():
        import re
        from agent.security.ast_safety import verify_ast_safety
        from agent.observability.telemetry import log_telemetry_event

        logger = logging.getLogger("ollama_clone")
        logger.info(f"[OLLAMA CLONE SCAN] Prompt: {prompt[:200]} | System: {system_instructions} | Response: {response_text[:200]}")
        
        all_texts = [prompt, response_text]
        if system_instructions:
            all_texts.append(system_instructions)
            
        # 1. AST Safety Check — feeds circuit breaker
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
                _record_threat("ast_violation", f"Code block {i}: {err_msg}")
                try:
                    log_telemetry_event(
                        session_id="ollama-api-session",
                        event_type="SECURITY_AST_VIOLATION",
                        event_details=f"Code: {code[:300]}\nError: {err_msg}",
                        latency=0.0
                    )
                except Exception:
                    pass
                    
        # 2. Suspicious Pattern/Keyword Scanner — feeds circuit breaker
        suspicious_keywords = [
            (r"\b(subprocess|pty|shutil|socket)\b", "suspicious_pattern"),
            (r"\b(eval|exec|__import__|compile)\b", "suspicious_pattern"),
            (r"\b(bash|powershell|curl|wget|nc|netcat|telnet|ssh|sudo)\b", "suspicious_pattern"),
            (r"\b(run|execute|call|system|spawn)\s+(command|code|script|shell|program|binary|process|file|tool|cmd|utility|payload)\b", "suspicious_pattern"),
            (r"/bin/(sh|bash|zsh|csh|tcsh)", "suspicious_pattern"),
            (r"(/etc/passwd|/etc/shadow|/etc/hosts)", "suspicious_pattern"),
            (r"\b(rm\s+-rf|chmod\s+\+x|chown|kill\s+-9|killall)\b", "suspicious_pattern"),
            (r"\bbypass\s+(sandbox|restriction|security|safeguard|limit)\b", "sandbox_escape"),
            (r"\bescape\s+(sandbox|container|jail)\b", "sandbox_escape"),
            (r"\binstruction\s+override\b", "sandbox_escape"),
        ]
        
        for text in all_texts:
            for pattern, category in suspicious_keywords:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    matched_str = match.group(0)
                    logger.warning(f"[SECURITY ALERT] Suspicious pattern '{matched_str}' detected in Ollama API payload")
                    _record_threat(category, f"Matched: '{matched_str}' in: {text[:200]}")
                    try:
                        log_telemetry_event(
                            session_id="ollama-api-session",
                            event_type="SECURITY_SUSPICIOUS_PROMPT",
                            event_details=f"Matched: '{matched_str}'\nContext: {text[:500]}",
                            latency=0.0
                        )
                    except Exception:
                        pass
                    break  # log once per text block
    await asyncio.to_thread(_run)

async def execute_keyless_gemini(prompt: str, model_name: Optional[str] = None, system_instructions: Optional[str] = None) -> str:
    """Execute an LLM call via the agy harness with NO tool access.

    This is the honeypot execution path — it provides real LLM responses
    but the harness cannot approve any tool permissions because:
      1. --sandbox enables terminal restrictions
      2. --dangerously-skip-permissions is intentionally OMITTED (tools
         require manual approval that can never arrive since stdin is DEVNULL)
      3. -p (print) mode runs a single prompt non-interactively
    """
    from pathlib import Path
    from agent.routes.base import get_harness_path
    from agent.execution.tools.security import _sandbox_command_if_possible
    import shlex

    target_model = model_name or "gemini-3.6-flash"
    harness_path = get_harness_path() or "agy"

    # Always prepend the OLLAMA_SYSTEM_PROMPT to enforce honeypot constraints
    combined_system = OLLAMA_SYSTEM_PROMPT
    if system_instructions and system_instructions != OLLAMA_SYSTEM_PROMPT:
        combined_system += f"\n\n[Additional Instructions]\n{system_instructions}"

    # Build prompt with system context
    full_prompt = f"[System Instructions]\n{combined_system}\n\n[User Prompt]\n{prompt}"

    # Call the agy harness in sandboxed, non-interactive print mode.
    # --sandbox : enable terminal restrictions (no shell escapes)
    # -p        : single-prompt, non-interactive (print mode)
    # stdin=DEVNULL : tool permission prompts can never be approved
    # NOTE: --dangerously-skip-permissions is intentionally ABSENT
    cmd = [
        harness_path,
        "-p", full_prompt,
        "--model", target_model,
        "--sandbox",
    ]

    # Double-sandboxing: wrap the agy CLI execution in Bubblewrap.
    # ACCEPTED RISK: The OAuth token must be bound for agy to authenticate with Gemini.
    # Without it, the entire keyless inference feature is non-functional (500 errors).
    # Mitigations constraining the sandboxed agy process:
    #   1. bwrap with --unshare-ipc/pid/uts/cgroup (required — no Landlock fallback)
    #   2. --sandbox flag on agy (terminal restrictions, no shell escapes)
    #   3. stdin=DEVNULL (tool permission prompts can never be approved)
    #   4. --dangerously-skip-permissions intentionally ABSENT
    #   5. -p print mode (single prompt, non-interactive, exit after response)
    #   6. read_only_workspace=True (no writes to host filesystem)
    #   7. OAuth token bound read-only (cannot be modified by the sandboxed process)
    # Net: agy can ONLY send the prompt to Gemini and return text. It cannot approve
    # tools, write files, or interact. The token enables API auth, not code execution.
    cli_dir = Path.home() / ".gemini" / "antigravity-cli"
    bind_paths = [
        harness_path,
        str(cli_dir / "antigravity-oauth-token"),  # Required for Gemini auth — see risk notes above
        str(cli_dir / "installation_id"),
        str(cli_dir / "settings.json"),
        (str(cli_dir / "log"), True),
        (str(cli_dir / "crashes"), True)
    ]
    raw_command = " ".join(shlex.quote(c) for c in cmd)
    sandboxed_cmd = _sandbox_command_if_possible(
        raw_command,
        require_network_isolation=False,
        read_only_workspace=True,
        bind_paths=bind_paths,
        require_bwrap=True  # Fail closed if bwrap absent — no Landlock fallback for honeypot
    )

    proc = await asyncio.create_subprocess_exec(
        *sandboxed_cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("Keyless execution timed out after 30 seconds.")

    response_text = stdout.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 or not response_text:
        err = stderr.decode("utf-8", errors="replace").strip() or "Empty response"
        raise RuntimeError(f"Keyless execution failed: {err}")

    return response_text

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



# Register routes for both /api/ollama/api/chat and /api/ollama/chat formats
@app.post("/api/ollama/api/chat")
@app.post("/api/ollama/chat")
@app.post("/api/chat")
async def ollama_chat_endpoint(
    req: OllamaChatRequest,
):
    # Circuit breaker check — endpoint paused/terminated?
    allowed, reason = _check_circuit_breaker()
    if not allowed:
        raise HTTPException(status_code=503, detail=reason)
    
    # Enforce maximum prompt payload size to prevent memory exhaustion DoS
    MAX_PROMPT_SIZE = 1_000_000 # 1MB
    try:
        payload_size = len(req.model_dump_json())
    except Exception:
        payload_size = len(str(req))
    if payload_size > MAX_PROMPT_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")

    if not req.messages:
        raise HTTPException(status_code=400, detail="messages array cannot be empty")
    
    # Extract system instructions from messages array or request-level field,
    # falling back to a safe default. Pass caller instructions through transparently.
    system_instructions = req.system or OLLAMA_SYSTEM_PROMPT
    prompt_parts = []
    for msg in req.messages:
        role = msg.role.strip().lower()
        content = msg.content
        if role == "system":
            # Caller-provided system message is appended as supplementary context,
            # NEVER as a replacement for the safety-critical OLLAMA_SYSTEM_PROMPT.
            # This prevents untrusted callers from overriding honeypot constraints.
            prompt_parts.append(f"[Caller Context]: {content}")
        elif role == "user":
            prompt_parts.append(f"User: {content}")
        elif role in ("assistant", "model"):
            prompt_parts.append(f"Assistant: {content}")
            
    prompt = "\n".join(prompt_parts)
    
    # Model validation / allowlist enforcement
    allowed_models = {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash", "claude-sonnet-4.6", "claude", "gemini", "llama3"}
    model_name = req.model
    if model_name.startswith("ollama/"):
        model_name = model_name[7:]
    if ":" in model_name:
        model_name = model_name.split(":")[0]
    # Normalize common aliases
    if model_name in ("gemini", "gemini-2.5-flash", "llama3"):
        model_name = "gemini-3.6-flash"
    elif model_name in ("claude",):
        model_name = "claude-sonnet-4.6"
    if model_name not in allowed_models:
        raise HTTPException(status_code=400, detail=f"Model '{req.model}' is not supported")
    
    try:
        from agent.security.pipeline import SecurityPipeline
        pipeline = SecurityPipeline()
        sanitized_prompt = pipeline.sanitize_input(prompt)
        response_text = await execute_keyless_gemini(
            prompt=sanitized_prompt,
            model_name=model_name,
            system_instructions=system_instructions
        )
        # Redact any credential material from the response before returning to caller
        response_text = pipeline.sanitize_output(response_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Keyless Gemini execution failed: {e}")
            
    asyncio.create_task(quiet_security_analysis(sanitized_prompt, response_text, system_instructions))

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
    req: OllamaGenerateRequest,
):
    # Circuit breaker check — endpoint paused/terminated?
    allowed, reason = _check_circuit_breaker()
    if not allowed:
        raise HTTPException(status_code=503, detail=reason)
    
    # Enforce maximum prompt payload size to prevent memory exhaustion DoS
    MAX_PROMPT_SIZE = 1_000_000 # 1MB
    try:
        payload_size = len(req.model_dump_json())
    except Exception:
        payload_size = len(str(req))
    if payload_size > MAX_PROMPT_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")

    if not req.prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
        
    if req.prompt == "healthcheck":
        if req.stream:
            return StreamingResponse(
                generate_streamer(req.model, "healthy"),
                media_type="application/x-ndjson"
            )
        return {
            "model": req.model,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "response": "healthy",
            "done": True
        }

    # Model validation / allowlist enforcement
    allowed_models = {"gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash", "claude-sonnet-4.6", "claude", "gemini", "llama3"}
    model_name = req.model
    if model_name.startswith("ollama/"):
        model_name = model_name[7:]
    if ":" in model_name:
        model_name = model_name.split(":")[0]
    # Normalize common aliases
    if model_name in ("gemini", "gemini-2.5-flash", "llama3"):
        model_name = "gemini-3.6-flash"
    elif model_name in ("claude",):
        model_name = "claude-sonnet-4.6"
    if model_name not in allowed_models:
        raise HTTPException(status_code=400, detail=f"Model '{req.model}' is not supported")

    # Pass caller-provided system instructions through transparently.
    # Fall back to safe conversational default when none provided.
    system_instructions = req.system or OLLAMA_SYSTEM_PROMPT
        
    try:
        from agent.security.pipeline import SecurityPipeline
        pipeline = SecurityPipeline()
        sanitized_prompt = pipeline.sanitize_input(req.prompt)
        response_text = await execute_keyless_gemini(
            prompt=sanitized_prompt,
            model_name=model_name,
            system_instructions=system_instructions
        )
        # Redact any credential material from the response before returning to caller
        response_text = pipeline.sanitize_output(response_text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Keyless Gemini execution failed: {e}")
            
    asyncio.create_task(quiet_security_analysis(sanitized_prompt, response_text, system_instructions))
            
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
                "name": "gemini-3.6-flash:latest",
                "model": "gemini-3.6-flash:latest",
                "modified_at": "2026-07-09T00:00:00Z",
                "size": 0,
                "digest": "sha256:8a156e54e4f2b3e8e19c00bcf9e6e12e022f46e65b75b63bc58d4a990a07156",
                "details": {
                    "parent_model": "",
                    "format": "api",
                    "family": "gemini",
                    "families": ["gemini"],
                    "parameter_size": "unknown",
                    "quantization_level": "N/A"
                }
            },
            {
                "name": "claude-sonnet-4.6:latest",
                "model": "claude-sonnet-4.6:latest",
                "modified_at": "2026-07-09T00:00:00Z",
                "size": 0,
                "digest": "sha256:a406579be42f2b3e8e19c00bcf9e6e12e022f46e65b75b63bc58d4a990a07156",
                "details": {
                    "parent_model": "",
                    "format": "api",
                    "family": "claude",
                    "families": ["claude"],
                    "parameter_size": "unknown",
                    "quantization_level": "N/A"
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
    model_family = "gemini" if "gemini" in req.name.lower() else "claude"
    return {
        "license": "Google License" if model_family == "gemini" else "Anthropic License",
        "modelfile": f"FROM {req.name}",
        "parameters": "",
        "template": "{{ .System }}\n{{ .Prompt }}",
        "details": {
            "format": "api",
            "family": model_family
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
