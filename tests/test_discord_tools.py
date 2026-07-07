import pytest
from unittest import mock
import json
from agent import tools

@pytest.mark.asyncio
async def test_post_to_discord():
    with mock.patch("aiohttp.ClientSession.post") as mock_post:
        mock_response = mock.AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"status": "success", "message_id": 123, "channel": "lacie"}
        mock_post.return_value.__aenter__.return_value = mock_response
        
        res = await tools.post_to_discord("lacie", "hello")
        data = json.loads(res)
        assert data["status"] == "success"
        assert data["message_id"] == 123

@pytest.mark.asyncio
async def test_read_discord_channel():
    with mock.patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = mock.AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"messages": [{"id": "1", "author": "Ada", "content": "hello"}]}
        mock_get.return_value.__aenter__.return_value = mock_response
        
        res = await tools.read_discord_channel("lacie", limit=5)
        data = json.loads(res)
        assert len(data["messages"]) == 1
        assert data["messages"][0]["content"] == "hello"

@pytest.mark.asyncio
async def test_list_discord_channels():
    with mock.patch("aiohttp.ClientSession.get") as mock_get:
        mock_response = mock.AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"channels": [{"id": "123", "name": "lacie", "guild": "Ada Control"}]}
        mock_get.return_value.__aenter__.return_value = mock_response
        
        res = await tools.list_discord_channels()
        data = json.loads(res)
        assert len(data["channels"]) == 1
        assert data["channels"][0]["name"] == "lacie"
