"""D&D Character Rolls Plugin."""
import asyncio
import json
import random
from pathlib import Path
from fastapi import FastAPI, HTTPException

def setup_plugin(app: FastAPI, register_tools, register_scheduled_task):
    @app.post("/api/dnd/regenerate")
    async def dnd_regenerate():
        names_pool = [
            "Zephyr", "Alaric", "Eowyn", "Sylas", "Valerius", "Lyra", "Kaelen", "Thorne",
            "Eldrin", "Seraphina", "Garrick", "Rowan", "Maeve", "Caelum", "Aurelia", "Baelor",
            "Dorian", "Elara", "Faelar", "Gideon", "Isolde", "Jaxon", "Kira", "Lucius", "Morgath"
        ]
        selected_names = random.sample(names_pool, 4)
        stats_order = ["Strength", "Dexterity", "Constitution", "Intelligence", "Wisdom", "Charisma"]
        class_mapping = {
            "Strength": "Fighter",
            "Dexterity": "Rogue",
            "Constitution": "Barbarian",
            "Intelligence": "Wizard",
            "Wisdom": "Cleric",
            "Charisma": "Bard"
        }
        
        def roll_4d6_drop_lowest():
            rolls = [random.randint(1, 6) for _ in range(4)]
            rolls.sort()
            return sum(rolls[1:])
            
        characters = []
        for i in range(4):
            char_stats = {stat: roll_4d6_drop_lowest() for stat in stats_order}
            highest_attr = max(stats_order, key=lambda s: char_stats[s])
            suggested_class = class_mapping[highest_attr]
            characters.append({
                "name": selected_names[i],
                "stats": char_stats,
                "suggested_class": suggested_class
            })
            
        output_data = {"characters": characters}
        static_file_path = Path(__file__).parent.parent.parent / "static" / "dnd_characters.json"
        
        def write_file():
            with open(static_file_path, "w") as f:
                json.dump(output_data, f, indent=2)
                
        try:
            await asyncio.to_thread(write_file)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to write dnd_characters.json: {e}")
            
        return output_data
