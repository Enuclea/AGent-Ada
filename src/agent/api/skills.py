import time
import os
import re as re_lib
from pathlib import Path, PurePosixPath
from typing import Optional
from fastapi import HTTPException, Request
from pydantic import BaseModel

# Global dictionary tracking request timestamps: {ip: [timestamps]}
LIMIT_DATA = {}

def check_rate_limit(ip: str, limit: int = 10, period: int = 60) -> bool:
    if os.environ.get("TESTING") == "1":
        return True
    now = time.time()
    times = LIMIT_DATA.setdefault(ip, [])
    times[:] = [t for t in times if now - t < period]
    if len(times) >= limit:
        return False
    times.append(now)
    return True

from agent.api.router import app
from agent import tools

class InstallSkillRequest(BaseModel):
    name: str
    description: str
    instructions: str
    author: Optional[str] = None
    version: Optional[str] = None

@app.get("/api/skills")
async def get_skills_endpoint():
    from agent.core.registry import tool_registry
    try:
        skills = tool_registry.discover_skills()
        return {"status": "success", "skills": [s.model_dump() for s in skills]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/skills/install")
async def install_skill_endpoint(req: InstallSkillRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip, limit=5, period=60):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    try:
        sanitized_name = re_lib.sub(r"[^a-zA-Z0-9_\-\s\.]", "", req.name)
        while ".." in sanitized_name:
            sanitized_name = sanitized_name.replace("..", ".")
        folder_name = sanitized_name.lower().replace(" ", "_")
        if not folder_name:
            raise ValueError("Invalid skill name after sanitization")
            
        # Secure directory validation using pathlib.PurePosixPath
        pure_path = PurePosixPath(folder_name)
        if ".." in pure_path.parts or pure_path.is_absolute() or len(pure_path.parts) != 1:
            raise ValueError("Skill path escapes the skills directory")
            
        skills_dir_resolved = tools.SKILLS_DIR.resolve()
        skill_dir = (tools.SKILLS_DIR / folder_name).resolve()
        
        # Double check containment
        if not str(skill_dir).startswith(str(skills_dir_resolved) + os.sep):
            raise ValueError("Skill path escapes the skills directory")
            
        skill_dir.mkdir(parents=True, exist_ok=True)
        
        fm_content = (
            f"---\n"
            f"name: \"{req.name}\"\n"
            f"description: \"{req.description}\"\n"
        )
        if req.author:
            fm_content += f"author: \"{req.author}\"\n"
        if req.version:
            fm_content += f"version: \"{req.version}\"\n"
        fm_content += f"---\n\n{req.instructions}"
        
        with open(skill_dir / "SKILL.md", "w", encoding="utf-8") as f:
            f.write(fm_content)
            
        return {"status": "success", "detail": f"Skill '{req.name}' successfully installed!"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/repo-skills")
async def get_repo_skills_endpoint():
    try:
        repo_skills = tools._find_repository_skills()
        skills_list = []
        for name, info in repo_skills.items():
            skills_list.append({
                "name": name,
                "type": info["type"],
                "description": info["description"]
            })
        return {"status": "success", "skills": skills_list}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/repo-skills/{name}/code")
async def get_repo_skill_code_endpoint(name: str):
    try:
        code = tools.view_repository_skill_code(name)
        if code.startswith("Error"):
            raise HTTPException(status_code=404, detail=code)
        return {"status": "success", "code": code}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/repo-skills/{name}/install")
async def install_repo_skill_endpoint(name: str, request: Request, paranoid: bool = True):
    ip = request.client.host if request.client else "unknown"
    if not check_rate_limit(ip, limit=5, period=60):
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")
    try:
        res = await tools.install_repository_skill(name, paranoid=paranoid, confirm=False)
        if res.startswith("Error"):
            raise HTTPException(status_code=400, detail=res)
        return {"status": "success", "detail": res}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
