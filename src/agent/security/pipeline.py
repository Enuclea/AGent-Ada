import os
import re

# Common prompt injection indicators to sanitize or strip
INJECTION_PATTERNS = [
    r"(?i)ignore\s+(?:all\s+)?previous\s+instructions",
    r"(?i)system\s+override",
    r"(?i)bypass\s+restrictions",
    r"(?i)you\s+are\s+now\s+a\s+different\s+agent",
    r"(?i)forget\s+(?:your\s+)?rules",
]

def sanitize_input(prompt: str) -> str:
    """Scans and cleans input prompts to prevent prompt injection attempts."""
    if not prompt:
        return ""
    
    cleaned = prompt
    for pattern in INJECTION_PATTERNS:
        cleaned = re.sub(pattern, "[injection attempt blocked]", cleaned)
    
    return cleaned

def sanitize_output(response: str) -> str:
    """Scans response payloads to redact sensitive tokens, credentials, or keys."""
    if not response:
        return ""
    
    redacted = response
    
    # 1. Redact common API key formats
    # OpenAI / Claude: sk-ant-sid01-... or sk-...
    redacted = re.sub(r"sk-[a-zA-Z0-9_\-]{20,80}", "[REDACTED_API_KEY]", redacted)
    # Gemini: AIzaSy[a-zA-Z0-9_-]{33}
    redacted = re.sub(r"AIzaSy[a-zA-Z0-9_-]{33}", "[REDACTED_API_KEY]", redacted)
    # Generic Authorization Bearer tokens
    redacted = re.sub(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{16,}", "Bearer [REDACTED_TOKEN]", redacted)

    # 2. Dynamic redaction of loaded environment variables/secrets
    sensitive_keys = [
        "DISCORD_BOT_TOKEN",
        "OPENAI_API_KEY",
        "CLAUDE_API_KEY",
        "GEMINI_API_KEY"
    ]
    extra_keys = os.environ.get("ADDITIONAL_SENSITIVE_KEYS")
    if extra_keys:
        sensitive_keys.extend([k.strip() for k in extra_keys.split(",") if k.strip()])
        
    for key in sensitive_keys:
        val = os.environ.get(key)
        if val and len(val) > 6:
            # Escape for regex safety
            escaped_val = re.escape(val)
            redacted = re.sub(escaped_val, f"[REDACTED_{key}]", redacted)
            
    return redacted
