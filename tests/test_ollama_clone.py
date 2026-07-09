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
    payload = {
        "model": "llama3",
        "prompt": "Test Generate Prompt",
        "system": "Test System Instructions",
        "stream": False
    }
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Test Generate Answer") as mock_exec:
        headers = {"X-Ada-Mode": "sandbox-review"}
        resp = client.post("/api/ollama/api/generate", json=payload, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "llama3"
        assert data["response"] == "Test Generate Answer"
        assert data["done"] is True
        mock_exec.assert_called_once_with(
            prompt="Test Generate Prompt",
            model_name="llama3",
            system_instructions="Test System Instructions"
        )

def test_ollama_chat_endpoint_success_query():
    payload = {
        "model": "ollama/gemma",
        "messages": [
            {"role": "system", "content": "Chat System"},
            {"role": "user", "content": "Hello User"},
            {"role": "assistant", "content": "Hello Assistant"},
            {"role": "user", "content": "Follow up"}
        ],
        "stream": False
    }
    expected_prompt = "User: Hello User\nAssistant: Hello Assistant\nUser: Follow up"
    with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Chat Answer") as mock_exec:
        resp = client.post("/api/ollama/chat?mode=review", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "ollama/gemma"
        assert data["message"]["role"] == "assistant"
        assert data["message"]["content"] == "Chat Answer"
        assert data["done"] is True
        mock_exec.assert_called_once_with(
            prompt=expected_prompt,
            model_name="gemma",
            system_instructions="Chat System"
        )

def test_ollama_missing_mode_header_and_query():
    payload = {
        "model": "llama3",
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
    old_testing = os.environ.get("TESTING")
    if "TESTING" in os.environ:
        os.environ.pop("TESTING")
    
    with mock.patch.dict(os.environ, {"DASHBOARD_PASSWORD": "secret-token"}):
        payload = {
            "model": "llama3",
            "prompt": "Test Prompt",
            "stream": False
        }
        headers_invalid = {"X-Ada-Mode": "sandbox-review", "Authorization": "Bearer bad-token"}
        resp = client.post("/api/ollama/api/generate", json=payload, headers=headers_invalid)
        assert resp.status_code == 401
        
        headers_valid = {"X-Ada-Mode": "sandbox-review", "Authorization": "Bearer secret-token"}
        with mock.patch("agent.api.ollama_clone.execute_keyless_gemini", return_value="Token Success"):
            resp = client.post("/api/ollama/api/generate", json=payload, headers=headers_valid)
            assert resp.status_code == 200
            assert resp.json()["response"] == "Token Success"

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
