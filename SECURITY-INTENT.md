# Security Intent

This document states **why** AGent/AGent-Ada is secured the way it is, and what we deliberately do **not** try to do. For concrete controls, threat details, and operational recommendations, see [SECURITY.md](SECURITY.md).

If the two conflict on tone, prefer this file for *goals and non-goals*, and SECURITY.md for *mechanisms and current policy*.

---

## What this product is

AGent is a **single-entity agent harness**: it wraps common operational tasks into agentic workflows for one operator or one small trusted team. It is in the same product family as tools like Hermes or OpenClaw—not a multi-tenant SaaS agent platform, not an internet-facing application server, and not primarily a heavy software-engineering IDE agent.

The intended loop is:

1. An operator (or small team) runs the harness on infrastructure they control.
2. The agent performs recurring work: integrations, routines, automation, light scripting.
3. The agent is encouraged to **author its own tools, scripts, skills, and processes** under that operator’s authority.
4. Trust expands only when the operator **explicitly promotes** new capability (plugins, routes, endpoints, unsandboxed execution).

The public build’s job is to ship **sane defaults** for that model—not to invent multi-tenant isolation theater.

---

## What this product is not

| Non-goal | Why it is out of scope |
|----------|------------------------|
| Multi-tenant isolation | One entity owns the host, the data, and the blast radius. |
| Internet-facing public agent | Exposure is an operator deployment choice, not a product promise. |
| Protecting admins from themselves | An operator with root/host access can always defeat controls; pretending otherwise is false security. |
| Guaranteeing safety of arbitrary third-party code | No framework can fully sanitize unvetted plugins or skills. |
| Replacing host/network security | Sandbox and pipeline are depth-in-depth, not a substitute for private networks, least privilege, and monitoring. |

If you need hard tenancy boundaries, public multi-user auth, or “safe on the open internet by default,” this is the wrong product shape.

---

## Threat model in one sentence

**The true attack vector is injection of unknown code into a trust rung that can act with host or in-process privilege.**

Prompt injection, malicious documents, poisoned memory, remote skills, unsigned plugins, and model-emitted scripts are all instances of that same problem: **untrusted content becoming executable authority**.

Secondary concerns (rate abuse, secret leakage in logs, misconfiguration) matter and are mitigated where cheap, but they do not redefine the product.

---

## Trust rungs

Capability climbs a ladder. Crossing a rung must be **deliberate**.

```text
  [host / in-process plugins]     ← admin-only; full process trust
           ↑ promote only
  [sandboxed skills / tools]      ← untrusted code runs isolated
           ↑ install / enable
  [model output & instructions]   ← text; not executable by default
           ↑ user / channel input
  [external data]                 ← web, mail, tickets, files
```

**Public defaults** keep new capability on the lower rungs until an operator turns something on or promotes it.  
**Personal deployments** may collapse steps for speed; that is an accepted operator choice, not a public default.

---

## Operator sovereignty

We **cannot and should not** stop an administrator who:

- installs an in-process plugin they have not reviewed,
- sets `ALLOW_UNSANDBOXED_EXECUTION`,
- enables the Ollama-compatible endpoint on an unmonitored host,
- binds services to the public internet,
- or pastes hostile content into a privileged session.

Those are **operator decisions**. The product’s duty is:

1. **Safe enough by default** for a single user or single trusted team on private infrastructure.
2. **Friction that forces consideration** before expanding risk—especially routes, plugins, and network-facing endpoints.
3. **Honest documentation** of accepted risks when those switches are flipped.
4. **Auditability** so consequences are visible after the fact.

We do not try to build a nanny for root. We try to make the *default path* hard to stumble into catastrophe, and the *powerful path* require an explicit “I meant to do that.”

---

## Opt-in is the security control

Powerful surfaces must be **off until turned on**, with configuration that lives where the operator can see it (typically host `.env` / env vars—not mutable runtime state inside a container).

Examples of the intended pattern (names may evolve; the pattern should not):

| Surface | Default | Why opt-in |
|---------|---------|------------|
| Plugin loading (`ADA_ENABLE_PLUGINS`) | Off | In-process code is full host trust for that process. |
| Ollama-compatible / honeypot endpoint | Off | Binds live credentials into a networked sandbox; accepted risk only when understood. |
| Unsandboxed execution | Off / fail-closed | Sandbox absence must not silently become “run anyway.” |
| Custom routes & exotic execution paths | Disabled or non-primary until configured | Routes change *how* work runs; enabling one should be a conscious routing decision. |
| Remote / third-party skills | Signed + sandboxed when allowed | Unknown code must not load as trusted host logic. |

**Implication for maintainers:** new powerful features ship **disabled**. Enabling them is a product moment—docs and env flags should state the risk in plain language. Convenience flags that auto-enable whole classes of untrusted code are a design bug.

**Implication for operators:** if you turn a route or plugin system on, you have accepted that path’s trust model. Review what you load. Prefer AI-authored, operator-reviewed code over third-party packs.

---

## Default security bar

Out of the box, for a **single user or single team** on private infrastructure, the codebase should be:

- **Sufficiently secure without further tuning** for normal harness use (local/private deploy, core routes, no third-party plugins).
- **Fail-closed** where isolation is required and cannot be guaranteed.
- **Secret-aware** on outputs (redaction) so routine logging and messaging do not casually spray credentials.
- **Input-hardened** enough to blunt casual injection—not as a complete solution, but as a speed bump.
- **Non-surprising** about what is executable: model text is not automatically host code; skills stay sandboxed; plugins stay off.

“Sufficiently secure” here means: a careful operator can run the public defaults without needing a security research team, and a careless third-party plugin ecosystem is not assumed or encouraged.

It does **not** mean: safe against a determined attacker with host access, or safe when the operator deliberately disables isolation.

---

## Self-authored tools (the happy path)

The preferred expansion of capability is:

```text
agent proposes → code lands in a reviewable place →
operator (or explicit promote policy) accepts →
skill (sandboxed) or plugin (admin, opt-in) loads
```

This eliminates most supply-chain risk. The public product should make that path easy and the third-party path possible but never ambient.

Personal intent of the project: **use the AI to develop its own tools, scripts, and processes.** Security intent of the public product: **that loop must not silently equate “model wrote it” with “host trusts it.”**

---

## Relationship to SECURITY.md

| Document | Role |
|----------|------|
| **SECURITY-INTENT.md** (this file) | Product posture, non-goals, trust model, default vs opt-in philosophy. |
| **SECURITY.md** | Concrete features, plugin vs skill rules, endpoint constraints, reporting, operational recommendations. |

When implementing or reviewing changes, ask:

1. Does this keep **defaults** safe for single-user / single-team private use?
2. Does any new power require an **explicit enable** with documented implications?
3. Does untrusted or model-emitted code gain a higher trust rung **without** a deliberate promote?
4. Are we accidentally designing for multi-tenant or internet-facing guarantees we do not offer?

If (3) is yes, reject or redesign. If (4) is yes, document the non-goal or cut the feature shape.

---

## Summary

- **One entity, private-by-intent, workflow agent—not multi-tenant public SaaS.**
- **Unknown code is the enemy; privilege promotion is the control.**
- **Defaults protect the common case; opt-in forces the operator to own the uncommon case.**
- **We do not save admins from admin choices; we make those choices visible and deliberate.**
- **Self-authored tools are the intended power path; third-party is unsupported trust.**

That is the lens. Mechanisms live in SECURITY.md and in the fail-closed defaults of the code.
