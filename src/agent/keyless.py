import os
import shutil
import asyncio
import uuid
import glob
import json
import re
from pathlib import Path
from typing import Optional, List, Dict, Any
from google.antigravity.models import GeminiAPIEndpoint

class KeylessGeminiAPIEndpoint(GeminiAPIEndpoint):
    def validate_endpoint(self) -> None:
        # Bypass client-side API key validation for Gemini Developer API.
        # This allows routing to keyless / Ultra plan gateways via Go localharness/agy.
        pass

def get_harness_path() -> Optional[str]:
    """Resolves the path to the system-wide agy binary."""
    if "ANTIGRAVITY_HARNESS_PATH" in os.environ:
        return os.environ["ANTIGRAVITY_HARNESS_PATH"]
    
    # Check system PATH for 'agy'
    agy_path = shutil.which("agy")
    if agy_path:
        return agy_path
        
    # Fallback to standard local bin paths
    for p in ["/Users/dan/.local/bin/agy", "/home/dan/.local/bin/agy"]:
        if os.path.exists(p):
            return p
            
    return None

def setup_keyless_environment() -> None:
    """Sets the ANTIGRAVITY_HARNESS_PATH env var if system agy is found."""
    harness_path = get_harness_path()
    if harness_path:
        os.environ["ANTIGRAVITY_HARNESS_PATH"] = harness_path

class KeylessAgyResponse:
    def __init__(self, text: str):
        self.text = text
        self._chunks = [text]
        self._index = 0

    @property
    def thoughts(self):
        # Return an empty async generator for thoughts
        async def _empty_thoughts():
            if False:
                yield ""
        return _empty_thoughts()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk

    async def structured_output(self) -> dict:
        # Robustly parse JSON from the text response
        text = self.text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        
        # Try to find json block inside markdown code blocks
        pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
        match = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
                
        # Try to find any curly brace structure
        match = re.search(r"(\{.*\})", text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
                
        # Return a structure matching EmailAnalysis if we couldn't parse
        return {
            "action_required": False,
            "importance_reason": f"Failed to parse model JSON: {text}",
            "task_title": "",
            "task_description": ""
        }

class KeylessAgyAgent:
    def __init__(
        self,
        model: Optional[str] = None,
        system_instructions: Optional[str] = None,
        conversation_id: Optional[str] = None,
        response_schema: Optional[Any] = None,
    ):
        self.model = model
        self.system_instructions = system_instructions
        self.conversation_id = conversation_id
        self.response_schema = response_schema
        self._conversations_dir = str(Path.home() / ".gemini" / "antigravity-cli" / "conversations")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

    def _get_newest_conversation_id(self) -> Optional[str]:
        if not os.path.exists(self._conversations_dir):
            return None
        db_files = glob.glob(os.path.join(self._conversations_dir, "*.db"))
        if not db_files:
            return None
        db_files.sort(key=os.path.getmtime, reverse=True)
        return os.path.basename(db_files[0])[:-3]

    async def chat(self, prompt: str) -> KeylessAgyResponse:
        # Prepend system instructions to the prompt
        full_prompt = prompt
        if self.system_instructions:
            full_prompt = f"[System Instructions]\n{self.system_instructions}\n\n[User Prompt]\n{prompt}"

        # If response_schema is specified, request JSON format
        if self.response_schema:
            schema_instructions = (
                "\n\nYou MUST return the response ONLY as a raw JSON object. Do not include markdown code block formatting (such as ```json). "
                "Do not include any explanation or extra text. The JSON object structure MUST match:\n"
            )
            # Generate sample JSON from response_schema fields
            if hasattr(self.response_schema, "model_fields"):
                schema_fields = self.response_schema.model_fields
            elif hasattr(self.response_schema, "__fields__"):
                schema_fields = self.response_schema.__fields__
            else:
                schema_fields = {}
            
            sample_dict = {}
            for name, field in schema_fields.items():
                annotation = getattr(field, "annotation", str)
                if annotation is bool:
                    sample_dict[name] = True
                elif annotation is int:
                    sample_dict[name] = 0
                else:
                    sample_dict[name] = "string"
            schema_instructions += json.dumps(sample_dict, indent=2)
            full_prompt += schema_instructions

        harness_path = get_harness_path() or "agy"
        cmd = [harness_path, "-p", full_prompt]
        if self.conversation_id:
            cmd.extend(["--conversation", self.conversation_id])

        # Get list of existing conversation IDs before run
        prev_newest = self._get_newest_conversation_id()

        # Run agy command, redirecting stdin from DEVNULL to avoid hangs
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        # Look for newly created conversation database
        curr_newest = self._get_newest_conversation_id()
        if curr_newest and curr_newest != prev_newest:
            self.conversation_id = curr_newest
        elif not self.conversation_id and curr_newest:
            self.conversation_id = curr_newest

        response_text = stdout.decode("utf-8", errors="replace")
        return KeylessAgyResponse(response_text)

