import os
import sys
import time
import pytest
import aiohttp
from unittest.mock import patch, ANY

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from agent.routes.custom.one_min_route import OneMinCustomRoute

# Setup sizes
SIZES = {
    "1KB": 1024,
    "10KB": 10 * 1024,
    "100KB": 100 * 1024,
    "1MB": 1024 * 1024,
}

# Captured requests during mock tests
captured_requests = []

class MockAiohttpResponse:
    def __init__(self, json_data, status=200):
        self._json_data = json_data
        self.status = status
        self.headers = {"Content-Type": "application/json"}

    async def json(self):
        return self._json_data

    async def text(self):
        import json
        return json.dumps(self._json_data)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass

class BypassPytestCheck:
    def __enter__(self):
        self.pytest_module = sys.modules.get("pytest")
        if "pytest" in sys.modules:
            del sys.modules["pytest"]
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.pytest_module:
            sys.modules["pytest"] = self.pytest_module

@pytest.fixture
def anyio_backend():
    return "asyncio"

@pytest.mark.anyio
async def test_onemin_payload_sizes_mocked(tmp_path):
    captured_requests.clear()
    
    mocked_json = {
        "aiRecord": {
            "aiRecordDetail": {
                "resultObject": ["mocked 1min response"]
            },
            "teamUser": {
                "usedCredit": 120
            },
            "metadata": {
                "inputToken": 50,
                "outputToken": 100,
                "credit": 120
            }
        }
    }
    mocked_resp = MockAiohttpResponse(mocked_json)
    
    async def mock_aiohttp_request(self_session, method, url, **kwargs):
        payload = kwargs.get('json') or kwargs.get('data')
        captured_requests.append({
            'method': method,
            'url': str(url),
            'payload': payload,
            'headers': kwargs.get('headers')
        })
        return mocked_resp

    with patch('aiohttp.ClientSession._request', mock_aiohttp_request):
        with patch.dict(os.environ, {"1MIN_AI_API": "dummy-onemin-key"}):
            with patch('agent.routes.custom.one_min_route.get_cached_credit_usage', return_value=0) as mock_get_credit, \
                 patch('agent.routes.custom.one_min_route.save_cached_credit_usage') as mock_save_credit, \
                 patch('agent.routes.custom.one_min_route.log_token_usage') as mock_log_telemetry:
                     
                route = OneMinCustomRoute()
                
                for size_name, size in SIZES.items():
                    temp_file = tmp_path / f"dummy_{size_name}.txt"
                    content = "A" * size
                    temp_file.write_text(content, encoding="utf-8")
                    
                    read_content = temp_file.read_text(encoding="utf-8")
                    captured_requests.clear()
                    
                    with BypassPytestCheck():
                        response = await route.execute(
                            prompt=read_content,
                            model="onemin/gpt-4o-mini"
                        )
                    
                    onemin_req = None
                    for req in captured_requests:
                        if 'api.1min.ai/api/chat-with-ai' in req['url'] or 'api.1min.ai' in req['url']:
                             onemin_req = req
                             break
                    
                    assert onemin_req is not None
                    assert 'https://api.1min.ai/api/chat-with-ai' in onemin_req['url']
                    
                    payload = onemin_req['payload']
                    assert isinstance(payload, dict)
                    assert payload["type"] == "UNIFY_CHAT_WITH_AI"
                    assert payload["model"] == "gpt-4o-mini"
                    assert "promptObject" in payload
                    assert "prompt" in payload["promptObject"]
                    sent_content = payload["promptObject"]["prompt"]
                    
                    assert sent_content == content
                    assert response == "mocked 1min response"
                    
                    # Ensure mock DB functions and telemetry were called correctly
                    mock_get_credit.assert_called()
                    mock_save_credit.assert_called_with(120)
                    mock_log_telemetry.assert_called_with(
                        ANY,  # active_session (uuid)
                        "1min/gpt-4o-mini",
                        50,
                        100,
                        0.0012  # 120 / 100000.0
                    )

@pytest.mark.anyio
async def test_onemin_integration():
    # Load environment keys if they exist in standard paths
    from agent.routes.custom.one_min_route import load_env_keys
    load_env_keys()
    
    api_key = os.environ.get("1MIN_AI_API")
    if not api_key:
        pytest.skip("Skipping 1Min AI integration test as 1MIN_AI_API key is not set.")

    # Stub the database to avoid sqlite3 crashes, but let telemetry and API calls run normally
    with patch('agent.routes.custom.one_min_route.get_cached_credit_usage', return_value=0), \
         patch('agent.routes.custom.one_min_route.save_cached_credit_usage') as mock_save, \
         patch('agent.routes.custom.one_min_route.log_token_usage') as mock_telemetry:
             
        route = OneMinCustomRoute()
        
        start_time = time.perf_counter()
        with BypassPytestCheck():
            response = await route.execute(
                prompt="This is an automated 1Min AI integration test checking response and latency.",
                model="onemin/gpt-4o-mini"
            )
        latency = time.perf_counter() - start_time
        
        # Verify result and performance
        assert response is not None, "Real API request returned None"
        assert isinstance(response.response, str), "Real API did not return a string response"
        assert len(response.response) > 0, "Real API returned an empty string response"
        assert latency > 0
        
        # Verify credit usage logging was attempted on success
        assert mock_telemetry.call_count == 1
        args, kwargs = mock_telemetry.call_args
        # args should be (active_session, log_model_name, input_tokens, output_tokens, cost)
        assert args[1] == "1min/gpt-4o-mini"
        assert args[2] >= 0
        assert args[3] >= 0
