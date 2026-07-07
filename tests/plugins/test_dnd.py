import json
import pytest
from pathlib import Path
from fastapi.testclient import TestClient
from agent.web import app

client = TestClient(app)
JSON_PATH = Path(__file__).parent.parent.parent / "src" / "agent" / "static" / "dnd_characters.json"

def test_dnd_characters_json_exists():
    assert JSON_PATH.exists(), "dnd_characters.json file does not exist"

def test_dnd_characters_count():
    with open(JSON_PATH, "r") as f:
        data = json.load(f)
    
    # We support both array and {"characters": [...]} schema
    characters = data if isinstance(data, list) else data.get("characters", [])
    assert len(characters) == 4, f"Expected exactly 4 characters, found {len(characters)}"

def test_dnd_characters_valid_stats():
    with open(JSON_PATH, "r") as f:
        data = json.load(f)
    
    characters = data if isinstance(data, list) else data.get("characters", [])
    required_stats = ["Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma"]
    
    for idx, char in enumerate(characters):
        assert "stats" in char, f"Character {idx} is missing 'stats'"
        stats = char["stats"]
        
        # Verify all stats are present
        for stat in required_stats:
            assert stat in stats, f"Character {idx} is missing stat '{stat}'"
            val = stats[stat]
            assert isinstance(val, int), f"Character {idx} stat '{stat}' is not an integer"
            assert 3 <= val <= 18, f"Character {idx} stat '{stat}' value {val} is out of bounds (3-18)"

def test_dnd_characters_stat_order():
    with open(JSON_PATH, "r") as f:
        data = json.load(f)
    
    characters = data if isinstance(data, list) else data.get("characters", [])
    expected_order = ["Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma"]
    
    for idx, char in enumerate(characters):
        stats = char["stats"]
        actual_order = list(stats.keys())
        assert actual_order == expected_order, f"Character {idx} stats keys order is incorrect: {actual_order} vs {expected_order}"

def test_dnd_characters_suggested_class_validity():
    with open(JSON_PATH, "r") as f:
        data = json.load(f)
    
    characters = data if isinstance(data, list) else data.get("characters", [])
    
    for idx, char in enumerate(characters):
        assert "suggested_class" in char, f"Character {idx} is missing 'suggested_class'"
        suggested = char["suggested_class"]
        stats = char["stats"]
        
        # Find highest attribute(s)
        max_val = max(stats.values())
        highest_stats = [k for k, v in stats.items() if v == max_val]
        
        if suggested == "Bard":
            assert stats["Charisma"] >= 10, f"Bard Charisma should be high, found {stats['Charisma']}"
        elif suggested == "Wizard":
            assert stats["Intelligence"] >= 10, f"Wizard Intelligence should be high, found {stats['Intelligence']}"
        elif suggested == "Paladin":
            assert stats["Charisma"] >= 10, f"Paladin Charisma is too low: {stats['Charisma']}"
            assert stats["Strength"] >= 10, f"Paladin Strength is too low: {stats['Strength']}"
        elif suggested == "Fighter" or suggested == "Barbarian":
            assert stats["Strength"] >= 10 or stats["Dexterity"] >= 10
        elif suggested == "Rogue" or suggested == "Ranger":
            assert stats["Dexterity"] >= 10
        elif suggested == "Cleric" or suggested == "Druid":
            assert stats["Wisdom"] >= 10

def test_modules_endpoint_dnd_assertions():
    response = client.get("/api/modules")
    assert response.status_code == 200
    data = response.json()
    assert "modules" in data
    modules = data["modules"]
    assert isinstance(modules, list)
    # Check if the dnd module is returned
    dnd_module = next((m for m in modules if m.get("id") == "dnd"), None)
    assert dnd_module is not None
    assert dnd_module["name"] == "D&D Character Rolls"
    assert dnd_module["enabled"] is True

def test_dnd_regenerate_endpoint():
    # Save the original file contents if it exists
    original_content = None
    if JSON_PATH.exists():
        original_content = JSON_PATH.read_text()
    
    try:
        response = client.post("/api/dnd/regenerate")
        assert response.status_code == 200
        data = response.json()
        assert "characters" in data
        characters = data["characters"]
        assert len(characters) == 4
        
        # Verify characters list structure
        for char in characters:
            assert "name" in char
            assert "stats" in char
            assert "suggested_class" in char
            assert isinstance(char["stats"], dict)
            assert len(char["stats"]) == 6
            
        # Verify the file was written to disk
        assert JSON_PATH.exists()
        written_data = json.loads(JSON_PATH.read_text())
        assert "characters" in written_data
        assert len(written_data["characters"]) == 4
        
    finally:
        # Restore original content to keep tests clean/hermetic
        if original_content is not None:
            JSON_PATH.write_text(original_content)
