#!/usr/bin/env python3
import os
import sys
import json
import requests
from pathlib import Path

def resolve_gemma_worker_urls():
    """Checks the remote worker list and returns direct URLs for gemma4:e4b."""
    username = os.environ.get("DASHBOARD_USERNAME", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD", "admin")
    
    workers_url = "http://localhost:8050/api/workers"
    
    # Default fallbacks
    fallback_urls = [
        "http://10.200.0.4:11434/api/generate",
        "http://10.200.0.3:11434/api/generate",
        "http://localhost:11434/api/generate"
    ]
    
    print("[Lacie] Checking remote worker list at http://localhost:8050/api/workers...")
    try:
        # Try Bearer token first
        bearer_headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {password}"
        }
        resp = requests.get(workers_url, headers=bearer_headers, timeout=5.0)
        if resp.status_code == 401:
            # Try Basic Auth
            resp = requests.get(workers_url, auth=(username, password), timeout=5.0)
            
        if resp.status_code == 200:
            data = resp.json()
            workers = data.get("workers", [])
            target_urls = []
            for w in workers:
                if w.get("status") != "online":
                    continue
                # Retrieve ollama models
                ollama_models = w.get("metadata", {}).get("ollama_models", [])
                if "gemma4:e4b" in ollama_models:
                    host = w.get("host")
                    if host:
                        ip = host.split(":")[0]
                        target_urls.append(f"http://{ip}:11434/api/generate")
            
            if target_urls:
                print(f"[Lacie] Found active workers for gemma4:e4b: {target_urls}")
                return target_urls
            else:
                print("[Lacie] No online workers found with gemma4:e4b model registered.")
        else:
            print(f"[Lacie] Failed to fetch worker list: HTTP {resp.status_code}")
    except Exception as e:
        print(f"[Lacie] Error querying worker list endpoint: {e}")
        
    print(f"[Lacie] Falling back to default Ollama URLs: {fallback_urls}")
    return fallback_urls

def query_gemini_flash(prompt: str) -> str:
    url = "http://localhost:8050/api/chat"
    username = os.environ.get("DASHBOARD_USERNAME", "admin")
    password = os.environ.get("DASHBOARD_PASSWORD", "admin")
    
    import uuid
    payload = {
        "prompt": prompt,
        "model": "gemini-3.5-flash",
        "disable_tools": True,
        "session_id": f"query-session-{uuid.uuid4()}"
    }
    
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {password}"
    }
    
    print(f"\n[Lacie] Streaming gemini-3.5-flash response...")
    accumulated_text = []
    try:
        # Try Bearer
        resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=30.0)
        if resp.status_code == 401:
            # Try Basic
            resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, auth=(username, password), stream=True, timeout=30.0)
            
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text}")
            
        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode('utf-8').strip()
            if line_str.startswith("data: "):
                data_payload = line_str[6:].strip()
                if data_payload == "[DONE]":
                    break
                try:
                    data_json = json.loads(data_payload)
                    if data_json.get("type") == "chunk":
                        content = data_json.get("content", "")
                        accumulated_text.append(content)
                        sys.stdout.write(content)
                        sys.stdout.flush()
                except Exception:
                    pass
        print() # Add trailing newline
        return "".join(accumulated_text)
    except Exception as e:
        print(f"\n[Lacie] Error querying gemini-3.5-flash: {e}")
        return f"Error: {e}"

def query_gemma_e4b(prompt: str, target_urls: list) -> str:
    payload = {
        "model": "gemma4:e4b",
        "prompt": prompt,
        "stream": True
    }
    
    print(f"\n[Lacie] Streaming gemma4:e4b response...")
    for url in target_urls:
        accumulated_text = []
        try:
            print(f"[Lacie] Trying Ollama endpoint: {url}")
            resp = requests.post(url, json=payload, stream=True, timeout=15.0)
            if resp.status_code != 200:
                print(f"[Lacie] Failed endpoint {url}: HTTP {resp.status_code}")
                continue
                
            for line in resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode('utf-8').strip()
                try:
                    data_json = json.loads(line_str)
                    content = data_json.get("response", "")
                    accumulated_text.append(content)
                    sys.stdout.write(content)
                    sys.stdout.flush()
                    if data_json.get("done"):
                        break
                except Exception:
                    pass
            print() # Add trailing newline
            return "".join(accumulated_text)
        except Exception as e:
            print(f"\n[Lacie] Error with endpoint {url}: {e}")
            continue
            
    return "Error: All endpoints failed."

def main():
    script_dir = Path(__file__).resolve().parent
    dataset_path = script_dir / "test_dataset.json"
    if not dataset_path.exists():
        dataset_path = Path("/app/scratch/test_dataset.json")
        
    if not dataset_path.exists():
        print(f"[Lacie] Error: Dataset file not found at {dataset_path}", file=sys.stderr)
        sys.exit(1)
        
    print(f"[Lacie] Loading test cases from {dataset_path}...")
    try:
        with open(dataset_path, "r") as f:
            test_cases = json.load(f)
    except Exception as e:
        print(f"[Lacie] Error parsing dataset JSON: {e}", file=sys.stderr)
        sys.exit(1)
        
    # Resolve target endpoints for gemma4:e4b
    gemma_urls = resolve_gemma_worker_urls()
    
    results = []
    
    for i, tc in enumerate(test_cases, start=1):
        tc_id = tc.get("id", f"case_{i}")
        prompt = tc.get("prompt", "")
        print(f"\n[Lacie] === Processing case {i}/{len(test_cases)}: {tc_id} ===")
        
        # Query gemini-3.5-flash
        gemini_response = query_gemini_flash(prompt)
        
        # Query gemma4:e4b
        gemma_response = query_gemma_e4b(prompt, gemma_urls)
        
        results.append({
            "id": tc_id,
            "category": tc.get("category", ""),
            "prompt": prompt,
            "expected_outcome": tc.get("expected_outcome", ""),
            "responses": {
                "gemini-3.5-flash": gemini_response,
                "gemma4:e4b": gemma_response
            }
        })
        
    # Write to target files
    output_filename = "i+j!/!_outputs.json"
    output_paths = [script_dir / output_filename]
    if script_dir != Path("/app/scratch"):
        output_paths.append(Path("/app/scratch") / output_filename)
        
    for out_path in output_paths:
        try:
            print(f"[Lacie] Saving output to {out_path}...")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w") as f:
                json.dump(results, f, indent=2)
        except Exception as e:
            print(f"[Lacie] Error writing output to {out_path}: {e}", file=sys.stderr)

    print("\n[Lacie] Processing completed successfully!")

if __name__ == "__main__":
    main()
