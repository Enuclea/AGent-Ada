"""Centralized configuration loading and validation module."""

import os
import json
from pathlib import Path
from typing import List, Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """System-wide configuration settings loaded from environment and config files."""
    
    agent_db_path: str = "/data/history.db"
    allow_unsandboxed_execution: bool = False
    ada_disable_sandbox: bool = False
    ada_skill_public_key: str = "4f8ea93fc321099ce3d5f57c4ed2588cec782ae28d2e70f81b39e31377a247f8"
    additional_sensitive_keys: Optional[str] = None
    
    # Platform-specific lists (loaded from platform_config.json)
    disabled_plugins: List[str] = []
    disabled_skills: List[str] = []
    lazy_plugins: List[str] = ["playwright"]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def load_platform_config(self) -> None:
        """Loads additional configuration parameters from platform_config.json."""
        # Check AGENT_DB_PATH to find platform_config.json
        db_env = os.environ.get("AGENT_DB_PATH", self.agent_db_path)
        config_path = Path(db_env).parent / "platform_config.json"
        if not config_path.exists():
            config_path = Path(os.getcwd()) / "data" / "platform_config.json"

        if config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                self.disabled_plugins = config_data.get("disabled_plugins", [])
                self.disabled_skills = config_data.get("disabled_skills", [])
                self.lazy_plugins = config_data.get("lazy_plugins", ["playwright"])
            except Exception as e:
                print(f"[CONFIG] Failed to parse platform_config.json: {e}")

# Global settings instance
settings = Settings()
settings.load_platform_config()

# Shared developer public key constant
DEVELOPER_PUBLIC_KEY = "4f8ea93fc321099ce3d5f57c4ed2588cec782ae28d2e70f81b39e31377a247f8"
