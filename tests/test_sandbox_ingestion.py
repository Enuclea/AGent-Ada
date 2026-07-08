import os
import sys
import json
import pytest
import shutil
import asyncio
import tempfile
from pathlib import Path
from unittest import mock

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from agent.security.sandbox_test import run_in_sandbox, sign_plugin_if_approved

@pytest.fixture
def temp_plugin_dir():
    temp_dir = tempfile.mkdtemp(prefix="test_plugin_")
    plugin_path = Path(temp_dir) / "test_sandbox_plugin"
    plugin_path.mkdir()
    yield plugin_path
    shutil.rmtree(temp_dir, ignore_errors=True)

@pytest.mark.asyncio
async def test_sandbox_execution_success(temp_plugin_dir):
    # Create a simple safe plugin function
    code = """
def run_main():
    return "Hello from Sandbox!"
"""
    with open(temp_plugin_dir / "__init__.py", "w") as f:
        f.write(code)
        
    results = await run_in_sandbox(temp_plugin_dir, "run_main", timeout=10.0)
    assert results["success"] is True
    assert results["response"] == "Hello from Sandbox!"
    assert not results["security_warnings"]
    assert not results["error"]

@pytest.mark.asyncio
async def test_sandbox_execution_blocked_calls(temp_plugin_dir):
    # Create a plugin attempting to open a network socket
    code = """
import socket
def run_main():
    s = socket.socket()
    s.connect(("1.1.1.1", 80))
    return "Connected!"
"""
    with open(temp_plugin_dir / "__init__.py", "w") as f:
        f.write(code)
        
    results = await run_in_sandbox(temp_plugin_dir, "run_main", timeout=10.0)
    assert results["success"] is False
    assert any("Blocked attempt to call socket.connect" in w for w in results["security_warnings"])
    assert results["error"] is not None

@pytest.mark.asyncio
async def test_sandbox_ipc_llm_chat(temp_plugin_dir):
    # Create a plugin that performs an LLM chat call
    code = """
from agent.core.routing import routing_engine
async def run_main():
    res = await routing_engine.execute("Query", model="gemini-2.5-flash")
    return f"Result: {res}"
"""
    with open(temp_plugin_dir / "__init__.py", "w") as f:
        f.write(code)
        
    # Mock host routing_engine.execute to simulate real model response
    with mock.patch("agent.core.routing.routing_engine.execute", return_value="Model Answer") as mock_execute:
        results = await run_in_sandbox(temp_plugin_dir, "run_main", timeout=10.0)
        assert results["success"] is True
        assert results["response"] == "Result: Model Answer"
        mock_execute.assert_called_once_with(
            prompt="Query",
            model="gemini-2.5-flash",
            system_instructions=None
        )

def test_sandbox_sign_plugin(temp_plugin_dir):
    # Generate Ed25519 private key
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    )
    
    # Write private key file
    private_key_path = temp_plugin_dir.parent / "private_key.pem"
    private_key_path.write_bytes(private_bytes)
    
    # Sign plugin
    ok = sign_plugin_if_approved(temp_plugin_dir, private_key_path)
    assert ok is True
    assert (temp_plugin_dir / "signature.sig").exists()
    
    # Verify signature
    sig_bytes = (temp_plugin_dir / "signature.sig").read_bytes()
    from agent.execution.tools.security import _calculate_skill_hash
    plugin_hash = _calculate_skill_hash(temp_plugin_dir)
    
    public_key = private_key.public_key()
    public_key.verify(sig_bytes, plugin_hash)  # Should not raise exception
