import json
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List, Optional

CONFIG_FILE_PATH = Path(__file__).parent / "config.json"

def load_config() -> Dict[str, Any]:
    """Loads the configuration from the central AGent server, falling back to config.json."""
    api_base = "http://127.0.0.1:8051"
    if CONFIG_FILE_PATH.exists():
        try:
            with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
                api_base = json.load(f).get("agent_api_base", api_base)
        except Exception:
            pass
    try:
        req = urllib.request.Request(
            f"{api_base}/api/discord/config",
            method="GET"
        )
        with urllib.request.urlopen(req, timeout=1.0) as response:
            if response.status == 200:
                data = json.loads(response.read().decode("utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception:
        pass

    # Reliable local fallback
    if not CONFIG_FILE_PATH.exists():
        return {
            "default_model": "gemini-3.5-flash",
            "channels": {}
        }
    try:
        with open(CONFIG_FILE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "default_model": "gemini-3.5-flash",
            "channels": {}
        }

def save_config(config: Dict[str, Any]) -> None:
    """Saves the configuration to config.json and pushes to the central AGent server."""
    # Write local fallback first
    try:
        with open(CONFIG_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Error saving local config fallback: {e}")

    # Central brokered API push
    api_base = config.get("agent_api_base", "http://127.0.0.1:8051")
    try:
        path = "/api/discord/config"
        payload = json.dumps({"config_data": config}).encode("utf-8")
        headers = get_auth_headers("POST", path, body=payload)
        headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{api_base}{path}",
            data=payload,
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=1.0) as response:
            pass
    except Exception:
        pass

def get_channel_config(channel_id: str) -> Optional[Dict[str, Any]]:
    """Retrieves config for a specific channel ID (string)."""
    config = load_config()
    return config.get("channels", {}).get(str(channel_id))

def is_channel_configured(channel_id: str) -> bool:
    """Checks if a channel ID is configured in config.json."""
    return get_channel_config(channel_id) is not None

def check_channel_permissions(
    channel_id: str, 
    user_roles: List[str], 
    user_id: str
) -> bool:
    """
    Checks if a user has permission to use AGent in a channel.
    Matches user's active role names or their user_id against the channel's allowed list.
    """
    chan_cfg = get_channel_config(channel_id)
    if not chan_cfg:
        return False
    
    allowed_users = chan_cfg.get("allowed_users", [])
    if str(user_id) in allowed_users or user_id in allowed_users:
        return True
        
    allowed_roles = chan_cfg.get("allowed_roles", [])
    if "@everyone" in allowed_roles:
        return True
        
    # Check if any user role matches allowed roles (case-insensitive check)
    user_roles_lower = {r.lower() for r in user_roles}
    for r in allowed_roles:
        if r.lower() in user_roles_lower:
            return True
            
    return False

def update_channel_permission(
    channel_id: str,
    channel_name: str,
    purpose: str = "read-only-qa",
    allowed_roles: Optional[List[str]] = None,
    allowed_users: Optional[List[str]] = None,
    on_mention: bool = True,
    prefix: Optional[str] = None
) -> None:
    """Updates or sets channel specific configuration rules."""
    config = load_config()
    if "channels" not in config:
        config["channels"] = {}
        
    config["channels"][str(channel_id)] = {
        "channel_name": channel_name,
        "purpose": purpose,
        "allowed_roles": allowed_roles if allowed_roles is not None else ["@everyone"],
        "allowed_users": allowed_users if allowed_users is not None else [],
        "on_mention": on_mention,
        "prefix": prefix
    }
    save_config(config)

def remove_channel(channel_id: str) -> bool:
    """Removes channel from configured allowed list."""
    config = load_config()
    if "channels" in config and str(channel_id) in config["channels"]:
        del config["channels"][str(channel_id)]
        save_config(config)
        return True
    return False

def get_agent_api_base() -> str:
    import os
    return os.environ.get("AGENT_API_BASE") or load_config().get("agent_api_base", "http://127.0.0.1:8050")

def get_roleplay_guild_ids() -> List[int]:
    return load_config().get("roleplay_guild_ids", [980680159961178123, 1518055111987953814])

def get_boss_user_ids() -> List[int]:
    return load_config().get("boss_user_ids", [405566743415750656, 1418503476857540739])

def get_moderation_channel_id() -> int:
    return load_config().get("moderation_channel_id", 1518103220495581326)

def get_thumbtack_channel_id() -> int:
    return load_config().get("thumbtack_channel_id", 1518534351002927205)

def get_bar_channel_id() -> int:
    return load_config().get("bar_channel_id", 1518087367465111594)

def get_linkshell_channel_id() -> int:
    return load_config().get("linkshell_channel_id", 980931413316628581)

def get_around_house_channel_id() -> int:
    return load_config().get("around_house_channel_id", 1017827413104803931)

def get_auth_headers(method: str, path: str, query: str = "", body: bytes = b"") -> Dict[str, str]:
    """Generates secure HMAC signature headers for API authentication."""
    import hmac
    import hashlib
    import time
    import os

    secret = os.environ.get("INTERNAL_API_SECRET", "").encode()
    if not secret:
        dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "admin")
        secret = hashlib.sha256(dashboard_password.encode()).digest()
        
    timestamp_str = str(int(time.time()))
    body_hash = hashlib.sha256(body).hexdigest()
    
    # Message binds method, path, query, timestamp, and body hash
    message = f"{method.upper()}:{path}:{query}:{timestamp_str}:{body_hash}".encode()
    sig = hmac.new(secret, message, hashlib.sha256).hexdigest()
    
    return {
        "X-Signature": sig,
        "X-Timestamp": timestamp_str
    }

