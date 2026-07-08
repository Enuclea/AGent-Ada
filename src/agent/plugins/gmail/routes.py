from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from google_auth_oauthlib.flow import Flow

router = APIRouter(prefix="/api/gmail", tags=["gmail"])

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

@router.get("/auth")
async def gmail_auth(request: Request):
    """Initiates Gmail OAuth2 authentication flow."""
    from agent.plugins.gmail.tools import load_gmail_paths
    creds_path, _ = load_gmail_paths()
    
    if not creds_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Gmail credentials client_secrets JSON file not found at {creds_path}. "
                   f"Please place your Google client_secrets credentials.json there."
        )
        
    flow = Flow.from_client_secrets_file(
        str(creds_path),
        scopes=SCOPES,
        redirect_uri=request.url_for("gmail_oauth2callback")
    )
    
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')
    return RedirectResponse(auth_url)

@router.get("/oauth2callback")
async def gmail_oauth2callback(request: Request, code: str):
    """Callback endpoint for Google OAuth2 code exchange."""
    from agent.plugins.gmail.tools import load_gmail_paths
    creds_path, token_path = load_gmail_paths()
    
    if not creds_path.exists():
        raise HTTPException(status_code=404, detail="Credentials config missing during callback.")
        
    flow = Flow.from_client_secrets_file(
        str(creds_path),
        scopes=SCOPES,
        redirect_uri=request.url_for("gmail_oauth2callback")
    )
    
    try:
        flow.fetch_token(code=code)
        creds = flow.credentials
        
        # Ensure directories exist and save the token file
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())
        
        return HTMLResponse(
            content="""
            <html>
                <body style="font-family: sans-serif; text-align: center; padding-top: 100px; background: #0f172a; color: #f8fafc;">
                    <div style="max-width: 500px; margin: 0 auto; padding: 40px; border-radius: 8px; background: #1e293b; box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1);">
                        <h2 style="color: #10b981; margin-bottom: 20px;">Gmail Linked Successfully!</h2>
                        <p style="margin-bottom: 30px; line-height: 1.6;">Your token has been written back to your configuration path. You can close this tab and the Gmail sync worker will begin checking your inbox.</p>
                        <button onclick="window.close()" style="background: #10b981; color: white; border: none; padding: 10px 20px; border-radius: 4px; font-weight: bold; cursor: pointer;">Close Window</button>
                    </div>
                </body>
            </html>
            """
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Authentication callback failed: {e}")
