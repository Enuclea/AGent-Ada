import os
import json
import pytest
pytestmark = pytest.mark.security
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timezone

try:
    import agent.plugins.multi_tenant_plugin.tenant_db as tenant_db
    import agent.plugins.multi_tenant_plugin.guards as guards
    import agent.plugins.multi_tenant_plugin.reporting as reporting
    HAS_MULTI_TENANT_PLUGIN = True
except ImportError:
    HAS_MULTI_TENANT_PLUGIN = False
import agent.db

@pytest.fixture
def temp_db():
    """Redirects DB_FILE_PATH to a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_db_path = Path(tmpdir) / "test_history.db"
        
        with patch("agent.db.DB_FILE_PATH", tmp_db_path):
            
            # Re-initialize DB
            tenant_db.init_tenant_db()
            
            # Connect directly to create token_telemetry table for stats testing
            conn = sqlite3.connect(tmp_db_path)
            cursor = conn.cursor()
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS token_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                model_name TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost REAL,
                timestamp TEXT
            )
            """)
            conn.commit()
            conn.close()
            
            yield tmp_db_path

@pytest.mark.skipif(not HAS_MULTI_TENANT_PLUGIN, reason="multi-tenant plugin not available")
@pytest.mark.anyio
async def test_tenant_db_crud(temp_db):
    """Verify that tenant, server, rule, role, and license records can be managed."""
    with patch("agent.db.DB_FILE_PATH", temp_db):
        tenant_db.create_tenant("t1", "Tenant One", "owner_123")
        tenant = tenant_db.get_tenant("t1")
        assert tenant is not None
        assert tenant["name"] == "Tenant One"
        assert tenant["owner_id"] == "owner_123"
        
        tenant_db.associate_discord_server("guild_999", "t1", "Phoenix Server")
        server = tenant_db.get_discord_server("guild_999")
        assert server is not None
        assert server["tenant_id"] == "t1"
        
        tenant_db.add_tenant_rule("t1", "max_tokens", "500")
        rules = tenant_db.get_tenant_rules("t1")
        assert len(rules) == 1
        assert rules[0]["rule_name"] == "max_tokens"
        assert rules[0]["rule_value"] == "500"
        
        tenant_db.add_tenant_role("t1", "admin", {"can_exec": True})
        roles = tenant_db.get_tenant_roles("t1")
        assert len(roles) == 1
        assert roles[0]["role_name"] == "admin"
        assert roles[0]["permissions"]["can_exec"] is True
        
        tenant_db.add_tenant_license("lic_abc", "t1", "enterprise", "active", "2030-01-01T00:00:00+00:00")
        license = tenant_db.get_tenant_license("lic_abc")
        assert license is not None
        assert license["license_type"] == "enterprise"

@pytest.mark.skipif(not HAS_MULTI_TENANT_PLUGIN, reason="multi-tenant plugin not available")
@pytest.mark.anyio
async def test_tool_access_guards_isolation(temp_db):
    """Verify that cross-guild tool execution is caught and blocked by the guards."""
    # Setup mock channel to guild mappings
    mock_mappings = {
        "channel_control": "guild_owner",
        "channel_other": "guild_other"
    }
    
    with patch.dict(os.environ, {"TEST_GUILD_MAPPING": json.dumps(mock_mappings)}):
        # Clear local cache first
        guards.CHANNEL_GUILD_CACHE.clear()
        
        # Test case 1: Active session is 'channel_control' (guild_owner).
        # We try to backup the same channel. Should be allowed.
        guards.enforce_server_scoped_isolation(
            session_id="discord-session-channel_control",
            tool_name="backup_discord_channel",
            tool_args={"channel_id": "channel_control"}
        )
        
        # Test case 2: Try to backup channel from another guild. Should raise PermissionError.
        with pytest.raises(PermissionError) as exc_info:
            guards.enforce_server_scoped_isolation(
                session_id="discord-session-channel_control",
                tool_name="backup_discord_channel",
                tool_args={"channel_id": "channel_other"}
            )
        assert "outside the active session guild" in str(exc_info.value)
        
        # Test case 3: Try to pass external guild_id in arguments. Should raise PermissionError.
        with pytest.raises(PermissionError) as exc_info:
            guards.enforce_server_scoped_isolation(
                session_id="discord-session-channel_control",
                tool_name="run_command",
                tool_args={"guild_id": "guild_other"}
            )
        assert "specifies guild guild_other" in str(exc_info.value)

@pytest.mark.skipif(not HAS_MULTI_TENANT_PLUGIN, reason="multi-tenant plugin not available")
@pytest.mark.anyio
async def test_report_compilation_restricted_to_owner(temp_db):
    """Verify that generated reports are strictly restricted to the caller's owned servers."""
    # Create Owner 1 setup: Tenant 1, Guild 1, Channel 1
    with patch("agent.db.DB_FILE_PATH", temp_db):
         
        tenant_db.create_tenant("tenant_1", "Owner One Tenant", "owner_1")
        tenant_db.associate_discord_server("guild_1", "tenant_1", "Guild One")
        tenant_db.add_tenant_license("lic_1", "tenant_1", "pro", "active", "2030-12-31T00:00:00+00:00")
        
        # Create Owner 2 setup: Tenant 2, Guild 2, Channel 2
        tenant_db.create_tenant("tenant_2", "Owner Two Tenant", "owner_2")
        tenant_db.associate_discord_server("guild_2", "tenant_2", "Guild Two")
        tenant_db.add_tenant_license("lic_2", "tenant_2", "free", "active", "2030-12-31T00:00:00+00:00")
        
        # Add token telemetry data
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        # Telemetry for Channel 1 (Guild 1)
        cursor.execute(
            "INSERT INTO token_telemetry (session_id, model_name, input_tokens, output_tokens, cost, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            ("discord-session-channel_1", "gemini-3.5-flash", 100, 200, 0.05, "2026-06-29T12:00:00Z")
        )
        # Telemetry for Channel 2 (Guild 2)
        cursor.execute(
            "INSERT INTO token_telemetry (session_id, model_name, input_tokens, output_tokens, cost, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
            ("discord-session-channel_2", "gemini-3.5-flash", 500, 1000, 0.25, "2026-06-29T12:00:00Z")
        )
        conn.commit()
        conn.close()
        
        # Setup mock channel to guild mappings
        mock_mappings = {
            "channel_1": "guild_1",
            "channel_2": "guild_2"
        }
        
        with patch.dict(os.environ, {"TEST_GUILD_MAPPING": json.dumps(mock_mappings)}):
            guards.CHANNEL_GUILD_CACHE.clear()
            
            # Fetch report for Owner 1
            report_1 = reporting.generate_tenant_report("owner_1")
            
            # Verify Owner 1 report details
            assert report_1["owner_id"] == "owner_1"
            assert len(report_1["licenses"]) == 1
            assert report_1["licenses"][0]["license_key"] == "lic_1"
            assert report_1["licenses"][0]["tenant_name"] == "Owner One Tenant"
            
            usage_1 = report_1["usage"]
            assert usage_1["total_cost"] == 0.05
            assert usage_1["total_input_tokens"] == 100
            assert "guild_1" in usage_1["guild_breakdown"]
            assert "guild_2" not in usage_1["guild_breakdown"]  # Restricted/Blocked!
            
            # Fetch report for Owner 2
            report_2 = reporting.generate_tenant_report("owner_2")
            
            # Verify Owner 2 report details
            assert report_2["owner_id"] == "owner_2"
            assert len(report_2["licenses"]) == 1
            assert report_2["licenses"][0]["license_key"] == "lic_2"
            
            usage_2 = report_2["usage"]
            assert usage_2["total_cost"] == 0.25
            assert usage_2["total_input_tokens"] == 500
            assert "guild_2" in usage_2["guild_breakdown"]
            assert "guild_1" not in usage_2["guild_breakdown"]  # Restricted/Blocked!
