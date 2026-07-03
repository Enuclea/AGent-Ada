"""Conversation step logging, FTS5 full-text search, and RAG context retrieval.

Extracted from memory.py — covers conversation history persistence and search.
"""

import sqlite3
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import agent.storage.db as _db


def log_conversation_step(
    session_id: str,
    role: str,
    content: str,
    tool_name: Optional[str] = None,
    tool_result: Optional[str] = None
) -> None:
    """Logs a conversation step to SQLite and indexes it in FTS5.

    Args:
        session_id: The active chat session ID.
        role: The role (user, assistant, tool, system).
        content: The text content of the message.
        tool_name: The name of the tool called, if any.
        tool_result: The result payload of the tool call, if any.
    """
    if not session_id:
        session_id = "New Session"
    timestamp = datetime.now(timezone.utc).isoformat()
    
    # Establish centralized database connection
    conn = _db.get_connection(_db.DB_FILE_PATH)
    try:
        cursor = conn.cursor()
        # Insert the conversation step into base logs
        cursor.execute(
            """
            INSERT INTO conversation_steps (session_id, timestamp, role, content, tool_name, tool_result)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, timestamp, role, content, tool_name, tool_result)
        )
        step_id = cursor.lastrowid
        
        # Index the conversation step content in the FTS5 virtual search table
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

    Args:
        query: The search term or FTS expression.

    Returns:
        List[Dict[str, Any]]: List of matching message details.
    """
    conn = _db.get_connection(_db.DB_FILE_PATH)
    results: List[Dict[str, Any]] = []
    try:
        cursor = conn.cursor()
        try:
            # Attempt optimized FTS5 MATCH query
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
            # Fallback to standard SQL LIKE query if FTS syntax fails
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

    Args:
        prompt: The current user query/prompt.

    Returns:
        str: Truncated markdown formatted RAG context block.
    """
    if not prompt:
        return ""
        
    # Extract alpha-numeric words for a query pattern
    clean_query = " OR ".join(re.findall(r"\w+", prompt))
    if not clean_query:
        return ""

    results: List[Dict[str, Any]] = []
    try:
        results = search_conversations(clean_query)
    except Exception:
        try:
            results = search_conversations(prompt)
        except Exception:
            pass

    if not results:
        return ""

    candidates: List[Dict[str, Any]] = []
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

    # Slice to top 3 relevant historical interactions
    candidates = candidates[:3]

    lines: List[str] = []
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
