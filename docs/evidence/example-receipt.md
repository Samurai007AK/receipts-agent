# Receipt: VERIFIED

**Task:** Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our AP system, and tell me once it is done.

Run `live-2` · 16 steps · 20 LLM calls · 41296 tokens · 76.8s · models: openai/gpt-oss-120b, qwen/qwen3.8-27b, openai/gpt-oss-20b

## Contract

- ✓ Exactly 1 ledger row for INV-1042 — observed: `1`
- ✓ Ledger INV-1042: amount equals 1,240.00 — observed: `1240.0`
- ✓ Ledger INV-1042: due date equals 2026-11-02 — observed: `2026-11-02`
- ✓ A message to alex@company.test mentions Invoice INV-1042 has been entered into the AP system — observed: `message 1 to alex@company.test: 'INV-1042 posted'`

## Done

- **invoice INV-1042** [DONE] amount=1240.00, due_date=2026-11-02, invoice_no=INV-1042, vendor=Acme Corp
- **notification notify:alex@company.test** [DONE] body=Invoice INV-1042 has been entered into the AP system., subject=INV-1042 posted, to=alex@company.test

## Deliberately NOT done

- nothing

## Waiting on you

- nothing

## Unexpected changes

- none

## Evidence

- `GET /api/ledger?vendor=Acme%20Corp&invoice_no=INV-1042` → 200: `[{"id":3,"vendor":"Acme Corp","invoice_no":"INV-1042","amount":1240.0,"due_date":"2026-11-02","status":"open","created_at":"2026-10-04T16:49:49","source":"web"}]`
- `GET /api/ledger?vendor=Acme%20Corp&invoice_no=INV-1042` → 200: `[{"id":3,"vendor":"Acme Corp","invoice_no":"INV-1042","amount":1240.0,"due_date":"2026-11-02","status":"open","created_at":"2026-10-04T16:49:49","source":"web"}]`
- `GET /api/ledger?vendor=Acme%20Corp&invoice_no=INV-1042` → 200: `[{"id":3,"vendor":"Acme Corp","invoice_no":"INV-1042","amount":1240.0,"due_date":"2026-11-02","status":"open","created_at":"2026-10-04T16:49:49","source":"web"}]`
- `GET /api/outbox?to=alex@company.test` → 200: `{"id": 1, "to": "alex@company.test", "subject": "INV-1042 posted", "body": "Invoice INV-1042 has been entered into the AP system.", "sent_at": "2026-10-04T16:50:27"}`

## Agent's summary

Entered Acme Corp invoice INV-1042 into the AP system (amount 1240.00, due 2026-11-02) and sent a notification to alex@company.test confirming INV-1042 has been entered into the AP system. Both writes were VERIFIED in the system of record.
