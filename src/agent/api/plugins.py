import os
import json
from pathlib import Path
from typing import Optional, Dict, List, Any
from fastapi import HTTPException
from pydantic import BaseModel

from agent.api.router import app
from agent import tools

class PlatformConfigRequest(BaseModel):
    routes: Dict[str, Dict[str, Any]]
    plugins: Dict[str, bool]
    skills: Dict[str, bool]

@app.get("/api/config/platform")
async def get_platform_config():
    db_path = os.environ.get("AGENT_DB_PATH")
    if db_path:
        config_path = Path(db_path).parent / "platform_config.json"
    else:
        config_path = Path(os.getcwd()) / "data" / "platform_config.json"
        
    config_data = {}
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception:
            pass
            
    config_data.setdefault("routes", {})
    config_data.setdefault("plugins", {})
    config_data.setdefault("skills", {})

    from agent.core.routing import routing_engine
    available_routes = []
    for r in routing_engine.routes.values():
        available_routes.append({
            "name": r.name,
            "type": "custom" if "custom" in str(r.__class__.__module__) else "built-in",
            "default_status": r.default_status.value,
            "default_priority": r.default_priority
        })

    from agent.core.plugins import plugin_manager
    plugin_manager.discover_plugins()
    available_plugins = []
    for name, plugin in plugin_manager.plugins.items():
        available_plugins.append({
            "name": name,
            "path": str(plugin.path)
        })

    from agent.core.registry import tool_registry
    available_skills = []
    workspace_skills = Path(os.getcwd()) / ".agents" / "skills"
    skills_paths = [tools.SKILLS_DIR]
    if workspace_skills.exists() and workspace_skills.is_dir():
        skills_paths.append(workspace_skills)
        
    seen_paths = set()
    for path in skills_paths:
        if path.exists() and path.is_dir():
            for folder in path.iterdir():
                if folder.is_dir():
                    folder_resolved = str(folder.resolve())
                    if folder_resolved in seen_paths:
                        continue
                    seen_paths.add(folder_resolved)
                    skill_md = folder / "SKILL.md"
                    if skill_md.exists():
                        try:
                            with open(skill_md, "r", encoding="utf-8") as f:
                                content = f.read()
                            fm = tools._parse_frontmatter(content)
                            available_skills.append({
                                "name": fm.get("name", folder.name),
                                "description": fm.get("description", "No description.")
                            })
                        except Exception:
                            pass

    return {
        "status": "success",
        "config": config_data,
        "available_routes": available_routes,
        "available_plugins": available_plugins,
        "available_skills": available_skills
    }

@app.post("/api/config/platform")
async def save_platform_config(req: PlatformConfigRequest):
    db_path = os.environ.get("AGENT_DB_PATH")
    if db_path:
        config_path = Path(db_path).parent / "platform_config.json"
    else:
        config_path = Path(os.getcwd()) / "data" / "platform_config.json"
        
    config_path.parent.mkdir(parents=True, exist_ok=True)
    
    payload = {
        "routes": req.routes,
        "plugins": req.plugins,
        "skills": req.skills
    }
    
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            
        from agent.core.registry import tool_registry
        tool_registry._skills.clear()
        tool_registry.discover_skills()
        
        from agent.core.plugins import plugin_manager
        plugin_manager.reset()
        plugin_manager.load_plugins(app)
        
        return {"status": "success", "message": "Configuration saved and hot-reloaded successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save configuration: {e}")
