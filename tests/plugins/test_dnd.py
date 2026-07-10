"""Tests for the D&D Character Rolls Plugin.

Tests use the authenticated API routes. The plugin stores character data
in memory only (no disk writes), so tests verify API behavior, not files.
"""
import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from agent.web import app

client = TestClient(app)


def _get_characters_from_api():
    """Fetch characters from the authenticated API endpoint."""
    response = client.get("/api/dnd/characters")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    return data if isinstance(data, list) else data.get("characters", [])


def test_dnd_characters_api_returns_data():
    """Characters endpoint returns valid data."""
    characters = _get_characters_from_api()
    assert len(characters) == 4, f"Expected exactly 4 characters, found {len(characters)}"


def test_dnd_characters_valid_stats():
    """Each character has all 6 stats with valid values."""
    characters = _get_characters_from_api()
    required_stats = ["Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma"]
    
    for idx, char in enumerate(characters):
        assert "stats" in char, f"Character {idx} is missing 'stats'"
        stats = char["stats"]
        
        for stat in required_stats:
            assert stat in stats, f"Character {idx} is missing stat '{stat}'"
            val = stats[stat]
            assert isinstance(val, int), f"Character {idx} stat '{stat}' is not an integer"
            assert 3 <= val <= 18, f"Character {idx} stat '{stat}' value {val} is out of bounds (3-18)"


def test_dnd_characters_stat_order():
    """Stats keys are in the canonical order."""
    characters = _get_characters_from_api()
    expected_order = ["Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma"]
    
    for idx, char in enumerate(characters):
        stats = char["stats"]
        actual_order = list(stats.keys())
        assert actual_order == expected_order, f"Character {idx} stats keys order is incorrect: {actual_order} vs {expected_order}"


def test_dnd_characters_suggested_class_validity():
    """Suggested class is reasonable given highest stat."""
    characters = _get_characters_from_api()
    
    for idx, char in enumerate(characters):
        assert "suggested_class" in char, f"Character {idx} is missing 'suggested_class'"
        suggested = char["suggested_class"]
        stats = char["stats"]
        
        if suggested == "Bard":
            assert stats["Charisma"] >= 10, f"Bard Charisma should be high, found {stats['Charisma']}"
        elif suggested == "Wizard":
            assert stats["Intelligence"] >= 10, f"Wizard Intelligence should be high, found {stats['Intelligence']}"
        elif suggested in ("Fighter", "Barbarian"):
            assert stats["Strength"] >= 10 or stats["Dexterity"] >= 10
        elif suggested in ("Rogue", "Ranger"):
            assert stats["Dexterity"] >= 10
        elif suggested in ("Cleric", "Druid"):
            assert stats["Wisdom"] >= 10


def test_dnd_regenerate_endpoint():
    """POST /api/dnd/regenerate returns new characters (in memory only)."""
    response = client.post("/api/dnd/regenerate")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
    data = response.json()
    assert "characters" in data
    characters = data["characters"]
    assert len(characters) == 4
    
    for char in characters:
        assert "name" in char
        assert "stats" in char
        assert "suggested_class" in char
        assert isinstance(char["stats"], dict)
        assert len(char["stats"]) == 6


def test_dnd_regenerate_changes_data():
    """Regenerate produces different characters each time (probabilistic)."""
    response1 = client.post("/api/dnd/regenerate")
    data1 = response1.json()
    response2 = client.post("/api/dnd/regenerate")
    data2 = response2.json()
    # With 25 names and random stats, identical rolls are astronomically unlikely
    names1 = [c["name"] for c in data1["characters"]]
    names2 = [c["name"] for c in data2["characters"]]
    assert names1 != names2 or data1 != data2, "Two consecutive regenerations should differ"


def test_dnd_static_assets_served():
    """Plugin serves its own static assets via authenticated routes."""
    for filename in ["widget.js", "widget.css", "module.json"]:
        response = client.get(f"/api/plugins/dnd/static/{filename}")
        assert response.status_code == 200, f"Static asset {filename} not served: {response.status_code}"


def test_dnd_static_path_traversal_blocked():
    """Path traversal attempts on static assets are blocked."""
    response = client.get("/api/plugins/dnd/static/../../../__init__.py")
    # FastAPI normalizes the path before it reaches our handler (404),
    # or our handler catches it explicitly (403). Both are safe.
    assert response.status_code in (403, 404), f"Expected 403 or 404 for path traversal, got {response.status_code}"


def test_dnd_no_disk_writes():
    """Plugin does not write to disk — no characters.json in plugin tree."""
    plugin_data = Path(__file__).parent.parent.parent / "src" / "agent" / "plugins" / "dnd" / "data"
    assert not plugin_data.exists(), "Plugin should NOT have a data/ directory — runtime data is in-memory only"
