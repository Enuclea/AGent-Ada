import json
import pytest
from pathlib import Path

JSON_PATH = Path(__file__).parent.parent / "src" / "agent" / "static" / "dnd_characters.json"

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
    
    # Define mapping of high stats to sensible classes
    # e.g. Strength -> Fighter/Barbarian/Paladin, Dexterity -> Rogue/Ranger, Intelligence -> Wizard, Wisdom -> Cleric/Druid, Charisma -> Bard/Sorcerer/Warlock
    for idx, char in enumerate(characters):
        assert "suggested_class" in char, f"Character {idx} is missing 'suggested_class'"
        suggested = char["suggested_class"]
        stats = char["stats"]
        
        # Find highest attribute(s)
        max_val = max(stats.values())
        highest_stats = [k for k, v in stats.items() if v == max_val]
        
        # Check if the class suggestion is sensible given the highest stats or at least major stats
        # Bard needs Charisma, Wizard needs Intelligence, Rogue needs Dexterity, etc.
        if suggested == "Bard":
            # Bard's main stat is Charisma. It should be relatively high (at least >= 10, ideally highest or close to it)
            assert stats["Charisma"] >= 10, f"Bard Charisma should be high, found {stats['Charisma']}"
        elif suggested == "Wizard":
            # Wizard's main stat is Intelligence.
            assert stats["Intelligence"] >= 10, f"Wizard Intelligence should be high, found {stats['Intelligence']}"
        elif suggested == "Paladin":
            # Paladin needs Strength and Charisma.
            assert stats["Charisma"] >= 10, f"Paladin Charisma is too low: {stats['Charisma']}"
            assert stats["Strength"] >= 10, f"Paladin Strength is too low: {stats['Strength']}"
        elif suggested == "Fighter" or suggested == "Barbarian":
            assert stats["Strength"] >= 10 or stats["Dexterity"] >= 10
        elif suggested == "Rogue" or suggested == "Ranger":
            assert stats["Dexterity"] >= 10
        elif suggested == "Cleric" or suggested == "Druid":
            assert stats["Wisdom"] >= 10
