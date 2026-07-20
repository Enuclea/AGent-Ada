---
name: model-assessment
description: Standardized agentic benchmark flow that runs a workbook of tasks (Perl, PHP, Terminal, MikroTik RouterOS) against a local Ollama model and wakes Ada for grading.
---

# Model Assessment Benchmark Skill

This skill runs a standardized workbook of coding, terminal, and networking configuration tasks against a target local LLM (running on Ollama), records performance telemetry, executes the output locally to verify correctness, and posts the results to Ada for final assessment.

## Usage

Run the assessment from the command line:

```bash
python3 .agents/skills/model-assessment/scripts/run_assessment.py --model <ollama_model> --host <ollama_host_ip_or_localhost> --session <discord_session_id_or_none>
```

## How It Works

1. **Workbook Loading**: Loads standardized tasks from `resources/workbook.json`.
2. **Local Inference**: Queries the target model via Ollama's HTTP API (`/api/generate`) and captures generated code and commands, logging latency and token statistics.
3. **Execution Verification**: Runs verifiers (`scripts/verifiers.py`) to execute Perl, PHP, and Bash scripts locally inside a temporary test space and validate the output. MikroTik configurations are parsed and validated for syntax.
4. **Grading & Wakeup**: Compiles a JSON report and sends it to the API `/api/chat` endpoint to wake Ada. Ada uses a frontier model (Gemini/Claude) to review the code, grade the results (A-F), and post the report to Discord.
