"""Playwright Browser Automation Plugin."""
from fastapi import FastAPI
from agent.plugins.playwright.routes import router
from agent.plugins.playwright.tools import playwright_browse_url

def setup_plugin(app: FastAPI, register_tools, register_scheduled_task):
    """Setup contract called by plugins loader at application startup."""
    # 1. Register API router to serve screenshots
    app.include_router(router)
    print("[PLUGINS: Playwright] Registered routes.")
    
    # 2. Register tools for agent use
    register_tools([playwright_browse_url])
    print("[PLUGINS: Playwright] Registered playwright_browse_url tool.")
