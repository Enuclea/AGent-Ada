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
    
    # 3. Register standard scheduled task handler and schedule it (check every 10 minutes)
    from agent.core.plugins import register_scheduled_task_handler
    
    async def gmail_sync_handler(prompt: str):
        print("[PLUGINS: Gmail] Executing scheduled Gmail sync...")
        await sync_gmail_emails()
        
    register_scheduled_task_handler("sync_gmail_emails", gmail_sync_handler)
    register_scheduled_task("sync_gmail_emails", "Sync Gmail emails since last check.", "*/10 * * * *")
    print("[PLUGINS: Gmail] Scheduled periodic inbox check every 10 minutes.")
