"""Gmail Integration Plugin."""
from fastapi import FastAPI

def setup_plugin(app: FastAPI, register_tools, register_scheduled_task):
    """Setup contract called by plugins loader at application startup."""
    from agent.plugins.gmail.routes import router
    from agent.plugins.gmail.tools import sync_gmail_emails
    
    # 1. Register OAuth endpoints
    app.include_router(router)
    print("[PLUGINS: Gmail] Registered OAuth authentication routes.")
    
    # 2. Register tools for agent use
    register_tools([sync_gmail_emails])
    print("[PLUGINS: Gmail] Registered sync_gmail_emails tool.")
    
    # 3. Register standard scheduled task (check every 10 minutes)
    register_scheduled_task("sync_gmail_emails", 10, sync_gmail_emails)
    print("[PLUGINS: Gmail] Scheduled periodic inbox check every 10 minutes.")
