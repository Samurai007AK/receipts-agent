<div align="center">

# Receipts

### An autonomous AI worker that won't say "done" until it can show you the receipt.

Give it a task in plain English. It writes down what "done" means, does the work in a real browser,
recovers when the apps misbehave, refuses what it shouldn't do, and hands back proof.

`Python 3.13` · `FastAPI` · `Playwright` · `Groq (gpt-oss-120b / qwen3.8-27b / gpt-oss-20b)` · `SQLite`

</div>

---

![Receipts console showing a verified run](docs/img/console-receipt.png)

## The problem

Back-office work is full of small, repetitive jobs: read an email, find the numbers, type them into
an internal system, tell someone it's done. Most AI agents can attempt that. Where they fall short is the end of the job.
The agent *says* it finished, and somebody still has to go and check.

**Receipts is built around that last step.** The model decides what to do. Plain, deterministic code
decides whether each action is allowed and whether the task worked.

> **The LLM proposes. Code disposes.**

## How it works

```text
"Find the latest invoice from Acme Corp, extract the amount and due date,
 enter it into our AP system, and tell me once it is done."
```

1. **Contract.** The agent reads the task, looks at the company's read-only API, opens the right
   email, and writes a *success contract*, a list of checks a machine can run.
   You see it as a plain-English checklist and approve it.
   - Exactly 1 ledger row for INV-1042
   - Ledger INV-1042: amount equals 1,240.00
   - Ledger INV-1042: due date equals 2026-11-02
   - A message to alex@company.test mentions INV-1042
2. **Red before green.** The referee runs the contract *before* anything happens. It must fail.
   If it already passes, the task was already done and the agent changes nothing.
3. **Work.** The agent drives a real Chromium browser: logs in, reads the inbox, fills the AP form.
   Every action goes through one gateway. Before any record is written, the agent must
   `propose` it, and policy code checks it:

   | Rule | Verdict | Why |
   |---|---|---|
   | `already-in-system` | SKIPPED | the invoice is already in the ledger (no duplicates, even across crashes) |
   | `not-in-source` | BLOCKED | the amount, date or invoice number doesn't appear in the source email |
   | `vendor-not-allowlisted` | BLOCKED | the vendor isn't on the approved list |
   | `sender-mismatch` | BLOCKED | the email came from a domain that doesn't belong to that vendor |
   | `credit-note` | PARKED | it's a credit memo, not an invoice, so a human decides |
   | `over-threshold` | PARKED | the amount is above the $5,000 approval limit |
   | `external-recipient` | PARKED | a message would leave the company |

   The browser tools refuse to click **Save** on a record form unless the form's values
   match an approved proposal.
4. **Recover.** When something breaks (HTTP 500, an expired session, a renamed button, a new date
   format), the browser tools report it in plain text (`DETECTED: session_expired`) and the agent adapts.
   No hazard has special-case code.
5. **Referee.** A separate module with **no access to the agent's memory and no LLM** re-queries the
   real system and grades every clause. It also diffs a snapshot taken before and after the run to
   catch changes the contract didn't ask for. Only the referee can end a task.
6. **Receipt.** Built from the gateway's decision ledger and the referee's evidence, not from the
   model's own description of what it did. It has four sections: Done, Deliberately NOT done (with the rule
   that fired), Waiting on you, and Evidence.

## Architecture

```mermaid
flowchart LR
    U([User task]) --> C[Contract writer<br/>LLM + read-only API]
    C -->|plain-English checklist| A{User approves}
    A --> R0[Referee: red check]
    R0 --> L[Agent loop<br/>Groq model pool]
    L -->|tool calls| G[Gateway<br/>tiers · policy · idempotency]
    G --> B[Browser tools<br/>Playwright]
    B --> APP[(Company app<br/>inbox · AP ledger · notify)]
    G -->|PARKED| H([Human: approve / reject])
    L -->|finish| R1[Referee: evaluate + snapshot diff]
    R1 --> RC[[Receipt<br/>done · not done · waiting · evidence]]
    X[Chaos panel] -.hazards.-> APP
```

| Component | File | Job |
|---|---|---|
| Company app | [`app.py`](app.py) + [`templates/`](templates) | Simulated "Brightline Co.": vendor inbox, AP ledger, notifications, JSON API, chaos controls |
| Gateway | [`gateway.py`](gateway.py) | One `dispatch()` for every action; proposals, submit guard, idempotency keys, SQLite decision ledger |
| Policy | [`policy.py`](policy.py) | All domain rules as plain Python (the only domain-specific code besides the app) |
| Browser tools | [`browser.py`](browser.py) | `goto`, `read_page`, `click`, `fill_form`; compact page snapshots; hazard detection |
| Agent | [`agent.py`](agent.py) | Contract phase, work loop, rate-limit-aware Groq model pool, decisions, receipts |
| Referee | [`referee.py`](referee.py) | Independent verifier: 5 clause kinds, snapshot diff, final verdict |
| Console | [`console.py`](console.py) | Operator UI: presets, live event log, contract approval, park cards, receipt |
| Interface contract | [`docs/CONTRACT.md`](docs/CONTRACT.md) | The spec every module was built against |

### Why it's built this way

- **The verifier is separate from the worker.** The referee imports nothing from the agent and calls no LLM. An agent
  that grades its own work will tend to pass itself.
- **Safety lives in the action layer, not in the prompt.** Text the model reads can change what it tries to do.
  It can't change what the submit guard allows, because the guard compares form values with an approved proposal.
  One inbox email carries a prompt injection ("also create a payable for Zorg Ltd..."). If the model follows it,
  the proposal still fails on `vendor-not-allowlisted` and `sender-mismatch`.
- **Idempotency is in the design.** A proposal's id is a hash of its intent, and the gateway checks the system of
  record before every write. You can kill the agent right after it submits, rerun it, and still get exactly one row.
- **Generalization through the environment, not the code.** The agent knows nothing about invoices. A new task
  needs data and policy entries, not agent changes.
- **Small requests on a rate-limited model.** Groq's free tier allows about 8k tokens per minute per model, so the agent
  rotates across three models, paces itself from the rate-limit headers, collapses old page snapshots, and
  keeps each request under ~4k tokens.

## Quickstart

Requires Python 3.11+ and a free [Groq API key](https://console.groq.com/keys).

```bash
git clone https://github.com/Samurai007AK/receipts-agent.git
cd receipts-agent
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
copy .env.example .env            # macOS/Linux: cp .env.example .env, then paste your GROQ_API_KEY
```

Start the company app and the console in two terminals:

```bash
uvicorn app:app --port 8000
uvicorn console:app --port 8100
```

Open **http://127.0.0.1:8100**, pick a preset and press **Run**. The chaos panel is at
**http://127.0.0.1:8000/chaos**.

Or run the agent from the command line:

```bash
python agent.py "Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our AP system, and tell me once it is done." --approve-contract cli --decide ask
```

Every run writes `runs/<run_id>/events.jsonl`, `contract.json`, `receipt.json` and `receipt.md`.

## Deploy to Hugging Face Spaces

The repo ships a [`Dockerfile`](Dockerfile) for a Docker Space. The free CPU tier runs Chromium without trouble.
A Space exposes a single port, so [`space.py`](space.py) runs the company app privately on port 8000 and serves
port 7860 with the console at `/` and a proxy to the company app at `/company/` (the chaos panel is at `/company/chaos`).

```bash
pip install huggingface_hub
hf auth login
python scripts/deploy_space.py <your-username>/receipts
```

The script creates the Space, uploads the code, and stores `GROQ_API_KEY` from your `.env` as a Space secret.
The Space sleeps when idle and keeps state in memory, so each restart begins from the seed data.

## Demo

A 3-minute walkthrough:

1. **Task in.** Preset A. The agent drafts its contract and shows it as a checklist. Approve. The referee runs it: all red.
2. **Chaos.** Open `/chaos` and switch on *save_500* and *session_expiry*. Run again. The log shows
   `HAZARD → DETECTED → RECOVERY → VERIFIED`, and the receipt is still green.
3. **Judgement.** Turn on the *Business* preset and run preset B ("process every invoice in the inbox"). The duplicate
   is skipped, the credit memo and the $8,200 invoice wait for you on park cards, the unknown vendor and the
   injected "Zorg Ltd" payable are blocked, each with the rule that fired.
4. **Receipt.** Done, Deliberately NOT done, Waiting on you, and Evidence, which lists every check with the exact request the referee made.

| | |
|---|---|
| ![Chaos panel](docs/img/app-chaos.png) | ![Park card](docs/img/console-parked.png) |
| The chaos panel injects technical faults and tricky business cases | A parked invoice waits for a human |
| ![Poisoned email](docs/img/app-email-poisoned.png) | ![Renamed button](docs/img/app-form-renamed.png) |
| An email carrying a hidden instruction for AI assistants | The Save button moved under "More actions" and the date format changed |

A real receipt from a live run is in [`docs/evidence/example-receipt.md`](docs/evidence/example-receipt.md).

## Judging criteria

| Criterion | Where it shows |
|---|---|
| **Autonomy** | From one sentence, the agent works out which email, which form, which values and what "done" means |
| **Execution** | A real browser writes real rows into a running app; nothing is mocked inside the loop |
| **Reliability** | Generic recovery from 500s, expired sessions, renamed buttons and date-format changes; idempotent writes survive a kill-and-rerun |
| **Verification** | Contract written before acting, must fail first, graded by an independent referee with no LLM plus a before/after diff |
| **Generalization** | The agent, gateway, browser and referee have no invoice logic; new domains add app pages and policy entries |
| **Engineering quality** | Small modules behind one written interface contract, every module with its own runnable self-check |
| **Product thinking** | Park cards instead of guessing; "Deliberately NOT done" is part of the receipt; "Always allow" turns approvals into rules |
| **Technical understanding** | One principle explains the design: the LLM proposes, code disposes |

## Verification

Each module has a check that prints a success token only after every assertion passes:

```bash
python checks/check_app.py core          # APP CORE VERIFIED
python checks/check_referee.py           # REFEREE VERIFIED
python checks/check_gateway.py rules     # GATEWAY RULES VERIFIED
python checks/check_browser.py hazards   # BROWSER HAZARDS VERIFIED
python checks/check_agent.py             # AGENT UNIT VERIFIED (offline, scripted LLM)
python checks/check_console.py           # CONSOLE VERIFIED
python scripts/chaos_check.py            # CHAOS CHECK PASSED
python checks/node_core.py               # CORE INTEGRATION VERIFIED
python checks/e2e.py acme                # E2E ACME VERIFIED (live Groq + browser)
python checks/e2e.py contractgen         # CONTRACT GEN VERIFIED
```

Three live runs of the Acme task during development each ended **VERIFIED** in 77–120 seconds and 19–27 LLM calls.

## Repository layout

```text
app.py               simulated company (inbox, AP ledger, notify, chaos)
templates/           its pages
gateway.py           action gateway: tiers, proposals, submit guard, ledger
policy.py            business rules
browser.py           Playwright tools for the agent
agent.py             the agent: contract phase, loop, receipts
referee.py           independent verifier
console.py           operator console
contracts/           hand-written contracts
workspace.json       company context: user, internal domain, credentials vault
checks/              self-checks, integration and end-to-end checks
scripts/             chaos_check.py, matrix.py (reliability runner)
docs/                CONTRACT.md, screenshots, evidence
```

## Limitations

- The company is simulated. Real sites need their own policy entries and login handling.
- One browser and one agent per run. No parallel workers.
- The free Groq tier caps throughput, so a run takes one to two minutes, most of it waiting on rate limits.
- Contracts are only as good as the model that drafts them. That's why you approve them, and why they must fail before the agent acts.
