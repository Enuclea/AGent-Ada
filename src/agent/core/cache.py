"""Dual-level LLM caching layer.

Provides exact-match L1 caching and semantic/TF-IDF L2 similarity caching
to reduce LLM routing costs and latency.
"""

import os
import hashlib
import json
import time
import math
import re
import asyncio
from collections import Counter
from typing import Optional, List, Dict, Any
import aiohttp

from agent.storage.db import get_connection, DB_FILE_PATH

# Configuration Defaults
DEFAULT_TTL = 86400  # 24 hours
L2_SEMANTIC_THRESHOLD = 0.90  # Cosine similarity for embeddings
L2_TFIDF_THRESHOLD = 0.80     # Cosine similarity for TF-IDF fallback

def _compute_sha256(model: str, prompt: str, system_instructions: Optional[str]) -> str:
    """Computes a unique cache key based on LLM input inputs."""
    sys_inst = system_instructions or ""
    serialized = f"{model.strip()}:{prompt.strip()}:{sys_inst.strip()}"
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

def _tokenize(text: str) -> List[str]:
    """Basic alphanumeric tokenization for TF-IDF similarity."""
    return re.findall(r"\w+", text.lower())

def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Computes standard cosine similarity between two float vectors."""
    if len(v1) != len(v2) or not v1:
        return 0.0
    dot_product = sum(x * y for x, y in zip(v1, v2))
    norm_a = math.sqrt(sum(x * x for x in v1))
    norm_b = math.sqrt(sum(x * x for x in v2))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot_product / (norm_a * norm_b)

def _compute_tfidf_vector(doc_tokens: List[str], idf: Dict[str, float]) -> Dict[str, float]:
    """Helper to compute TF-IDF weights for a document token list."""
    tf = Counter(doc_tokens)
    total_words = len(doc_tokens)
    if total_words == 0:
        return {}
    return {word: (count / total_words) * idf.get(word, 1.0) for word, count in tf.items()}

def _tfidf_similarity(vec1: Dict[str, float], vec2: Dict[str, float]) -> float:
    """Computes cosine similarity between two sparse TF-IDF dict vectors."""
    intersection = set(vec1.keys()) & set(vec2.keys())
    numerator = sum(vec1[x] * vec2[x] for x in intersection)
    sum1 = sum(val ** 2 for val in vec1.values())
    sum2 = sum(val ** 2 for val in vec2.values())
    denominator = math.sqrt(sum1) * math.sqrt(sum2)
    if not denominator:
        return 0.0
    return numerator / denominator

async def _fetch_gemini_embedding(text: str, api_key: str) -> Optional[List[float]]:
    """Calls Gemini Embeddings API to retrieve vector representation of text."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/text-embedding-004:embedContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {"content": {"parts": [{"text": text}]}}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=5.0) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data["embedding"]["values"]
    except Exception as e:
        print(f"[CACHE] Error fetching Gemini embedding: {e}")
    return None

async def _async_background_embedding(cache_key: str, prompt: str, api_key: str) -> None:
    """Asynchronously fetches embedding and updates DB cache row to avoid blocking caller."""
    try:
        embedding = await _fetch_gemini_embedding(prompt, api_key)
        if embedding:
            embedding_json = json.dumps(embedding)
            conn = get_connection(DB_FILE_PATH)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE llm_cache SET embedding = ? WHERE cache_key = ?",
                    (embedding_json, cache_key)
                )
                conn.commit()
            except Exception as e:
                print(f"[CACHE] Database background embedding update failed: {e}")
            finally:
                conn.close()
    except Exception as e:
        print(f"[CACHE] Background task failed: {e}")

async def get_cached_response(
    model: str,
    prompt: str,
    system_instructions: Optional[str] = None
) -> Optional[str]:
    """Retrieves cached LLM response using L1 exact-match or L2 semantic similarity.

    Args:
        model: Model name.
        prompt: LLM input prompt.
        system_instructions: Optional system instructions context.

    Returns:
        The cached completion string if found, otherwise None.
    """
    cache_key = _compute_sha256(model, prompt, system_instructions)
    now = int(time.time())

    # --- Level 1: Exact Match ---
    conn = get_connection(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT response, created_at, ttl_seconds FROM llm_cache WHERE cache_key = ?",
            (cache_key,)
        )
        row = cursor.fetchone()
        if row:
            response, created_at, ttl_seconds = row
            if created_at + ttl_seconds > now:
                print(f"[ROUTING: CACHE] L1 exact cache hit!")
                return response
    except Exception as e:
        print(f"[CACHE] L1 cache read failed: {e}")
    finally:
        conn.close()

    # --- Level 2: Semantic Vector / TF-IDF Fallback ---
    # Disable L2 similarity cache for structured, templated, or system-driver prompts
    # to prevent false positive collisions on boilerplate.
    is_structured = (
        "[System Instructions]" in prompt 
        or "[User Prompt]" in prompt 
        or "Given the user request:" in prompt
        or "You are a plan decomposer" in prompt
        or "You are executing Step" in prompt
    )
    if is_structured:
        return None

    conn = get_connection(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        # Retrieve all non-expired cached entries for the same model
        cursor.execute(
            "SELECT prompt, response, embedding FROM llm_cache WHERE model = ? AND (created_at + ttl_seconds) > ?",
            (model, now)
        )
        candidates = cursor.fetchall()
    except Exception as e:
        print(f"[CACHE] Candidate retrieval failed: {e}")
        candidates = []
    finally:
        conn.close()

    if not candidates:
        return None

    # Try embedding-based semantic matching
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        query_vector = await _fetch_gemini_embedding(prompt, api_key)
        if query_vector:
            best_match_response = None
            best_similarity = 0.0

            for cand_prompt, cand_response, cand_embedding_str in candidates:
                if not cand_embedding_str:
                    continue
                try:
                    cand_vector = json.loads(cand_embedding_str)
                    sim = _cosine_similarity(query_vector, cand_vector)
                    if sim > best_similarity:
                        best_similarity = sim
                        best_match_response = cand_response
                except Exception:
                    continue

            if best_similarity >= L2_SEMANTIC_THRESHOLD:
                print(f"[ROUTING: CACHE] L2 semantic cache hit! Similarity: {best_similarity:.4f}")
                return best_match_response

    # Fallback: TF-IDF Text Similarity
    query_tokens = _tokenize(prompt)
    if not query_tokens:
        return None

    candidate_docs = []
    for cand_prompt, cand_response, _ in candidates:
        tokens = _tokenize(cand_prompt)
        if tokens:
            candidate_docs.append((cand_prompt, cand_response, tokens))

    if not candidate_docs:
        return None

    # Compute IDF over query + candidates
    all_tokens_lists = [query_tokens] + [tokens for _, _, tokens in candidate_docs]
    N = len(all_tokens_lists)
    df = Counter()
    for tokens in all_tokens_lists:
        for word in set(tokens):
            df[word] += 1

    idf = {word: math.log(1.0 + (N / count)) for word, count in df.items()}
    query_vector = _compute_tfidf_vector(query_tokens, idf)

    best_match_response = None
    best_similarity = 0.0

    for cand_prompt, cand_response, tokens in candidate_docs:
        cand_vector = _compute_tfidf_vector(tokens, idf)
        sim = _tfidf_similarity(query_vector, cand_vector)
        if sim > best_similarity:
            best_similarity = sim
            best_match_response = cand_response

    if best_similarity >= L2_TFIDF_THRESHOLD:
        print(f"[ROUTING: CACHE] L2 TF-IDF fallback cache hit! Similarity: {best_similarity:.4f}")
        return best_match_response

    return None

async def set_cached_response(
    model: str,
    prompt: str,
    system_instructions: Optional[str],
    response: str,
    ttl_seconds: int = DEFAULT_TTL
) -> None:
    """Stores LLM response in cache and schedules asynchronous embedding generation.

    Args:
        model: Model name.
        prompt: LLM input prompt.
        system_instructions: Optional system instructions context.
        response: Generated text response.
        ttl_seconds: TTL duration in seconds.
    """
    cache_key = _compute_sha256(model, prompt, system_instructions)
    now = int(time.time())

    # Write exact-match cache entry instantly
    conn = get_connection(DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO llm_cache 
            (cache_key, model, prompt, system_instructions, response, created_at, ttl_seconds, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (cache_key, model, prompt, system_instructions, response, now, ttl_seconds)
        )
        conn.commit()
    except Exception as e:
        print(f"[CACHE] Cache write failed: {e}")
    finally:
        conn.close()

    # Trigger non-blocking background task to calculate embedding
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        asyncio.create_task(_async_background_embedding(cache_key, prompt, api_key))
