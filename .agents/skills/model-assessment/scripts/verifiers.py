import os
import sys
import json
import subprocess
import tempfile
import re
from pathlib import Path
from typing import Tuple

def clean_code(code_str: str, lang: str = "") -> str:
    """Removes markdown code blocks if the model accidentally outputted them."""
    cleaned = code_str.strip()
    if cleaned.startswith("```"):
        # Remove starting ```lang or ```
        lines = cleaned.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned

def verify_perl_coding(code: str) -> Tuple[bool, str]:
    code = clean_code(code, "perl")
    dummy_log = (
        "192.168.1.1 - - [20/Jul/2026:00:00:00 +0000] \"GET / HTTP/1.1\" 200 123\n"
        "10.0.0.5 - - [20/Jul/2026:00:00:01 +0000] \"GET /admin HTTP/1.1\" 403 456\n"
        "192.168.1.2 - - [20/Jul/2026:00:00:02 +0000] \"POST /login HTTP/1.1\" 500 789\n"
        "10.0.0.5 - - [20/Jul/2026:00:00:03 +0000] \"GET /index.php HTTP/1.1\" 404 123\n"
    )
    expected_output = ["10.0.0.5", "192.168.1.2"]
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dir_path = Path(tmpdir)
        script_file = tmp_dir_path / "test.pl"
        log_file = tmp_dir_path / "access.log"
        
        script_file.write_text(code, encoding="utf-8")
        log_file.write_text(dummy_log, encoding="utf-8")
        
        try:
            res = subprocess.run(
                ["perl", str(script_file)],
                input=dummy_log,
                text=True,
                capture_output=True,
                timeout=5.0
            )
            if res.returncode != 0:
                return False, f"Execution failed (Exit Code {res.returncode}): {res.stderr}"
            
            output_lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
            if output_lines == expected_output:
                return True, "Success! Outputs matched expected sorted unique IPs."
            else:
                return False, f"Incorrect Output.\nExpected: {expected_output}\nGot: {output_lines}\nStderr: {res.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Execution timed out."
        except Exception as e:
            return False, f"Verifier error: {e}"

def verify_php_coding(code: str) -> Tuple[bool, str]:
    code = clean_code(code, "php")
    # Ensure PHP opening tag exists
    if not code.strip().startswith("<?php"):
        code = "<?php\n" + code
        
    dummy_json = [
        {"port": 80, "bytes": 100},
        {"port": 443, "bytes": 200},
        {"port": 80, "bytes": 50}
    ]
    expected_data = {"80": 150, "443": 200}
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dir_path = Path(tmpdir)
        script_file = tmp_dir_path / "test.php"
        script_file.write_text(code, encoding="utf-8")
        
        try:
            res = subprocess.run(
                ["php", str(script_file)],
                input=json.dumps(dummy_json),
                text=True,
                capture_output=True,
                timeout=5.0
            )
            if res.returncode != 0:
                return False, f"Execution failed (Exit Code {res.returncode}): {res.stderr}"
            
            try:
                out_data = json.loads(res.stdout.strip())
                # Normalize keys to strings for comparison
                normalized_out = {str(k): int(v) for k, v in out_data.items()}
                if normalized_out == expected_data:
                    return True, "Success! Outputs matched expected aggregated port bytes."
                else:
                    return False, f"Incorrect Output.\nExpected: {expected_data}\nGot: {normalized_out}"
            except json.JSONDecodeError:
                return False, f"Invalid JSON output returned: {res.stdout.strip()}\nStderr: {res.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Execution timed out."
        except Exception as e:
            return False, f"Verifier error: {e}"

def verify_terminal_mastery(command: str) -> Tuple[bool, str]:
    command = clean_code(command)
    expected_output = [
        "2 [ERROR] failed to connect",
        "1 [ERROR] database error"
    ]
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_dir_path = Path(tmpdir)
        logs_dir = tmp_dir_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        
        log_file = logs_dir / "app.log"
        log_content = (
            "[INFO] start\n"
            "[ERROR] failed to connect\n"
            "[ERROR] failed to connect\n"
            "[ERROR] debug statement\n"
            "[ERROR] database error\n"
        )
        log_file.write_text(log_content, encoding="utf-8")
        
        try:
            # Run command inside tmpdir
            res = subprocess.run(
                command,
                shell=True,
                text=True,
                capture_output=True,
                cwd=tmpdir,
                timeout=5.0
            )
            if res.returncode != 0:
                return False, f"Command execution failed (Exit Code {res.returncode}): {res.stderr}"
            
            output_lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
            if output_lines == expected_output:
                return True, "Success! Output matched expected sorted counts."
            else:
                return False, f"Incorrect Output.\nExpected: {expected_output}\nGot: {output_lines}\nStderr: {res.stderr}"
        except subprocess.TimeoutExpired:
            return False, "Execution timed out."
        except Exception as e:
            return False, f"Verifier error: {e}"

def verify_mikrotik_config(commands_str: str) -> Tuple[bool, str]:
    commands_str = clean_code(commands_str)
    lines = [line.strip() for line in commands_str.splitlines() if line.strip()]
    
    issues = []
    # Check if commands contain required keywords
    has_vlan = False
    has_ip = False
    has_firewall = False
    
    for line in lines:
        if "/interface vlan" in line or "interface=ether1" in line:
            if "vlan-id=10" in line or "vlan-id=\"10\"" in line:
                has_vlan = True
        if "/ip address" in line:
            if "10.10.10.1/24" in line and ("interface=VLAN-10" in line or "interface=\"VLAN-10\"" in line):
                has_ip = True
        if "/ip firewall filter" in line:
            if "chain=forward" in line and "action=drop" in line:
                if "in-interface=VLAN-10" in line and "out-interface=ether1" in line:
                    has_firewall = True
                    
    if not has_vlan:
        issues.append("Missing VLAN-10 configuration with VLAN ID 10 on ether1.")
    if not has_ip:
        issues.append("Missing IP address 10.10.10.1/24 assignment to VLAN-10.")
    if not has_firewall:
        issues.append("Missing firewall filter rule to drop forward chain traffic from VLAN-10 to ether1.")
        
    if not issues:
        return True, "Success! RouterOS commands validated successfully against test criteria."
    else:
        return False, f"Validation failed:\n" + "\n".join([f"- {iss}" for iss in issues])
