from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/api/playwright", tags=["playwright"])

# Persistent directory for screenshots (under /data volume)
SCREENSHOTS_DIR = Path("/data/screenshots")

def ensure_screenshots_dir():
    global SCREENSHOTS_DIR
    try:
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        # Fallback to local /tmp/screenshots if /data is not writable
        SCREENSHOTS_DIR = Path("/tmp/screenshots")
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

ensure_screenshots_dir()

@router.get("/screenshot/{filename}")
async def get_screenshot(filename: str):
    # Sanitize the filename to prevent directory traversal
    safe_name = Path(filename).name
    file_path = SCREENSHOTS_DIR / safe_name
    
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Screenshot not found.")
        
    return FileResponse(file_path)
