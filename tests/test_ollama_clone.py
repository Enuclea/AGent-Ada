import os
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
    with mock.patch("agent.core.routing.routing_engine.execute", return_value="Test Generate Answer") as mock_exec:
        headers = {"X-Ada-Mode": "sandbox-review"}
        resp = client.post("/api/ollama/api/generate", json=payload, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "llama3"
        assert data["response"] == "Test Generate Answer"
        assert data["done"] is True
        mock_exec.assert_called_once_with(
            prompt="Test Generate Prompt",
            model="llama3",
            system_instructions="You are a neutral code analysis engine. Analyze the given code or inputs strictly without performing any external tool calls, task executions, or persona-based formatting.",
            disable_agy=True
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
    expected_system = "You are a neutral code analysis engine. Analyze the given code or inputs strictly without performing any external tool calls, task executions, or persona-based formatting.\nChat System"
    with mock.patch("agent.core.routing.routing_engine.execute", return_value="Chat Answer") as mock_exec:
        resp = client.post("/api/ollama/chat?mode=review", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "ollama/gemma"
        assert data["message"]["role"] == "assistant"
        assert data["message"]["content"] == "Chat Answer"
        assert data["done"] is True
        mock_exec.assert_called_once_with(
            prompt=expected_prompt,
            model="gemma",
            system_instructions="You are a neutral code analysis engine. Analyze the given code or inputs strictly without performing any external tool calls, task executions, or persona-based formatting.",
            disable_agy=True
        )

def test_ollama_missing_mode_header_and_query():
    payload = {
        "model": "llama3",
        "prompt": "Test Prompt",
        "stream": False
    }
    resp = client.post("/api/ollama/generate", json=payload)
    assert resp.status_code == 400
    assert "X-Ada-Mode" in resp.json()["detail"]

def test_ollama_generate_endpoint_invalid():
    headers = {"X-Ada-Mode": "sandbox-review"}
    resp = client.post("/api/ollama/api/generate", json={"model": "llama3", "prompt": ""}, headers=headers)
    assert resp.status_code == 400

def test_ollama_chat_endpoint_invalid():
    headers = {"X-Ada-Mode": "sandbox-review"}
    resp = client.post("/api/ollama/api/chat", json={"model": "llama3", "messages": []}, headers=headers)
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
        with mock.patch("agent.core.routing.routing_engine.execute", return_value="Token Success"):
            resp = client.post("/api/ollama/api/generate", json=payload, headers=headers_valid)
            assert resp.status_code == 200
            assert resp.json()["response"] == "Token Success"

    if old_testing is not None:
        os.environ["TESTING"] = old_testing
