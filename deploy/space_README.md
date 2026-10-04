---
title: Receipts
emoji: 🧾
colorFrom: green
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: An AI worker that proves every task it finishes
---

# Receipts

An autonomous AI back-office worker. Give it a task in plain English. It writes a machine-checkable
success contract, does the work in a real browser on a simulated company app, recovers from injected
failures, refuses what policy forbids, and only finishes when an independent referee confirms the result.

- `/` is the operator console. Pick a preset and press **Run**.
- `/company/chaos` is the chaos panel. Inject faults (HTTP 500, expired session, renamed button, date format)
  or business traps (duplicate invoice, credit memo, over-limit invoice, unknown vendor, prompt injection).
- `/company/` is the simulated company app (login `clerk` / `receipts123`).

The Space sleeps when idle and its state is in memory, so every restart begins from the seed data.
Source and full documentation: https://github.com/Samurai007AK/receipts-agent
