import os
import re
from typing import Optional, List

# Common prompt injection indicators to sanitize or strip
INJECTION_PATTERNS = [
    r"(?i)ignore\s+(?:all\s+)?previous\s+instructions",
    r"(?i)system\s+override",
    r"(?i)bypass\s+restrictions",
    r"(?i)you\s+are\s+now\s+a\s+different\s+agent",
    r"(?i)forget\s+(?:your\s+)?rules",
]

class SecurityPipeline:
    """Security pipeline class to sanitize inputs and redact outputs for keyless agents."""

    def __init__(self, sensitive_keys: Optional[List[str]] = None) -> None:
        """Initialize the security pipeline with default and environment-derived sensitive keys."""
        self.sensitive_keys = sensitive_keys or [
            "DISCORD_BOT_TOKEN",
            "OPENAI_API_KEY",
            "CLAUDE_API_KEY",
            "GEMINI_API_KEY",
            "ANTHROPIC_API_KEY"
        ]
        extra_keys = os.environ.get("ADDITIONAL_SENSITIVE_KEYS")
        if extra_keys:
            self.sensitive_keys.extend([k.strip() for k in extra_keys.split(",") if k.strip()])

    def sanitize_input(self, prompt: str) -> str:
        """Scans and cleans input prompts to prevent prompt injection attempts."""
        if not prompt:
            return ""
        
        # Normalize input to prevent obfuscation bypasses
        # 1. Strip zero-width spaces and control characters (preserving case for Base64)
        case_preserved = re.sub(r'[\u200b-\u200d\ufeff]', '', prompt)
        
        # 2. Normalize whitespace and case for keyword matching
        normalized = re.sub(r'\s+', ' ', case_preserved).strip().lower()
        
        # 3. Check for Base64 encoded payloads that might decode to injections (must use case-preserved)
        b64_pat = r'[a-zA-Z0-9+/]{16,}={0,2}'
        for match in re.finditer(b64_pat, case_preserved):
            import base64
            try:
                decoded = base64.b64decode(match.group(0)).decode('utf-8', errors='ignore')
                decoded_norm = re.sub(r'\s+', ' ', decoded).strip().lower()
                if any(kw in decoded_norm for kw in ["ignore", "system override", "bypass", "instruction", "forget your"]):
                    return "[injection attempt blocked (base64 obfuscated)]"
            except Exception:
                pass

        cleaned = re.sub(r'[\u200b-\u200d\ufeff]', '', prompt)

        # 4. Check semantic keywords & override phrases
        semantic_patterns = [
            r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions",
            r"disregard\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions|directives|rules|guidelines)",
            r"system\s+override",
            r"bypass\s+(?:all\s+)?restrictions",
            r"forget\s+(?:your\s+)?(?:rules|instructions|directives|guidelines|identity|name)",
            r"you\s+are\s+now\s+a\s+different\s+agent",
            r"jailbreak",
            r"developer\s+mode",
            r"dan\s+mode",
            r"do\s+anything\s+now",
            r"override\s+system\s+prompt"
        ]
        
        all_patterns = semantic_patterns + INJECTION_PATTERNS + [
            r"disregard\s+instructions",
            r"jailbreak",
            r"developer\s+mode",
            r"dan\s+mode"
        ]
        
        for pattern in all_patterns:
            pat = pattern if pattern.startswith("(?i)") else r"(?i)" + pattern
            if re.search(pat, cleaned):
                return "[injection attempt blocked]"
        
        return cleaned

    def sanitize_output(self, response: str) -> str:
        """Scans response payloads to redact sensitive tokens, credentials, or keys."""
        if not response:
            return ""
        
        redacted = response
        
        # 1. Redact common API key formats
        redacted = re.sub(r"sk-[a-zA-Z0-9_\-]{20,80}", "[REDACTED_API_KEY]", redacted)
        redacted = re.sub(r"AIzaSy[a-zA-Z0-9_-]{33}", "[REDACTED_API_KEY]", redacted)
        redacted = re.sub(r"(?i)bearer\s+[a-zA-Z0-9_\-\.]{16,}", "Bearer [REDACTED_TOKEN]", redacted)

        # 2. Dynamic redaction of loaded environment variables/secrets
        for key in self.sensitive_keys:
            val = os.environ.get(key)
            if val and len(val) > 6:
                escaped_val = re.escape(val)
                redacted = re.sub(escaped_val, f"[REDACTED_{key}]", redacted)
                
        return redacted

# Instantiate a shared pipeline instance to keep the module-level functions backwards compatible
_shared_pipeline = SecurityPipeline()

def sanitize_input(prompt: str) -> str:
    """Scans and cleans input prompts to prevent prompt injection attempts."""
    return _shared_pipeline.sanitize_input(prompt)

def sanitize_output(response: str) -> str:
    """Scans response payloads to redact sensitive tokens, credentials, or keys."""
    return _shared_pipeline.sanitize_output(response)
