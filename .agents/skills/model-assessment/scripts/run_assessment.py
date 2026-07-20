#!/usr/bin/env python3
import sys
import os
import json
import time
import argparse
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

# Add parent directory to sys.path so we can import verifiers
sys.path.insert(0, str(Path(__file__).resolve().parent))
import verifiers

def run_ollama_query(host: str, model: str, prompt: str) -> dict:
    url = f"http://{host}:11434/api/generate"
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False
    }
    
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    start_time = time.time()
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latency = time.time() - start_time
            return {
                "success": True,
                "response": data.get("response", ""),
                "latency": latency,
                "prompt_eval_count": data.get("prompt_eval_count", 0),
                "eval_count": data.get("eval_count", 0)
            }
    except Exception as e:
        latency = time.time() - start_time
        return {
            "success": False,
            "error": str(e),
            "latency": latency
        }

def wake_ada(api_base: str, session_id: str, report_data: dict) -> None:
    url = f"{api_base}/api/chat"
    
    prompt = (
        "🤖 **Model Assessment Benchmark Completed** 📊\n\n"
        "Here is the JSON telemetry report:\n"
        f"```json\n{json.dumps(report_data, indent=2)}\n```\n\n"
        "Please review the code implementations and RouterOS commands, grade each task category (A-F), "
        "evaluate overall performance and speed, and post a structured grading scorecard."
    )
    
    payload = {
        "prompt": prompt,
        "session_id": session_id,
        "disable_tools": True
    }
    
    headers = {"Content-Type": "application/json"}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST"
    )
    
    print(f"\nWaking Ada on session {session_id} to perform the final assessment...")
    try:
        with urllib.request.urlopen(req) as resp:
            buffer = ""
            while True:
                chunk = resp.read(1024)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            event = json.loads(data_str)
                            if event.get("type") == "chunk":
                                sys.stdout.write(event.get("content", ""))
                                sys.stdout.flush()
                        except Exception:
                            pass
            print("\nAda assessment completed successfully.")
    except Exception as e:
        print(f"Error waking Ada: {e}")

def main():
    parser = argparse.ArgumentParser(description="Model Assessment Benchmark Runner")
    parser.add_argument("--model", type=str, required=True, help="Ollama model to assess (e.g. gemma4:12b)")
    parser.add_argument("--host", type=str, default="localhost", help="Ollama host address (default: localhost)")
    parser.add_argument("--session", type=str, help="Optional Discord session ID to wake Ada for assessment")
    parser.add_argument("--api-port", type=int, default=8050, help="AGent API port (default: 8050)")
    args = parser.parse_args()
    
    # 1. Load Workbook
    project_root = Path(__file__).resolve().parent.parent
    workbook_path = project_root / "resources" / "workbook.json"
    
    if not workbook_path.exists():
        print(f"Error: Workbook file not found at {workbook_path}")
        sys.exit(1)
        
    with open(workbook_path, "r") as f:
        tasks = json.load(f)
        
    print(f"Starting Model Assessment Benchmark against model '{args.model}' on host '{args.host}'...")
    print(f"Total Tasks: {len(tasks)}")
    
    results = []
    
    # 2. Run Task Loop
    for idx, task in enumerate(tasks, 1):
        print(f"\n[{idx}/{len(tasks)}] Running Category: {task['category']}...")
        print(f"Prompt: {task['description']}")
        
        q_res = run_ollama_query(args.host, args.model, task["prompt"])
        
        if not q_res["success"]:
            print(f"❌ Ollama query failed: {q_res.get('error')}")
            results.append({
                "task_id": task["id"],
                "category": task["category"],
                "latency_seconds": q_res["latency"],
                "verified": False,
                "message": f"Ollama generation failed: {q_res.get('error')}"
            })
            continue
            
        generated_code = q_res["response"]
        print(f"Generated output ({len(generated_code)} chars). Running verifier...")
        
        # Dispatch to corresponding verifier
        verified = False
        message = ""
        
        if task["id"] == "perl_coding":
            verified, message = verifiers.verify_perl_coding(generated_code)
        elif task["id"] == "php_coding":
            verified, message = verifiers.verify_php_coding(generated_code)
        elif task["id"] == "terminal_mastery":
            verified, message = verifiers.verify_terminal_mastery(generated_code)
        elif task["id"] == "mikrotik_config":
            verified, message = verifiers.verify_mikrotik_config(generated_code)
            
        status_str = "✅ PASSED" if verified else "❌ FAILED"
        print(f"Verification Status: {status_str}")
        print(f"Verifier Message: {message}")
        
        results.append({
            "task_id": task["id"],
            "category": task["category"],
            "latency_seconds": q_res["latency"],
            "prompt_eval_count": q_res.get("prompt_eval_count", 0),
            "eval_count": q_res.get("eval_count", 0),
            "tokens_per_second": q_res.get("eval_count", 0) / q_res["latency"] if q_res["latency"] > 0 else 0,
            "generated_output": generated_code,
            "verified": verified,
            "message": message
        })
        
    # 3. Compile Results
    report = {
        "benchmark_timestamp": datetime.now(timezone.utc).isoformat(),
        "target_model": args.model,
        "target_host": args.host,
        "total_latency_seconds": sum(r["latency_seconds"] for r in results),
        "tasks_completed": len(results),
        "tasks_passed": sum(1 for r in results if r["verified"]),
        "results": results
    }
    
    # Save Report
    results_dir = project_root / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    report_file = results_dir / f"assessment_report_{int(time.time())}.json"
    
    with open(report_file, "w") as f_out:
        json.dump(report, f_out, indent=2)
        
    print(f"\n==================================================")
    print(f"📊 Assessment Completed! Report saved to: {report_file}")
    print(f"Passed: {report['tasks_passed']}/{report['tasks_completed']}")
    print(f"Total Generation Time: {report['total_latency_seconds']:.2f}s")
    print(f"==================================================")
    
    # 4. Wake Ada if requested
    if args.session:
        api_base = f"http://localhost:{args.api_port}"
        wake_ada(api_base, args.session, report)

if __name__ == "__main__":
    main()
