import tempfile
from pathlib import Path
from unittest import mock
import pytest

from agent import memory

@pytest.fixture
def temp_memory_file():
    """Fixture that redirects MEMORY_FILE_PATH to a temporary file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir) / "memory.json"
        with mock.patch("agent.memory.MEMORY_FILE_PATH", tmp_path):
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
    
    # Write more than 100 active tasks
    for i in range(115):
        memory.add_active_task(f"task-{i}", "test", f"details-{i}")
        memory.update_active_task_status(f"task-{i}", "completed")
        
    stats = memory.compact_all_memories()
    
    assert stats["memory_json_before_facts"] == 4
    assert stats["memory_json_after_facts"] == 2 # "double fact" (exact lowercase match) and "Short Fact" (subset of longer) pruned
    assert stats["active_tasks_before"] == 115
    assert stats["active_tasks_after"] == 100 # capped at 100


