# Security Policy and Threat Model

**AGent-Ada** is designed as a secure, developer-focused AI agent orchestration harness. Security is a core priority, with multiple layers of protection including sandboxing, cryptographic verification, input/output sanitization, and rate limiting.

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

- **Prompt Injection** (direct or indirect via files/memory/context)
- **Malicious Plugins/Skills** (supply-chain attacks, persistence)
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

## Reporting Vulnerabilities

We take security seriously. Please report potential issues privately to Enuclea Support [support@enuclea.com] or GitHub Security Advisories].

- Include reproduction steps, affected versions, and impact.
- We aim to acknowledge reports within 48 hours and address critical issues promptly.

## Scope

This policy covers the core AGent-Ada harness. Third-party plugins, external tools, and underlying LLMs (AntiGravity, Grok, Ollama, etc.) are outside the security boundary.

---

**In short**: AGent-Ada gives you powerful tools and strong defaults, but **the safest path is self-authored code on isolated infrastructure**. Use third-party content at your own risk, with best-effort protections applied.

Thank you for helping keep the ecosystem safer.