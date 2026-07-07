from agent import memory

def record_memory_fact(fact: str) -> str:
    """Records a new fact, note, or piece of knowledge about the user, project, or task.
    
    Use this to store important context that should be remembered across runs, such as
    user preferences, project details, build commands, or lessons learned.
    
    Args:
        fact: The text of the fact to record. E.g. "The build command is 'npm run build'" or "User prefers dark mode".
    """
    return memory.add_fact(fact)

def record_memory_key_value(key: str, value: str) -> str:
    """Stores or updates a key-value setting or preference in persistent memory.
    
    Args:
        key: The key identifier (e.g. 'user_name', 'project_status').
        value: The value to store associated with the key.
    """
    return memory.update_key_value(key, value)

def search_past_conversations(query: str) -> str:
    """Searches past conversation logs and transcripts using full-text search (FTS5).
    
    Use this to recall past tasks, solutions, commands, and conversations.
    
    Args:
        query: The search query terms (e.g. 'black formatting', 'test_hang.py', or 'welcome nickname').
    """
    results = memory.search_conversations(query)
    if not results:
        return f"No matches found for query: '{query}'"
        
    lines = [f"Found {len(results)} matches for query '{query}':"]
    # Group results by session id to present them cleanly
    by_session = {}
    for res in results:
        by_session.setdefault(res["session_id"], []).append(res)
        
    for sess_id, steps in by_session.items():
        lines.append(f"\n--- Session: {sess_id} ---")
        for step in steps:
            role = step["role"].upper()
            content = step["content"].strip()
            # Truncate content to avoid too long outputs
            if len(content) > 300:
                content = content[:300] + "... [truncated]"
            if step["tool_name"]:
                lines.append(f"  [{role}] Tool Call: {step['tool_name']}({content})")
            else:
                lines.append(f"  [{role}]: {content}")
                
    return "\n".join(lines)

def record_roleplay_memory(key: str, fact: str) -> str:
    """Saves an important FFXIV roleplay fact, detail, or memory about a person, place, or event in the current session.
    
    Use this to help Ada remember things users tell her, their preferences, debts, or historical events in the bar.
    
    Args:
        key: The person, subject, or topic of the memory (e.g. 'The Lady', 'Gilgamesh', 'Mead', 'Bar Rules').
        fact: The specific detail to remember (e.g. 'Enjoys chamomile tea and hates ale', 'Owes 100 gil').
    """
    session_id = getattr(memory, "active_roleplay_session_id", None) or "global-roleplay"
    memory.add_roleplay_memory(session_id, key, fact)
    return f"Ada has noted and remembered that {key}: {fact}"
