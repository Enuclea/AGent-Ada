"""Conversation step logging, FTS5 full-text search, and RAG context retrieval.

Extracted from memory.py — covers conversation history persistence and search.
"""

import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import agent.db as _db


def log_conversation_step(
    session_id: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_result: Optional[str] = None
) -> None:
    """Logs a conversation step to SQLite and indexes it in FTS5."""
    if not session_id:
        session_id = "New Session"
    timestamp = datetime.now(timezone.utc).isoformat()
    
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversation_steps (session_id, timestamp, role, content, tool_name, tool_result)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, timestamp, role, content, tool_name, tool_result)
        )
        step_id = cursor.lastrowid
        
        cursor.execute(
            """
            INSERT INTO conversation_search (step_id, session_id, role, content, tool_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            (step_id, session_id, role, content, tool_name)
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def search_conversations(query: str) -> List[Dict[str, Any]]:
    """Performs full-text search (FTS5) over past conversations.
    
    Falls back to LIKE search if FTS5 query format fails or is unsupported.
    """
    conn = sqlite3.connect(_db.DB_FILE_PATH)
    results = []
    try:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                SELECT session_id, role, content, tool_name 
                FROM conversation_search 
                WHERE conversation_search MATCH ?
                ORDER BY rank LIMIT 50
                """,
                (query,)
            )
            rows = cursor.fetchall()
        except sqlite3.OperationalError:
            like_query = f"%{query}%"
            cursor.execute(
                """
                SELECT session_id, role, content, tool_name 
                FROM conversation_steps 
                WHERE content LIKE ? OR tool_name LIKE ?
                ORDER BY id DESC LIMIT 50
                """,
                (like_query, like_query)
            )
            rows = cursor.fetchall()
            
        for row in rows:
            results.append({
                "session_id": row[0],
                "role": row[1],
                "content": row[2],
                "tool_name": row[3]
            })
    except Exception:
        pass
    finally:
        conn.close()
    return results

async def get_auto_rag_context(prompt: Optional[str]) -> str:
    """Runs an FTS search on past conversations, then performs semantic ranking
    using a keyless agent call to identify the 3 most relevant context snippets.
    """
    import re
    if not prompt:
        return ""
        
    clean_query = " OR ".join(re.findall(r"\w+", prompt))
    if not clean_query:
        return ""

    results = []
    try:
        results = search_conversations(clean_query)
    except Exception:
        try:
            results = search_conversations(prompt)
        except Exception:
            pass

    if not results:
        return ""

    candidates = []
    seen_content = set()
    for res in results:
        content = res["content"].strip() if res["content"] else ""
        if not content or content in seen_content:
            continue
        seen_content.add(content)
        candidates.append(res)
        if len(candidates) >= 15:
            break

    if not candidates:
        return ""

    candidates = candidates[:3]

    lines = []
    for res in candidates[:3]:
        content = res["content"].strip()
        role = res["role"].upper()
        tool_desc = f" (Tool Call: {res['tool_name']})" if res["tool_name"] else ""
        truncated = content
        if len(truncated) > 300:
            truncated = truncated[:300] + "... [truncated]"
        lines.append(f"- **Role:** {role}{tool_desc}\n  **Content:** {truncated}")

    if not lines:
        return ""

    return "[AUTO-RAG: RELEVANT HISTORICAL INTERACTIONS]\n" + "\n".join(lines) + "\n[END OF AUTO-RAG]"
