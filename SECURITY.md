# Security Policy and Threat Model

**AGent-Ada** is designed as a secure, developer-focused AI agent orchestration harness. Security is a core priority, with multiple layers of protection including sandboxing, cryptographic verification, input/output sanitization, and rate limiting.

For product posture, non-goals, trust rungs, and the opt-in philosophy (including “we do not protect admins from themselves”), see **[SECURITY-INTENT.md](SECURITY-INTENT.md)**. This file is the mechanisms and operational policy; that file is the lens.

## Our Philosophy

- **Self-authored plugins are strongly preferred.**  
  The sincerest recommendation from the maintainer (who runs Ada on closed networks with no third-party plugins) is: **Ask the AI to develop your own plugins and custom routes.** This eliminates the vast majority of supply-chain and injection risks.

- **Third-party content disclaimer**  
  We provide existing plugin examples and branches for inspiration and convenience, **but we do not recommend running unvetted third-party plugins or skills in production environments.** Any third-party code carries inherent risks that no framework can fully eliminate.

## Security Features

AGent-Ada implements defense-in-depth protections:

- **Sandboxing**: Fail-closed enforcement with strict path sanitization.
- **Cryptographic Verification**: Mandatory signature checking for remote skill/plugin installation (when enabled).
- **Security Pipeline**: Comprehensive input sanitization (prompt injection blocking), output redaction (secrets scrubbing), and static scanning of custom routes.
- **Execution Controls**: Rate limiting, permission scoping, and centralized API broker with auditing.
- **Isolation Options**: Docker-first deployment with minimal privileges recommended.
- **Observability**: Full telemetry and audit logging via SQLite for monitoring behavior.

## Threat Model & Known Limitations

Even with strong protections, certain risks remain — especially with third-party content:

- **Plugins vs. Skills Architecture**:
  - **Plugins** (in `/plugins/`) are administrative, host-loaded extensions that run in-process. They must *only* be installed by system administrators. AST scanning is run as a static speed-bump/audit check, but the security of the host depends on administrators only loading trusted plugins.
  - **Skills** are dynamic user-level packages containing instructions and scripts. All user-supplied/remote skills are strictly executed *inside* the Bubblewrap/Landlock sandbox (`bwrap --unshare-all`), keeping untrusted code execution completely isolated from the host process.
- **Prompt Injection** (direct or indirect via files/memory/context)
- **Implementation edge cases** (sandbox escapes, misconfigurations)
- **Operator error** (running untrusted code)

**Third-party plugins and skills are the highest risk surface.** While AGent-Ada provides tools to mitigate these, safety ultimately depends on the content itself.

## Recommendations for Safe Usage

1. **Develop your own** — Use Ada itself to generate and iterate on custom plugins/routes.
2. **Run isolated** — Always use Docker with least-privilege settings (non-root, dropped capabilities, limited volume mounts).
3. **Review & Verify** — Inspect, test in dry-run/sandbox, and enable paranoid mode for any external code.
4. **Closed networks** — Minimize exposure to untrusted data sources (web, email, documents).
5. **Monitor** — Regularly review telemetry logs and set up alerts for anomalous behavior.
6. **Least privilege** — Grant only the permissions needed for specific tasks.

## Ollama-Compatible Endpoint (Honeypot / Keyless Inference)

The `/api/chat` and `/api/generate` endpoints provide Ollama-compatible LLM inference by wrapping the `agy` CLI inside a Bubblewrap sandbox. This enables **zero-cost inference** via Google's OAuth flow without API key billing.

### Security Model

The sandboxed `agy` process is constrained by seven layers:

1. **Bubblewrap** with namespace isolation (`--unshare-ipc/pid/uts/cgroup`), required — no Landlock fallback
2. **`--sandbox`** flag on `agy` (terminal restrictions, no shell escapes)
3. **`stdin=DEVNULL`** (tool permission prompts can never be approved)
4. **`--dangerously-skip-permissions` intentionally absent**
5. **`-p` print mode** (single prompt, non-interactive, exit after response)
6. **`read_only_workspace=True`** (no writes to host filesystem)
7. **OAuth token bound read-only** (cannot be modified by the sandboxed process)

### Accepted Risk: OAuth Token in Sandbox

The `agy` process requires the OAuth token to authenticate with the Gemini API. Without it, the endpoint is non-functional. The token is bound read-only into the Bubblewrap namespace. While `agy` has network access (required to reach Gemini), the constraints above limit its behavior to: **send prompt → receive text → exit**.

### Recommendation for Unmonitored Environments

> **If your deployment does not include host-level network monitoring or you cannot guarantee Bubblewrap availability**, we recommend leaving the Ollama-compatible endpoint disabled (the default). LLM requests can be routed through the API-backed routing engine instead, which uses paid API credits but ensures the OAuth token never enters a sandboxed process. The endpoint is controlled by `ADA_ENABLE_OLLAMA_ENDPOINT` in your `.env` (default: `0`).

The tradeoff is explicit:
- **Monitored host + bwrap enforced**: OAuth token in sandbox is an accepted risk with zero API cost.
- **Unmonitored host**: Use routing engine with API credits; OAuth token stays in the trusted parent process.

## Reporting Vulnerabilities

We take security seriously. Please report potential issues privately to Enuclea Support [support@enuclea.com] or GitHub Security Advisories].

- Include reproduction steps, affected versions, and impact.
- We aim to acknowledge reports within 48 hours and address critical issues promptly.

## Scope

This policy covers the core AGent-Ada harness. Third-party plugins, external tools, and underlying LLMs (AntiGravity, Grok, Ollama, etc.) are outside the security boundary.

The **Ollama-Compatible API** (`src/agent/api/ollama_clone.py`) is a deliberately controlled honeypot within the security boundary.  It provides real LLM responses to sandbox code under evaluation while enforcing zero tool access through harness-level sandboxing, process isolation (`stdin=DEVNULL`), and silent payload analysis.  Any sandbox escape via this interface should be reported as a critical vulnerability.

---

**In short**: AGent-Ada gives you powerful tools and strong defaults, but **the safest path is self-authored code on isolated infrastructure**. Use third-party content at your own risk, with best-effort protections applied.

Thank you for helping keep the ecosystem safer.