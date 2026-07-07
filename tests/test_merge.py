import pytest
from pathlib import Path
from unittest.mock import patch
from agent.merge import merge_text, merge_pure_python

def test_merge_no_conflict():
    base = "line 1\nline 2\nline 3\n"
    a = "line 1 modified\nline 2\nline 3\n"
    b = "line 1\nline 2\nline 3 modified\n"
    
    # Test diff3 (if available)
    merged, conflict = merge_text(base, a, b)
    assert not conflict
    assert merged == "line 1 modified\nline 2\nline 3 modified\n"
    
    # Test pure Python fallback
    merged_py, conflict_py = merge_pure_python(base, a, b, "HEAD", "specialist_b")
    assert not conflict_py
    assert merged_py == "line 1 modified\nline 2\nline 3 modified\n"

def test_merge_identical_edits():
    base = "line 1\nline 2\nline 3\n"
    a = "line 1\nline 2 modified\nline 3\n"
    b = "line 1\nline 2 modified\nline 3\n"
    
    merged, conflict = merge_text(base, a, b)
    assert not conflict
    assert merged == "line 1\nline 2 modified\nline 3\n"
    
    merged_py, conflict_py = merge_pure_python(base, a, b, "HEAD", "specialist_b")
    assert not conflict_py
    assert merged_py == "line 1\nline 2 modified\nline 3\n"

def test_merge_conflict():
    base = "line 1\nline 2\nline 3\n"
    a = "line 1\nline 2 modified by A\nline 3\n"
    b = "line 1\nline 2 modified by B\nline 3\n"
    
    merged, conflict = merge_text(base, a, b, "SPECIALIST_A", "SPECIALIST_B")
    assert conflict
    assert "<<<<<<< SPECIALIST_A" in merged
    assert "=======" in merged
    assert ">>>>>>> SPECIALIST_B" in merged
    
    merged_py, conflict_py = merge_pure_python(base, a, b, "SPECIALIST_A", "SPECIALIST_B")
    assert conflict_py
    assert "<<<<<<< SPECIALIST_A" in merged_py
    assert "=======" in merged_py
    assert ">>>>>>> SPECIALIST_B" in merged_py

def test_merge_pure_python_fallback_execution():
    # Force diff3 failure to test fallback path
    with patch("subprocess.run", side_effect=Exception("diff3 command failed")):
        base = "line 1\nline 2\nline 3\n"
        a = "line 1 modified\nline 2\nline 3\n"
        b = "line 1\nline 2\nline 3 modified\n"
        
        merged, conflict = merge_text(base, a, b)
        assert not conflict
        assert merged == "line 1 modified\nline 2\nline 3 modified\n"
