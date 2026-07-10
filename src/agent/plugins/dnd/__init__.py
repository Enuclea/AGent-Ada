"""D&D Character Rolls Plugin.

Self-contained plugin — static assets (JS/CSS/module descriptor) live within
the plugin directory and are served via authenticated API routes. Character
roll data is held in memory only — no disk writes, no persistence, no
writable directories that could become injection/staging points.
"""
import json
import random
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse

# Plugin-local paths (immutable, part of signed package)
PLUGIN_DIR = Path(__file__).parent
STATIC_DIR = PLUGIN_DIR / "static"

# In-memory character state — no disk writes
_current_characters: dict = {}


def setup_plugin(app: FastAPI, register_tools, register_scheduled_task):
    """Register all DND routes on the main app (inherits auth)."""

    # --- Data API Routes ---

    @app.get("/api/dnd/characters")
    async def dnd_get_characters():
        """Return current character data. Authenticated via app dependency."""
        global _current_characters
        if not _current_characters:
            _current_characters = _generate_characters()
        return JSONResponse(content=_current_characters)

    @app.post("/api/dnd/regenerate")
    async def dnd_regenerate():
        """Regenerate random characters. Authenticated via app dependency.
        
        Data is generated entirely server-side (no caller input accepted)
        and held in memory only. No disk writes.
        """
        global _current_characters
        _current_characters = _generate_characters()
        return _current_characters

    # --- Static Asset Routes (plugin-served, authenticated) ---

    @app.get("/api/plugins/dnd/static/{filename:path}")
    async def dnd_static(filename: str):
        """Serve plugin static assets. Authenticated via app dependency."""
        file_path = (STATIC_DIR / filename).resolve()
        # Path traversal guard
        if not str(file_path).startswith(str(STATIC_DIR.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
        if not file_path.exists():
            raise HTTPException(status_code=404, detail=f"Static file not found: {filename}")
        # Determine content type
        suffix = file_path.suffix.lower()
        media_types = {
            ".js": "application/javascript",
            ".css": "text/css",
            ".json": "application/json",
        }
        return FileResponse(file_path, media_type=media_types.get(suffix, "application/octet-stream"))

    # --- Module Registration ---
    register_tools({
        "name": "dnd_module_info",
        "description": "Returns the DND module descriptor for dashboard widget registration.",
        "module_info": {
            "name": "D&D Character Rolls",
            "id": "dnd",
            "position": "sidebar",
            "iconClass": "fa-solid fa-dice-d20",
            "widgetJs": "/api/plugins/dnd/static/widget.js",
            "widgetCss": "/api/plugins/dnd/static/widget.css",
        }
    })


def _generate_characters() -> dict:
    """Generate 4 random D&D characters with 4d6-drop-lowest stats.
    
    All data is generated server-side. No caller input is accepted,
    preventing injection via the regenerate endpoint.
    """
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
        rolls = sorted([random.randint(1, 6) for _ in range(4)])
        return sum(rolls[1:])

    characters = []
    for i in range(4):
        char_stats = {stat: roll_4d6_drop_lowest() for stat in stats_order}
        highest_attr = max(stats_order, key=lambda s: char_stats[s])
        characters.append({
            "name": selected_names[i],
            "stats": char_stats,
            "suggested_class": class_mapping[highest_attr]
        })

    return {"characters": characters}
