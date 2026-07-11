# Partner Engineering Rules (AGent / AGent-Ada)

These rules apply to all implementation work by human or AI partners.
Goal: code that works for **any operator on any machine**, not only a single host.

---

## 0. Goal

Prefer **config + small modules** over one-off hardcoding.
Write for a stranger installing the public tree. Private Enuclea features stay optional and out of the public core.

---

## 1. Never hardcode filesystem paths

### Forbidden in runtime code

Applies to: `src/`, `discord/`, `workers/`, and any plugin shipped publicly.

- Absolute user paths such as `/home/dan/...`, `/Users/...`, `/home/ada/...`
- Machine-specific repo roots such as `/home/dan/AGent` or `/home/dan/AGent-Ada`
- Hardcoded secrets directories, DB paths, or binary paths unique to one install

### Required instead

**Project root**
- Resolve from `__file__` / package path
- Or use `Path.cwd()` only when that is documented for the entrypoint

**User data**
- `Path.home() / ".agent"`
- Or env vars such as `AGENT_DB_PATH`, `ADA_DATA_DIR`

**Workspaces the agent may touch**
- Env-driven list (for example `ADA_WORKSPACES`, colon-separated)
- Or paths provided by compose mounts
- Never bake a host home path into Python source

**CLI binaries (`agy`, `grok`)**
- `shutil.which("agy")` / `shutil.which("grok")`
- Or env such as `ANTIGRAVITY_HARNESS_PATH`

**Allowlists (Discord attachments, file sends, etc.)**
- Build from env + safe defaults such as `/tmp` and `/app` + configured workspaces
- Do not hardcode fixed home directories

**Tests**
- Use `tmp_path`, `tempfile`, or repo-relative paths
- No `/home/dan` (or any operator-specific absolute path) in tests

**Docs and examples**
- Placeholders only: `/path/to/workspace`, `$HOME`, `${ADA_WORKSPACE}`
- Never a real username path

**Docker**
- If a path must be absolute inside a container, use container paths (`/data`, `/app`) set via compose or env
- Do not put host home directories in Python source

### Path definition of done

Before finishing any change that touches paths, run:

```bash
rg -n '/home/dan|/Users/dan|/home/ada' src discord workers tests || true
```

Expect: no matches in runtime code. One-off scratch scripts only if clearly non-shipped and not part of the public product.

---

## 2. Modular by default

### Prefer

- One module approximately equals one job (broker, routing, storage, security pipeline, Discord events, and so on)
- Public API via small functions or classes (for example `get_shared_broker()`, route interfaces)
- Thin compatibility shims when needed (for example private `enuclea.api_broker` re-exporting core)
- New features as plugins, routes, or tools behind flags — not stuffed into god modules when avoidable

### Avoid

- Growing god files (`api/chat.py`, `discord/bot.py`, `core/keyless.py`, and similar) when a new module would do
- Duplicating the same logic in public AGent-Ada and private AGent
- Shared logic: put it in `src/agent/...`
- Enuclea-only logic: keep it under private plugins / `enuclea/`

### When changing a large file

Extract new behavior into a named module and call it from the large file.
Do not add large amounts of new domain logic inline without a written reason in the PR or task summary.

### Modularity definition of done

- New capability has a clear home module
- Public core can load without private-only dependencies
- Optional integrations fail soft (`try` / `except ImportError`) or stay behind env flags

---

## 3. Never hardcode tokens, keys, or passwords

### Forbidden in any committed file

- API keys, bot tokens, OAuth refresh tokens
- `INTERNAL_API_SECRET`, webhook secrets, private keys, connection passwords
- Live production credentials of any kind
- "Temporary" keys in source "just for testing"

### Required

**Runtime secrets**
- Environment variables or gitignored `.env` only

**Examples**
- `.env.example`, `*.example.json`
- Placeholders only (`change-me`, `your-...-key`)

**Optional integrations**
- Commented-out placeholders in example files

**Tests**
- Fixtures, mocks, or test env vars
- Never production values

### Also required

- Do not log secrets (full tokens, `Authorization` headers, raw `.env` dumps)
- Prefer existing redaction / security pipeline for outputs that might echo secrets
- If a secret appears in git history by mistake: stop, rotate the secret, tell the operator — do not only delete the line and move on

### Secrets definition of done

Heuristic scan before finish (not perfect — still think):

```bash
rg -nI 'api[_-]?key[[:space:]]*=[[:space:]]*["'\''][a-zA-Z0-9]{12,}|sk-[a-zA-Z0-9]{10,}|ghp_[a-zA-Z0-9]+|xox[baprs]-' \
  src discord workers config --glob '!**/*.example*' || true
```

No real credentials in diffs. Examples only.

---

## 4. Public vs private (AGent-Ada vs AGent)

### AGent-Ada (public)

- Portable harness only
- No Enuclea business modules required for core operation
- No host-specific volume mounts in default compose
- Safe defaults: sandbox on, plugins off unless opted in

### AGent (private)

- May keep Enuclea plugins, shop compose, and local ops
- Shared core still follows sections 1–3 so Ada stays clean when syncing

### Sync rules

When copying "identical" files between trees, only sync files that should truly match
(for example `install.sh`, shared core modules, shared docs sections).

Do not "fix" the private tree by deleting `enuclea` or forcing public compose onto the shop stack.

---

## 5. Defaults for strangers

Unless the task explicitly says "operator host mode" or equivalent:

- `ADA_DISABLE_SANDBOX=0` (sandbox on)
- `ADA_ENABLE_PLUGINS=0`
- Dangerous endpoints off by default (for example `ADA_ENABLE_OLLAMA_ENDPOINT=0`)
- Compose must not override `.env.example` toward less safe defaults without documenting why

---

## 6. Finish checklist

Before reporting a task done:

1. No new operator-specific absolute paths in runtime code or tests
2. New logic lives in a focused module (or a justified extension of an existing one)
3. No secrets in source; examples use placeholders only
4. Public tree does not gain private-only required dependencies
5. Any new setting is documented in `.env.example` (or equivalent)
6. Tests added or updated for the change; relevant suite passes
7. README / SECURITY only claim features that exist and are wired

---

## 7. Short system prompt

Use this block when starting a one-off partner session without full project rules loaded:

```text
Engineering constraints for all code changes:
1) Paths: Never hardcode host absolute paths (/home/dan, /Users/...). Use env vars, Path.home(), package-relative paths, or container paths (/app, /data). Allowlists must be config-driven.
2) Structure: Prefer modular modules with clear APIs over growing god files. Shared logic in src/agent; private Enuclea features stay private and optional.
3) Secrets: Never hardcode API keys, tokens, passwords, or private keys. Use environment variables and .env.example placeholders only. Never commit real secrets or log them.
4) Public defaults: safe (sandbox on, plugins off) unless the task says otherwise.
5) Verify with a search for /home/dan (and similar) plus a quick secret heuristic before claiming done.
```

---

## 8. Optional later enforcement

Rules alone help. Light automation helps more:

- Fail CI or pre-commit if `/home/dan` appears under `src`, `discord`, `workers`, or `tests`
- Fail if `.env` or real `api_keys.json` is staged
- Optional: `detect-secrets` or gitleaks on push

Add those as a separate task; sections 1–7 are enough for day-to-day partner work.
