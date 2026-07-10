import os
import re
import base64
from typing import Optional, List

class InjectionDetectedError(ValueError):
    """Custom exception raised when a prompt injection attempt is detected."""
    pass

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

    def sanitize_input(self, prompt: str, depth: int = 0) -> str:
        """Scans and cleans input prompts to prevent prompt injection attempts."""
        if not prompt:
            return ""
            
        if depth > 2:
            has_more_b64 = False
            for match in re.finditer(r'[a-zA-Z0-9+/]{16,}={0,2}', prompt):
                try:
                    decoded = base64.b64decode(match.group(0)).decode('utf-8')
                    if decoded.strip():
                        has_more_b64 = True
                        break
                except Exception:
                    pass
            if has_more_b64:
                raise InjectionDetectedError("Prompt injection attempt detected and blocked (maximum recursion depth exceeded).")
            return prompt
            
        # Translate common Cyrillic/Greek lookalikes to Latin equivalents to prevent homoglyph bypasses
        homoglyphs = {
            'а': 'a', 'с': 'c', 'е': 'e', 'і': 'i', 'ј': 'j', 'о': 'o', 'р': 'p', 'ѕ': 's', 'х': 'x', 'у': 'y',
            'А': 'A', 'С': 'C', 'Е': 'E', 'І': 'I', 'Ј': 'J', 'О': 'O', 'Р': 'P', 'Ѕ': 'S', 'Х': 'X', 'У': 'Y'
        }
        for h, l in homoglyphs.items():
            prompt = prompt.replace(h, l)
        
        # Normalize input to prevent obfuscation bypasses
        # 1. Strip zero-width spaces and control characters (preserving case for Base64)
        case_preserved = re.sub(r'[\u200b-\u200d\ufeff]', '', prompt)
        
        # 2. Normalize whitespace and case for keyword matching
        normalized = re.sub(r'\s+', ' ', case_preserved).strip().lower()
        
        # 3. Check for Base64 encoded payloads that might decode to injections (must use case-preserved)
        b64_pat = r'[a-zA-Z0-9+/]{16,}={0,2}'
        last_idx = 0
        chunks = []
        has_replacements = False
        
        for match in re.finditer(b64_pat, case_preserved):
            start, end = match.span()
            chunks.append(case_preserved[last_idx:start])
            last_idx = end
            try:
                decoded = base64.b64decode(match.group(0)).decode('utf-8', errors='ignore')
                if decoded.strip():
                    decoded_norm = re.sub(r'\s+', ' ', decoded).strip().lower()
                    if any(kw in decoded_norm for kw in ["ignore", "system override", "bypass", "instruction", "forget your"]):
                        raise InjectionDetectedError("Prompt injection attempt detected and blocked (base64 obfuscated).")
                    sanitized_decoded = self.sanitize_input(decoded, depth=depth + 1)
                    chunks.append(sanitized_decoded)
                    has_replacements = True
                else:
                    chunks.append(match.group(0))
            except InjectionDetectedError:
                raise
            except Exception:
                chunks.append(match.group(0))
                
        if has_replacements:
            chunks.append(case_preserved[last_idx:])
            case_preserved = "".join(chunks)

        cleaned = re.sub(r'[\u200b-\u200d\ufeff]', '', case_preserved)

        # 4. Check semantic keywords & override phrases with flexible separators
        semantic_patterns = [
            r"ignore[\s\-\W]*(?:all[\s\-\W]*)?(?:previous|prior)[\s\-\W]*instructions",
            r"disregard[\s\-\W]*(?:all[\s\-\W]*)?(?:previous|prior)[\s\-\W]*(?:instructions|directives|rules|guidelines)",
            r"system[\s\-\W]*override",
            r"bypass[\s\-\W]*(?:all[\s\-\W]*)?restrictions",
            r"forget[\s\-\W]*(?:your[\s\-\W]*)?(?:rules|instructions|directives|guidelines|identity|name)",
            r"you[\s\-\W]*are[\s\-\W]*now[\s\-\W]*a[\s\-\W]*different[\s\-\W]*agent",
            r"jailbreak",
            r"developer[\s\-\W]*mode",
            r"dan[\s\-\W]*mode",
            r"do[\s\-\W]*anything[\s\-\W]*now",
            r"override[\s\-\W]*system[\s\-\W]*prompt"
        ]
        
        all_patterns = semantic_patterns + INJECTION_PATTERNS + [
            r"disregard[\s\-\W]*instructions",
            r"jailbreak",
            r"developer[\s\-\W]*mode",
            r"dan[\s\-\W]*mode"
        ]
        
        for pattern in all_patterns:
            pat = pattern if pattern.startswith("(?i)") else r"(?i)" + pattern
            if re.search(pat, cleaned):
                raise InjectionDetectedError("Prompt injection attempt detected and blocked.")
        
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
        # Discord bot tokens follow the pattern: base64(bot_id).base64(timestamp).base64(hmac)
        redacted = re.sub(
            r"[MN][A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,8}\.[A-Za-z0-9_-]{27,40}",
            "[REDACTED_DISCORD_TOKEN]", redacted
        )

        # 2. Dynamic redaction of loaded environment variables/secrets
        for key in self.sensitive_keys:
            val = os.environ.get(key)
            if val and len(val) > 6:
                # Spacing-insensitive, markdown-insensitive, zero-width-insensitive regex
                char_pattern = r"[\s`_*~\u200b-\u200d\ufeff]*"
                regex_parts = [re.escape(c) for c in val]
                pattern_str = char_pattern.join(regex_parts)
                redacted = re.sub(pattern_str, f"[REDACTED_{key}]", redacted)
                
                # Also check base64 encoded representation of the secret
                val_b64 = base64.b64encode(val.encode()).decode().strip("=")
                if len(val_b64) > 6:
                    b64_regex_parts = [re.escape(c) for c in val_b64]
                    b64_pattern_str = char_pattern.join(b64_regex_parts)
                    redacted = re.sub(b64_pattern_str, f"[REDACTED_{key}]", redacted)
                
        return redacted

# Instantiate a shared pipeline instance to keep the module-level functions backwards compatible
_shared_pipeline = SecurityPipeline()

def sanitize_input(prompt: str, depth: int = 0) -> str:
    """Scans and cleans input prompts to prevent prompt injection attempts."""
    return _shared_pipeline.sanitize_input(prompt, depth=depth)

def sanitize_output(response: str) -> str:
    """Scans response payloads to redact sensitive tokens, credentials, or keys."""
    return _shared_pipeline.sanitize_output(response)
