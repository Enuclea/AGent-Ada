# AGent-Ada: Task & Route Failover Plan

This document outlines the cost-aware routing and failover strategies configured for the **agy**, **Magica**, and **1Min AI** custom routes within the AGent-Ada AI orchestration engine.

---

## 1. Cost Profiles & Candidate Selection

We categorize models based on their verified test costs to minimize usage credit drain:

| Provider / Model Family | Specific Model ID | Cost (Test Units) | Preference Class | Routing Behavior |
| :--- | :--- | :--- | :--- | :--- |
| **Grok** | `grok-4-fast-reasoning` / `grok-4.3` | **136** | **Preferred** | Primary failover target. |
| **Gemini** | `gemini-3.5-flash` | **455** | **Preferred** | Primary failover target & generic default. |
| **Claude Opus** | `claude-opus-4-8` | **2475** | **Reasonable** | High-reasoning fallback candidate. |
| **DeepSeek** | `deepseek-v3.2` | **5400** | **Expensive** | Avoided unless in Boardroom or specifically requested. |
| **GPT** | `gpt-5.5-pro` | **18090** | **Very Expensive** | Avoided unless in Boardroom or specifically requested. |

> [!IMPORTANT]
> The expensive **DeepSeek** (Magica) and **GPT** (1Min AI) models are excluded from automated candidate lists and failover queues to prevent unexpected billing. They are allowed *only* during Boardroom debates (determined by `boardroom` in the `conversation_id`) or when specifically requested by the user.

---

## 2. Hard Failover Rules

Our retry and failover routing behaves differently depending on the root cause of the execution failure:

### A. Quota Limits & Rate Constraints (e.g. HTTP 429)
* **Strategy**: Continue with the same model.
* **Mechanism**: If a model returns HTTP 429 or a rate limit message, the route will retry the same candidate (up to 3 total attempts) with exponential backoff before shifting to the next model in the chain.

### B. Congestion, Timeouts, or Lack of Response
* **Strategy**: Pointedly use a different model.
* **Mechanism**: If a model request times out (e.g., `TimeoutError`) or returns service errors (HTTP 502/503/504), the route bypasses all retries and immediately shifts to the next candidate model in the queue to recover the request without delaying.

---

## 3. Route Executions & Mappings

### Built-in agy Route (`agy.py`)
* **Default Candidate Chain**: `gemini <-> claude`
* **Behavior**: If Gemini experiences a timeout/congestion or rate limit, we failover to Claude. We stay within the `agy` route and attempt execution on both models before failing the route execution completely.
* **Failover Logic**: Rate limit/quota errors on Gemini retry Gemini before trying Claude; timeouts on Gemini immediately switch to Claude (which also retries on quota and fails over immediately on timeout).

### Magica Route (`magica_route.py`)
* **Default Target**: `gemini-3.5-flash`
* **Default Failover Chain**: `[Target Model] -> gemini-3.5-flash -> grok-4.3 -> claude-opus-4-8` (DeepSeek excluded unless Boardroom/specifically asked)

### 1Min AI Route (`one_min_route.py`)
* **Default Target**: `gemini-3.5-flash`
* **Default Failover Chain**: `[Target Model] -> grok-4-fast-reasoning -> gemini-3.5-flash -> claude-opus-4-8` (GPT excluded unless Boardroom/specifically asked)

---

## 4. Test Verification Summary

All route configurations are fully covered by pytest suites:

* **agy Suite (`test_agy_runner.py`)**:
  - `test_agy_failover_quota_vs_congestion` (PASSED)
* **Magica Suite (`test_magica_payloads.py`)**:
  - `test_magica_payload_sizes_mocked` (PASSED)
  - `test_magica_cost_routing_and_failover` (PASSED)
  - `test_magica_failover_quota_vs_congestion` (PASSED)
* **1Min AI Suite (`test_onemin_runner.py`)**:
  - `test_onemin_mocked_payloads` (PASSED)
  - `test_onemin_integration` (PASSED)
  - `test_onemin_failover_quota_vs_congestion` (PASSED)
