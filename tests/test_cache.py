import pytest
import time
import asyncio
from unittest.mock import patch, AsyncMock
from agent.core.cache import get_cached_response, set_cached_response, _compute_sha256
from agent.storage.db import get_connection, DB_FILE_PATH

@pytest.fixture(autouse=True)
def clean_cache_table():
    """Ensures llm_cache is clean before and after each test."""
    conn = get_connection(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM llm_cache")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    yield
    conn = get_connection(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM llm_cache")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

@pytest.mark.asyncio
async def test_l1_exact_match():
    model = "test-model"
    prompt = "Hello world!"
    system_instructions = "Be polite"
    response = "Hi there!"
    
    # Write to cache
    await set_cached_response(model, prompt, system_instructions, response, ttl_seconds=10)
    
    # Retrieve from cache
    cached = await get_cached_response(model, prompt, system_instructions)
    assert cached == response
    
    # Test different model doesn't match
    cached_diff_model = await get_cached_response("other-model", prompt, system_instructions)
    assert cached_diff_model is None

@pytest.mark.asyncio
async def test_l1_ttl_expiration():
    model = "test-model"
    prompt = "Expired prompt"
    response = "Expired response"
    
    # Write to cache with expired TTL
    await set_cached_response(model, prompt, None, response, ttl_seconds=-10)
    
    cached = await get_cached_response(model, prompt, None)
    assert cached is None

@pytest.mark.asyncio
async def test_l2_tfidf_fallback():
    model = "tfidf-model"
    await set_cached_response(model, "The quick brown fox jumps over the lazy dog", None, "Fox response", ttl_seconds=10)
    await set_cached_response(model, "Python is a great programming language", None, "Python response", ttl_seconds=10)
    
    # Query with a highly similar prompt (above 0.95 TF-IDF similarity threshold)
    cached = await get_cached_response(model, "The quick brown fox jumps over the lazy doggy", None)
    assert cached == "Fox response"

@pytest.mark.asyncio
async def test_l2_semantic_embeddings():
    model = "semantic-model"
    prompt1 = "What is the capital of France?"
    response1 = "Paris"
    
    # Mock embedding calls for both set and get
    # Return [1.0, 0.0] for prompt1 and [0.99, 0.01] for the search query (cosine similarity > 0.92)
    mock_embeddings = {
        prompt1: [1.0, 0.0],
        "What's the capital city of France?": [0.99, 0.01]
    }
    
    async def mock_fetch(text, api_key):
        return mock_embeddings.get(text, [0.0, 1.0])
        
    with patch("agent.core.cache._fetch_gemini_embedding", new=mock_fetch):
        # We pass a mock API key in environment to enable embedding flow
        with patch.dict("os.environ", {"GEMINI_API_KEY": "mock-key"}):
            # Save prompt1 response (which schedules background embedding calculation)
            await set_cached_response(model, prompt1, None, response1, ttl_seconds=10)
            
            # Wait briefly for background asyncio task to write embedding to database
            await asyncio.sleep(0.5)
            
            # Query similar prompt
            cached = await get_cached_response(model, "What's the capital city of France?", None)
            assert cached == response1
