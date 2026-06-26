import tempfile
from pathlib import Path
from unittest import mock
import pytest

from agent import memory

@pytest.fixture
def temp_memory_file():
    """Fixture that redirects MEMORY_FILE_PATH and DB_FILE_PATH to temporary files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "memory.json"
        tmp_db_path = Path(tmpdir) / "history.db"
        with mock.patch("agent.memory.MEMORY_FILE_PATH", tmp_path), \
             mock.patch("agent.memory.DB_FILE_PATH", tmp_db_path):
            memory.init_db()
            yield tmp_path

def test_load_memory_empty(temp_memory_file):
    """Test loading memory when the file does not exist."""
    data = memory.load_memory()
    assert data == {"facts": [], "key_value": {}}

def test_add_fact(temp_memory_file):
    """Test adding facts to memory."""
    res1 = memory.add_fact("User is developing AGent")
    assert "Successfully added fact" in res1
    
    # Check loading
    data = memory.load_memory()
    assert "User is developing AGent" in data["facts"]
    
    # Try adding duplicate
    res2 = memory.add_fact("User is developing AGent")
    assert "Fact already exists" in res2
    assert len(data["facts"]) == 1

def test_update_key_value(temp_memory_file):
    """Test setting and updating key-value entries in memory."""
    res = memory.update_key_value("user_name", "Dan")
    assert "Successfully set memory key" in res
    
    data = memory.load_memory()
    assert data["key_value"]["user_name"] == "Dan"

def test_get_fact_summary(temp_memory_file):
    """Test generating a text summary of persistent memory."""
    # Empty memory should yield empty summary
    assert memory.get_fact_summary() == ""
    
    memory.add_fact("Facts are cool")
    memory.update_key_value("mode", "fast")
    
    summary = memory.get_fact_summary()
    assert "[PERSISTENT MEMORY FROM PAST SESSIONS]" in summary
    assert "Facts are cool" in summary
    assert "mode: fast" in summary
    assert "[END OF PERSISTENT MEMORY]" in summary

@pytest.fixture
def temp_db_file():
    """Fixture that redirects DB_FILE_PATH to a temporary sqlite file and initializes it."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "history.db"
        with mock.patch("agent.memory.DB_FILE_PATH", tmp_path):
            memory.init_db()
            yield tmp_path

def test_sqlite_logging_and_search(temp_db_file):
    """Test logging a conversation step and searching it via FTS5."""
    res_empty = memory.search_conversations("hello")
    assert len(res_empty) == 0
    
    memory.log_conversation_step(
        session_id="test_sess_123",
        role="user",
        content="I am doing a unit test on FTS5 search",
        tool_name=None
    )
    
    memory.log_conversation_step(
        session_id="test_sess_123",
        role="tool_call",
        content="{'arg1': 'val1'}",
        tool_name="test_tool"
    )
    
    results = memory.search_conversations("unit test")
    assert len(results) == 1
    assert results[0]["session_id"] == "test_sess_123"
    assert results[0]["role"] == "user"
    assert "FTS5 search" in results[0]["content"]
    
    results_tool = memory.search_conversations("test_tool")
    assert len(results_tool) == 1
    assert results_tool[0]["role"] == "tool_call"
    assert results_tool[0]["tool_name"] == "test_tool"

def test_compact_all_memories(temp_memory_file, temp_db_file):
    """Test the complete compaction routine."""
    # Write some duplicate/redundant standard facts to memory.json
    memory.add_fact("Double Fact")
    # Duplicate addition check is handled inside add_fact, let's bypass it and write directly
    mem = memory.load_memory()
    mem["facts"] = ["Double Fact", "double fact", "Short Fact", "Short Fact that is much longer now and more descriptive"]
    memory.save_memory(mem)
    
    # Write some duplicate / redundant memories to sqlite roleplay table
    memory.add_roleplay_memory("discord-roleplay-1518087367465111594", "Key", "Fact") # main bar
    memory.add_roleplay_memory("discord-roleplay-other", "Key", "Fact") # other session with identical fact (redundant)
    memory.add_roleplay_memory("discord-roleplay-other", "Key", "Fact") # duplicate in same session
    
    # Write more than 100 active tasks
    for i in range(115):
        memory.add_active_task(f"task-{i}", "test", f"details-{i}")
        memory.update_active_task_status(f"task-{i}", "completed")
        
    stats = memory.compact_all_memories()
    
    assert stats["memory_json_before_facts"] == 4
    assert stats["memory_json_after_facts"] == 2 # "double fact" (exact lowercase match) and "Short Fact" (subset of longer) pruned
    assert stats["roleplay_memories_before"] == 3
    assert stats["roleplay_memories_after"] == 1 # only main bar is kept because "other" is identical
    assert stats["active_tasks_before"] == 115
    assert stats["active_tasks_after"] == 100 # capped at 100

def test_global_backstory_roleplay_memories(temp_db_file):
    """Test that Ada's backstory and lore memories are retrieved globally across session IDs."""
    # Add a memory about Ada's past in a specific session
    memory.add_roleplay_memory("discord-roleplay-session-1", "Ada's Past - Childhood", "She grew up in the Coerthas Western Highlands.")
    memory.add_roleplay_memory("discord-roleplay-session-1", "Ada's History", "Her uncle Octavian taught her archery.")
    memory.add_roleplay_memory("discord-roleplay-session-1", "Ada's Lore", "She traveled to Gridania once.")
    # Add standard memory that should not be global
    memory.add_roleplay_memory("discord-roleplay-session-1", "Tavern Rules", "No fighting allowed.")

    # Retrieve memories for a completely different session
    results = memory.get_roleplay_memories("discord-roleplay-session-2")
    
    # Verify that the global backstory memories are present
    keys = [r["key"] for r in results]
    assert "Ada's Past - Childhood" in keys
    assert "Ada's History" in keys
    assert "Ada's Lore" in keys
    # Verify that standard non-global memory is not present
    assert "Tavern Rules" not in keys


@pytest.mark.anyio
async def test_get_auto_rag_context(temp_db_file):
    """Test retrieving Auto-RAG context dynamically using FTS search."""
    # Initially should be empty
    assert await memory.get_auto_rag_context("FTS5") == ""

    # Log some steps
    memory.log_conversation_step(
        session_id="session_auto_rag",
        role="user",
        content="I am doing a unit test on FTS5 search",
        tool_name=None
    )
    memory.log_conversation_step(
        session_id="session_auto_rag",
        role="assistant",
        content="FTS5 search works wonderfully in SQLite.",
        tool_name=None
    )

    # Search for FTS5
    context = await memory.get_auto_rag_context("FTS5")
    assert "[AUTO-RAG: RELEVANT HISTORICAL INTERACTIONS]" in context
    assert "FTS5 search works wonderfully" in context or "unit test on FTS5" in context


