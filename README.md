# inbound-automation

Speed to lead, automated. A web form posts to a webhook, and within
milliseconds the lead is authenticated, validated, enriched, scored, routed,
stored, and (if it is hot) sent to Slack. It is built two ways:

- **[`inbound.py`](inbound.py)**: a standard-library Python service with
  HMAC request signing, validation, dedupe, enrichment, scoring, routing,
  SQLite storage and Slack alerts.
- **[`n8n/inbound-lead-workflow.json`](n8n/inbound-lead-workflow.json)**:
  the same flow as an importable n8n workflow, for teams that want to own it
  without writing code.

## Run the service

```
set WEBHOOK_SECRET=dev-secret                 # PowerShell: $env:WEBHOOK_SECRET = "dev-secret"
python inbound.py                             # listens on 127.0.0.1:8080/inbound

python inbound.py --sign sample_payload.json  # prints the X-Signature for that body
curl -X POST http://127.0.0.1:8080/inbound -H "Content-Type: application/json" \
     -H "X-Signature: <signature>" --data-binary @sample_payload.json

python -m pytest                              # 7 tests
node n8n/check_code_node.js                   # runs the n8n Code node's logic and checks the wiring
```

Response for the sample payload:

```json
{"status": "accepted", "email": "jamie.rivera@contoso.example", "company": "Contoso",
 "employees": 2400, "industry": "Software", "score": 100, "owner": "ae_enterprise"}
```

Set `SLACK_WEBHOOK_URL` to post real alerts. Without it, alerts print to
stderr as a dry run.

## What each step guards against

| Step | Result | Why |
|---|---|---|
| HMAC-SHA256 signature on the raw body, constant-time compare | 401 | Anyone can find a public webhook URL; only the form tool knows the secret |
| No `WEBHOOK_SECRET` set | refuses to start | An unauthenticated default is how webhooks get spammed |
| Body over 64 KB | 413 | Cheap protection against junk payloads |
| Schema validation | 400 with every problem listed | The form owner can fix all the problems in one pass |
| Same email + same form already seen | 200, no-op | Form tools retry and people double-click; reps should not get two alerts |
| Enrichment lookup | company, size, industry, country | Forms should ask for an email, not ten fields |
| Score ≥ 70 | Slack alert | Only hot leads interrupt a rep |

Enrichment reads [`firmographics.json`](firmographics.json), a stand-in for a
paid enrichment API, so the project runs with no accounts and no keys.
Replacing it with a real provider means changing one function.

## The n8n version

`Form webhook (header auth)` → `Validate and score` (Code node) → `Valid?` →
`Upsert to CRM` (HTTP) → `Hot lead?` → `Alert Slack` (HTTP).

Import it in n8n (Workflows → Import from file), add a Header Auth credential
to the webhook node, and set `CRM_UPSERT_URL`, `CRM_API_TOKEN` and
`SLACK_WEBHOOK_URL` as environment variables on the n8n host. The workflow
holds no credentials or URLs.

## Known limits

- One lock around one SQLite connection serializes requests. That is fine at
  form-submission volume; use a queue and a real database before sustained
  concurrent traffic.
- A failed Slack post is logged and not retried. The lead is still stored.

All data is synthetic.
