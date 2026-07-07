#!/bin/bash
# Sync verification script: detects drift between AGent (private) and AGent-Ada (public)
# Run from anywhere. Reports intentional vs unintentional divergences.

PRIVATE=/home/dan/AGent/src/agent
PUBLIC=/home/dan/AGent-Ada/src/agent

# Files that should be identical between repos in their modular locations
SHARED_FILES=(
    "__init__.py"
    "core/__init__.py"
    "core/agent_loop.py"
    "core/agent_types.py"
    "core/landlock.py"
    "core/orchestrator.py"
    "core/routing.py"
    "core/task_manager.py"
    "storage/__init__.py"
    "storage/db.py"
    "storage/persistence.py"
    "storage/conversation.py"
    "observability/__init__.py"
    "observability/telemetry.py"
    "observability/quiet_observer.py"
    "observability/grace_monitor.py"
    "evaluation/__init__.py"
    "evaluation/meta_evaluation.py"
    "interfaces/__init__.py"
    "interfaces/cli.py"
)

echo "=== AGent Repo Sync Verification ==="
echo "Private: $PRIVATE"
echo "Public:  $PUBLIC"
echo ""

drift_count=0
clean_count=0

for f in "${SHARED_FILES[@]}"; do
    if [ ! -f "$PRIVATE/$f" ]; then
        echo "⚠️  MISSING (private): $f"
        continue
    fi
    if [ ! -f "$PUBLIC/$f" ]; then
        echo "⚠️  MISSING (public): $f"
        continue
    fi
    
    diff_output=$(diff "$PRIVATE/$f" "$PUBLIC/$f" 2>&1)
    if [ -z "$diff_output" ]; then
        clean_count=$((clean_count + 1))
    else
        drift_count=$((drift_count + 1))
        echo "🔄 DRIFT: $f"
        echo "$diff_output" | head -10
        echo "---"
    fi
done

# Also check web.py (which has known divergences)
echo ""
echo "=== Files with Known Divergences ==="
diff_output=$(diff "$PRIVATE/interfaces/web.py" "$PUBLIC/interfaces/web.py" 2>&1)
if [ -n "$diff_output" ]; then
    diff_lines=$(echo "$diff_output" | wc -l)
    echo "interfaces/web.py: $diff_lines lines of diff (check for unintentional drift)"
    echo "$diff_output" | head -15
else
    echo "interfaces/web.py: ✅ identical"
fi

echo ""
diff_output=$(diff "$PRIVATE/core/registry.py" "$PUBLIC/core/registry.py" 2>&1)
if [ -n "$diff_output" ]; then
    diff_lines=$(echo "$diff_output" | wc -l)
    echo "core/registry.py: $diff_lines lines of diff (check for unintentional drift)"
    echo "$diff_output" | head -15
else
    echo "core/registry.py: ✅ identical"
fi

echo ""
echo "=== Summary ==="
echo "Shared files checked: ${#SHARED_FILES[@]}"
echo "Clean (identical): $clean_count"
echo "Drifted: $drift_count"

# Check static assets
static_diff=$(diff -rq "$PRIVATE/static/" "$PUBLIC/static/" 2>&1)
if [ -n "$static_diff" ]; then
    echo "Static assets: ⚠️  DRIFT detected"
    echo "$static_diff" | head -5
else
    echo "Static assets: ✅ identical"
fi
