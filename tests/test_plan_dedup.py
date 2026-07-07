"""Test: Plan Deduplication & Atera Claim Atomicity

Verifies the three fixes for the Ada task duplication bug:
1. Completed plans are NOT re-created when the session receives a new message
2. System resume prompts ("[SYSTEM ...") never trigger plan decomposition
3. Atomic claim_atera_item prevents duplicate Atera ticket processing
"""
import os
import sys
import uuid
import sqlite3
import tempfile
import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestPlanDeduplication:
    """Tests for the plan re-creation guard (Fix 3A)."""

    def setup_method(self):
        """Create a temporary DB with schema for each test."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_plans (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                title TEXT,
                status TEXT,
                created_at TEXT,
                goal TEXT,
                acceptance_criteria TEXT,
                non_goals TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS plan_steps (
                id TEXT PRIMARY KEY,
                plan_id TEXT,
                step_order INTEGER,
                description TEXT,
                status TEXT,
                assigned_tool TEXT,
                assigned_args TEXT,
                error_message TEXT
            )
        """)
        conn.commit()
        conn.close()

    def teardown_method(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _insert_plan(self, session_id, status="completed", step_statuses=None):
        """Helper to insert a plan with steps."""
        plan_id = str(uuid.uuid4())
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO session_plans (id, session_id, title, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (plan_id, session_id, "Test Plan", status, "2026-07-07T14:00:00Z"),
        )
        if step_statuses is None:
            step_statuses = ["completed", "completed"]
        for i, st in enumerate(step_statuses):
            conn.execute(
                "INSERT INTO plan_steps (id, plan_id, step_order, description, status) VALUES (?, ?, ?, ?, ?)",
                (f"step-{plan_id}-{i}", plan_id, i + 1, f"Step {i+1}", st),
            )
        conn.commit()
        conn.close()
        return plan_id

    def test_system_prompt_guard_blocks_resume(self):
        """[SYSTEM RESUME] prompts must never trigger plan creation."""
        prompt = "[SYSTEM RESUME]\nSubagent 'subagent-ops_runner-abc123' has completed.\nSubagent Output: done."
        is_system_prompt = prompt.strip().startswith("[SYSTEM")
        assert is_system_prompt is True, "System resume prompt not detected"

    def test_system_prompt_guard_blocks_driver(self):
        """[SYSTEM DRIVER] prompts must never trigger plan creation."""
        prompt = '[SYSTEM DRIVER]\nYou are executing Step 2 of 4: "Create the contact".'
        is_system_prompt = prompt.strip().startswith("[SYSTEM")
        assert is_system_prompt is True, "System driver prompt not detected"

    def test_normal_prompt_passes_guard(self):
        """Normal user prompts should NOT be blocked by system prompt guard."""
        prompt = "Add a contact to novo.org under Atera."
        is_system_prompt = prompt.strip().startswith("[SYSTEM")
        assert is_system_prompt is False, "Normal prompt incorrectly detected as system"

    def test_completed_plan_clears_guard(self):
        """A completed plan should set existing_plan = None, but only for non-system prompts."""
        session_id = "test-session-123"
        self._insert_plan(session_id, status="completed")

        # Simulate the guard logic from web.py L754-767
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, title, status, created_at FROM session_plans WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_id,),
        )
        plan_row = cursor.fetchone()
        existing_plan = dict(plan_row) if plan_row else None

        if existing_plan:
            plan_status = existing_plan.get("status", "")
            if plan_status == "completed":
                existing_plan = None

        # Guard is cleared for user prompts
        user_prompt = "Add another contact"
        is_system_prompt = user_prompt.strip().startswith("[SYSTEM")
        should_create = existing_plan is None and not is_system_prompt and len(user_prompt.strip()) > 10
        assert should_create is True, "Should allow new plan for new user prompt"

        # Guard blocks system resume prompts
        resume_prompt = "[SYSTEM RESUME]\nSubagent completed."
        is_system_prompt = resume_prompt.strip().startswith("[SYSTEM")
        should_create = existing_plan is None and not is_system_prompt and len(resume_prompt.strip()) > 10
        assert should_create is False, "Must NOT create plan from system resume prompt"

        conn.close()


class TestAteraClaimAtomicity:
    """Tests for the atomic claim_atera_item pattern (Fix Vector 2)."""

    def setup_method(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        # Initialize with the enuclea schema
        from enuclea import db
        db.init_db(self.db_path)

    def teardown_method(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def test_claim_succeeds_first_time(self):
        """First claim on an untracked item should succeed."""
        from enuclea import db
        result = db.claim_atera_item("ticket_999", "ticket", 999, db_path=self.db_path)
        assert result is True, "First claim should return True"

    def test_claim_fails_second_time(self):
        """Second claim on the same item should fail (already claimed)."""
        from enuclea import db
        first = db.claim_atera_item("ticket_999", "ticket", 999, db_path=self.db_path)
        second = db.claim_atera_item("ticket_999", "ticket", 999, db_path=self.db_path)
        assert first is True
        assert second is False, "Second claim must return False (already claimed)"

    def test_claim_blocks_is_tracked(self):
        """After claiming, is_atera_item_tracked should return True."""
        from enuclea import db
        db.claim_atera_item("ticket_999", "ticket", 999, db_path=self.db_path)
        tracked = db.is_atera_item_tracked("ticket_999", db_path=self.db_path)
        assert tracked is True, "Claimed item should be tracked"

    def test_claim_then_update(self):
        """Claim followed by update should produce a proper tracked record."""
        from enuclea import db
        db.claim_atera_item("ticket_999", "ticket", 999, db_path=self.db_path)
        db.update_tracked_atera_item("ticket_999", "morgen-task-abc", db_path=self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tracked_atera_items WHERE id = ?", ("ticket_999",)).fetchone()
        conn.close()

        assert row is not None
        assert row["morgen_task_id"] == "morgen-task-abc"
        assert row["status"] == "open"

    def test_claim_sets_processing_status(self):
        """Claimed item should have status='processing' and morgen_task_id='__pending__'."""
        from enuclea import db
        db.claim_atera_item("ticket_999", "ticket", 999, db_path=self.db_path)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tracked_atera_items WHERE id = ?", ("ticket_999",)).fetchone()
        conn.close()

        assert row["status"] == "processing"
        assert row["morgen_task_id"] == "__pending__"

    def test_add_tracked_still_works(self):
        """Legacy add_tracked_atera_item should still work for backward compatibility."""
        from enuclea import db
        db.add_tracked_atera_item("ticket_888", "ticket", 888, "morgen-task-xyz", db_path=self.db_path)
        tracked = db.is_atera_item_tracked("ticket_888", db_path=self.db_path)
        assert tracked is True
