import os
import json
import pytest
from unittest import mock
from fastapi.testclient import TestClient

from agent.api.router import app

client = TestClient(app)

@pytest.fixture(autouse=True)
def setup_env():
    # Save the old value of TESTING to prevent test pollution
    old_testing = os.environ.get("TESTING")
    os.environ["TESTING"] = "1"
    yield
    if old_testing is not None:
        os.environ["TESTING"] = old_testing
    else:
        os.environ.pop("TESTING", None)

def test_ollama_generate_endpoint_success():
    """Caller-provided system instructions are passed through transparently."""
    payload = {
        "model": "gemini-3.5-flash",
        "prompt": "Test Generate Prompt",
        "system": "Test System Instructions",
        "stream": False
    }
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Test Generate Answer") as mock_exec:
        resp = client.post("/api/ollama/api/generate", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "gemini-3.5-flash"
        assert data["response"] == "Test Generate Answer"
        assert data["done"] is True
        mock_exec.assert_called_once_with(
            prompt="Test Generate Prompt",
            model_name="gemini-3.5-flash",
            system_instructions="Test System Instructions"
        )

def test_ollama_generate_default_system_prompt():
    """When no system field provided, falls back to OLLAMA_SYSTEM_PROMPT."""
    from agent.api.ollama_clone import OLLAMA_SYSTEM_PROMPT
    payload = {
        "model": "gemini-3.5-flash",
        "prompt": "Test Prompt",
        "stream": False
    }
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Answer") as mock_exec:
        resp = client.post("/api/ollama/generate", json=payload)
        assert resp.status_code == 200
        mock_exec.assert_called_once_with(
            prompt="Test Prompt",
            model_name="gemini-3.5-flash",
            system_instructions=OLLAMA_SYSTEM_PROMPT
        )

def test_ollama_chat_endpoint_system_message_passthrough():
    """System message from messages array is passed through to LLM."""
    payload = {
        "model": "claude-sonnet-4.6",
        "messages": [
            {"role": "system", "content": "You are a Python expert."},
            {"role": "user", "content": "Hello User"},
            {"role": "assistant", "content": "Hello Assistant"},
            {"role": "user", "content": "Follow up"}
        ],
        "stream": False
    }
    expected_prompt = "User: Hello User\nAssistant: Hello Assistant\nUser: Follow up"
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Chat Answer") as mock_exec:
        resp = client.post("/api/ollama/chat", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "claude-sonnet-4.6"
        assert data["message"]["role"] == "assistant"
        assert data["message"]["content"] == "Chat Answer"
        assert data["done"] is True
        mock_exec.assert_called_once_with(
            prompt=expected_prompt,
            model_name="claude-sonnet-4.6",
            system_instructions="You are a Python expert."
        )

def test_ollama_chat_endpoint_no_system_message():
    """When no system message in messages array, falls back to OLLAMA_SYSTEM_PROMPT."""
    from agent.api.ollama_clone import OLLAMA_SYSTEM_PROMPT
    payload = {
        "model": "gemini-3.5-flash",
        "messages": [
            {"role": "user", "content": "Just a question"}
        ],
        "stream": False
    }
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Answer") as mock_exec:
        resp = client.post("/api/ollama/chat", json=payload)
        assert resp.status_code == 200
        mock_exec.assert_called_once_with(
            prompt="User: Just a question",
            model_name="gemini-3.5-flash",
            system_instructions=OLLAMA_SYSTEM_PROMPT
        )

def test_ollama_missing_mode_header_and_query():
    """Requests without mode headers still work normally."""
    payload = {
        "model": "gemini-3.5-flash",
        "prompt": "Test Prompt",
        "stream": False
    }
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Success Without Headers"):
        resp = client.post("/api/ollama/generate", json=payload)
        assert resp.status_code == 200
        assert resp.json()["response"] == "Success Without Headers"

def test_ollama_mock_endpoints():
    # Test tags endpoint
    resp = client.get("/api/tags")
    assert resp.status_code == 200
    assert "models" in resp.json()
    
    # Test show endpoint
    resp = client.post("/api/show", json={"name": "llama3"})
    assert resp.status_code == 200
    assert resp.json()["template"] == "{{ .System }}\n{{ .Prompt }}"
    
    # Test version endpoint
    resp = client.get("/api/version")
    assert resp.status_code == 200
    assert resp.json()["version"] == "0.1.48"

    # Test ps endpoint
    resp = client.get("/api/ps")
    assert resp.status_code == 200
    assert "models" in resp.json()

    # Test status checks
    resp = client.head("/")
    assert resp.status_code == 200

    resp = client.get("/api/ollama")
    assert resp.status_code == 200
    assert resp.text == "Ollama is running"

def test_ollama_generate_endpoint_invalid():
    resp = client.post("/api/ollama/api/generate", json={"model": "llama3", "prompt": ""})
    assert resp.status_code == 400

def test_ollama_chat_endpoint_invalid():
    resp = client.post("/api/ollama/api/chat", json={"model": "llama3", "messages": []})
    assert resp.status_code == 400

def test_ollama_bearer_token_authentication():
    # Temporarily disable the test bypass sentinel so bearer auth is actually exercised
    import agent.api.router as router_mod
    original_bypass = router_mod._test_bypass_enabled
    router_mod._test_bypass_enabled = False
    
    old_testing = os.environ.get("TESTING")
    if "TESTING" in os.environ:
        os.environ.pop("TESTING")
    
    try:
        with mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": "secret-token"}):
            payload = {
                "model": "llama3",
                "prompt": "Test Prompt",
                "stream": False
            }
            headers_invalid = {"Authorization": "Bearer bad-token"}
            resp = client.post("/api/ollama/api/generate", json=payload, headers=headers_invalid)
            assert resp.status_code == 401
            
            headers_valid = {"Authorization": "Bearer secret-token"}
            with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Token Success"):
                resp = client.post("/api/ollama/api/generate", json=payload, headers=headers_valid)
                assert resp.status_code == 200
                assert resp.json()["response"] == "Token Success"
    finally:
        router_mod._test_bypass_enabled = original_bypass
        if old_testing is not None:
            os.environ["TESTING"] = old_testing

def test_ollama_generate_stream():
    payload = {
        "model": "llama3",
        "prompt": "Stream Test",
        "stream": True
    }
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Hello Stream"):
        resp = client.post("/api/ollama/generate", json=payload)
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/x-ndjson"
        
        lines = [line for line in resp.iter_lines() if line]
        assert len(lines) > 0
        
        first_chunk = json.loads(lines[0])
        assert first_chunk["model"] == "llama3"
        assert first_chunk["done"] is False
        
        final_chunk = json.loads(lines[-1])
        assert final_chunk["done"] is True
