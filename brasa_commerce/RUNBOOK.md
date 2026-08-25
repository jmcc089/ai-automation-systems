# Brasa Commerce — runbook

Procedures, not architecture. What to run, in what order, and what the answer means.

Everything here was executed against the running system on 2026-08-08, not written from the code.
Where a step could not be run, it says so.

**Read this first: n8n is shared.** One Railway service (`n8n`, project *AI Solutions Architecture*)
runs Brasa, Cedar and Holt. Restarting it stops all three. So does saving a broken workflow, because
n8n's public API replaces the whole node array in one write. Nothing in this file that touches n8n is
a casual action.

**Read this second: the email budget is the real constraint.** Resend's free plan is **100 sends per
day**, shared across all three builds. One intake spends up to 2, one checkout 1, one approval 1. This
pass exhausted the quota twice in one day with a few dozen legitimate probes. Before any procedure
below that sends, check what is left.

---

## Is it working?

**The cheap check — costs nothing.** No email, no model call, no customer row. It proves the whole
front half of the system: DNS, Railway, n8n, the workflow's first nodes, and the write to Postgres.

```bash
curl -s -w '\n%{http_code} in %{time_total}s\n' \
  -X POST https://n8n-production-3503.up.railway.app/webhook/brasa-intake \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"runbook-probe","hello":"world"}'
```

Healthy: **HTTP 400** in **0.4–1.3 s**, with

```json
{"success":false,"status":"invalid_request","error":"body.data is missing or is not an object",
 "message":"The request was rejected before anything was written or emailed."}
```

Then confirm the refusal was *recorded*, which is the half that matters:

```sql
select n8n_execution_id, status, stage, duration_ms, what_happened
from bc_workflow_runs order by created_at desc limit 1;
```

Expect `rejected · Guard Intake` and a duration in **single-digit milliseconds**.

Those two numbers together are the useful part, and they are not a contradiction: the workflow does
1–8 ms of work and the caller waits about a second. Effectively all of that second is n8n-on-Railway
accepting and routing the request. It is the floor under every latency figure in this build, and if
the *client* time climbs while `duration_ms` stays flat, the problem is the platform, not the
workflow.

**What this check does NOT prove**: that DeepSeek answers, that Resend delivers, or that retrieval
works. It is a liveness check, not a health check. Read the next section before trusting it alone.

**200 is a failure here.** A `200` from this request means `Guard Intake` has been removed or rewired,
and the system is back to the behaviour of 2026-08-06, when a malformed body returned `200` with an
empty body after dying three nodes later.

**The full check — costs 2 sends and one classification.** Use it after a deploy, a key rotation, or
whenever the cheap check passes and something is still wrong. Use an address you own.

```bash
curl -s -w '\n%{http_code} in %{time_total}s\n' \
  -X POST https://n8n-production-3503.up.railway.app/webhook/brasa-intake \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"rb-'"$(date +%s)"'","eventType":"FORM_RESPONSE","data":{
       "responseId":"rb-'"$(date +%s)"'","fields":[
        {"label":"Full name","value":"Runbook Probe"},
        {"label":"Email address","value":"you+probe@example.com"},
        {"label":"Phone number","value":"+1 312 555 0147"},
        {"label":"Message","value":"Does the 10 inch glass lid fit the No.10 cast iron skillet?"}]}}'
```

Healthy: **200** with a `ticket_id`, in **5–17 seconds** (p50 ≈ 8.5 s, p95 ≈ 16 s — measured, and
slower than it feels like it should be because 91 % of it is the model). Then:

```sql
select status, stage, duration_ms, model, total_tokens, intent, escalate_reasons,
       error_node, what_happened
from bc_workflow_runs order by created_at desc limit 1;
```

`status = ok`, `model = deepseek-v4-flash`, non-zero tokens. Two readings that look like success and
are not:

- **`model = skipped`** — the classifier was bypassed. Either the message had fewer than ten letters
  or digits, or DeepSeek failed and `Model Unavailable` caught it. Check `escalate_reasons` for
  `model_unavailable`.
- **`status = ok` with an `error_node`** — the run completed but something inside it failed and was
  recorded rather than raised. 47 rows are like this from the quota exhaustion of 2026-08-07.

**Never reuse an idempotency key.** `data.responseId` on intake, `idempotency_key` on checkout. A
repeat is refused by a unique index by design: you get **200 and nothing happens**, which is
indistinguishable from a silent failure. `$(date +%s)` above avoids it.

---

## When it breaks

Work down this list. It is ordered by how often each has **actually** been the cause, counted from
`bc_workflow_runs` rather than guessed:

```
Send Team Email       28        Retrieve Products      3
Send Customer Email   24        Validate Input         3   (a refusal, not a failure)
Guard Intake           7  (a refusal, not a failure)   DeepSeek API           1
Send Approved Email    7        Create Shipment        1
```

**59 of 74 recorded failures are the email nodes, and almost all of them are one cause: the daily
quota.** Start there.

### 1. `bc_workflow_runs` — always start here

It is the durable record, and it survives things n8n does not. n8n prunes its own execution history
to the last handful; this table has no retention policy.

```sql
select started_at, status, stage, duration_ms, error_node, what_happened, source_event_id
from bc_workflow_runs order by started_at desc limit 20;
```

`what_happened` is one readable line per run, computed on write, so you do not need to know which
column means what. The status vocabulary is deliberate and each value means something different:

| status | meaning | who is at fault |
|---|---|---|
| `ok` | the run finished | nobody — **but check `error_node`** |
| `rejected` | refused at the door by `Guard Intake` or `Checkout Valid?` | the caller sent something invalid |
| `blocked` | an external limit, e.g. the Resend daily quota | nobody — an unpaid plan is not a defect |
| `error` | the system failed | us |
| `started` | never finished, or the run died before recording an outcome | investigate |

**The standing query for the worst class of bug** — a run that ended without recording anything:

```sql
select * from bc_workflow_runs where status = 'started' and duration_ms is null
  and started_at < now() - interval '5 minutes';
```

Nothing runs this on a schedule. That is the gap that let three orphan rows sit unnoticed on
2026-08-07; it is how they were eventually found.

**Run on 2026-08-08 it returns 12 rows, and all twelve are historical** — three from the
model-skipped path before it was fixed, nine from before this table recorded refusals at all. They
are left in place as the evidence for those findings. A row appearing here with a **recent**
`started_at` is the thing to act on.

### 2. Emails failing — nearly always the quota

```
Send Customer Email failed: You have reached your daily email sending quota. | … | HTTP 429
```

Check Resend's dashboard for today's count. The quota resets at **00:00 UTC**. Nothing is lost: the
ticket sits at `approved` rather than `sent`, which is the visible, truthful record of a reply that
exists and did not go out.

```sql
select id, status, created_at from bc_support_tickets
where status = 'approved' order by created_at desc;
```

**These are not retried automatically, on purpose.** Re-approving is refused by the idempotency guard,
and a retry that cannot distinguish a refused send from a delivered one would re-email customers. To
send one deliberately, a human decides and does it by hand.

Since 2026-08-25 the agent panel shows them without this query: they carry their own counter, read
*Email not sent*, and their approve button is disabled with the reason on it. When one no longer
matters, **Close, no reply** moves it to `closed` and files the reason as an internal note — the
26 rows left by the 2026-08-07 quota exhaustion are cleared that way, by hand. Run the query above
when you want the list outside the panel, or to count what is outstanding.

If the message says `You can only send testing emails to your own email address` instead, the sender
has regressed to Resend's sandbox — see `INCIDENTS.md` · *Every email failed and every record said it
succeeded*. That is a real defect, not a quota.

### 3. `DeepSeek API` — the model did not answer

Since 2026-08-08 this is **not** an outage. The node's error output goes to `Model Unavailable`, so a
ticket is still written and escalated to a human; the run records `escalate_reasons` containing
`model_unavailable` and `model = skipped`.

If you see a *run* in `error` at this node, the fallback itself is broken. Check that
`DeepSeek API` still has `onError: continueErrorOutput` and that its error output is still wired.

A long run that ends at 25 s is the node's timeout. That bound was chosen deliberately: the slowest
classification ever observed here is ~18 s.

### 4. `Retrieve Products` / `Retrieve Order` — Postgres function or key

`Could not find the function public.search_products` means the request never had a usable message —
almost always a malformed body reaching a node that assumes one. Since `Guard Intake` exists this
should be impossible from outside; if it happens, the guard has been bypassed.

`42501 permission denied for function search_order` from the **workflow** means n8n is using the wrong
Supabase key. That error from **outside** with the anon key is correct and expected.

### 5. `Create Order` — one call, one transaction

Since 2026-08-08 the order, its line items and its shipment are written by a single call to
`bc_place_order(jsonb)`. It is a plpgsql function, so it is one transaction: a failure anywhere inside
leaves **nothing**. `Insert Order Items` and `Create Shipment` no longer exist as nodes.

A failure here therefore means no order at all, which is the correct outcome and is visible in the run
log as `error` at `Create Order`. Read `error_message` — a violated CHECK or foreign key will name
itself, and `refusing to write an order with no line items` is the function's own guard.

The old defect is still worth being able to find, because one historical example remains:

```sql
select o.order_number, o.status, o.created_at
from bc_orders o left join bc_shipments s on s.order_id = o.id
where s.id is null order by o.created_at desc;
```

Expect exactly one row, `BC-6ZHJ2SSD` from 2026-08-07, kept as evidence. **A second row appearing
means the checkout has been rewired back to separate writes.**

---

## Stop and start

**Stopping Brasa alone**: deactivate the workflow in n8n (`Brasa Commerce — Support Desk`,
`1Ta9kHrR2dk8akKd`). The three webhooks stop answering; Cedar and Holt are untouched. This is almost
always what you want.

**Restarting n8n itself** (Railway → project *AI Solutions Architecture* → service `n8n` → Redeploy)
**stops all three builds** for the duration. In-flight runs are lost and, because the response node
never fires, their callers get no answer. Do not do this to fix one workflow.

---

## Editing the workflow safely

Two traps here cost this assurance pass real time. Neither is guessable from the n8n UI.

**Always back up first.** The API returns the whole workflow; keep it outside the repo.

```bash
curl -s -H "X-N8N-API-KEY: $N8N_KEY" \
  "$N8N_URL/api/v1/workflows/1Ta9kHrR2dk8akKd" > brasa-backup-$(date +%s).json
```

**Trap 1 — `PUT` accepts only four keys, and `settings` only two.** The body must be exactly
`{name, nodes, connections, settings}`, and `settings` may contain only `executionOrder` and
`errorWorkflow`. Passing back what `GET` returned fails with
`request/body/settings must NOT have additional properties`. n8n preserves the rest server-side.

**Trap 2 — parallel branches run in canvas order, not connection order.** With
`executionOrder: v1`, when one node feeds two, n8n runs them depth-first sorted by position (x, then
y). A branch placed lower on the canvas **never runs** if the first branch aborts the execution. This
silently swallowed a logging node three separate times during this pass. Either chain the nodes so the
order is a property of the graph, or set the y position deliberately and write down why.

**After any edit, count the nodes.** **53** as of 2026-08-08 — it was 55 until `Insert Order Items`
and `Create Shipment` were folded into the checkout's single transaction.

```bash
curl -s -H "X-N8N-API-KEY: $N8N_KEY" "$N8N_URL/api/v1/workflows/1Ta9kHrR2dk8akKd" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["nodes"]),"nodes, active:",d["active"])'
```

Then run the cheap check, then the full check.

---

## Rotating a key

`SUPABASE_SERVICE_KEY`, `DEEPSEEK_API_KEY` and `RESEND_API_KEY` are Railway environment variables on
the `n8n` service, read at runtime as `$env.NAME`. Change the variable, redeploy — **which restarts
n8n and therefore stops Cedar and Holt too**.

**The service-role key is the urgent one.** n8n redacts the `authorization` header in its saved
execution data but **not** `apikey`, and Supabase requires the same token in both — so the key that
bypasses every RLS policy sits in plaintext in n8n's execution history. Confirmed in execution `381`.
Rotating it invalidates those copies. Deleting old executions helps and destroys evidence, so decide
which you need first.

After rotating: run the cheap check (proves Supabase), then the full check (proves DeepSeek and
Resend). A wrong Supabase key shows up as `Retrieve Products` failing; a wrong DeepSeek key now shows
up as `model_unavailable` rather than an outage, which is easy to miss.

---

## Measuring it

Both scripts hit production and both cost sends. Read their docstrings before running.

```bash
python3 brasa_commerce/eval/run.py --repeat 5     # the frozen 19-case eval set; no rows, no email
python3 brasa_commerce/eval/latency.py --n 20     # 35 requests, ~45 sends
python3 brasa_commerce/eval/load.py --path checkout   # the ramp; 16 sends
python3 brasa_commerce/eval/sender_probe.py --verify  # does a real recipient receive the mail?
```

`run.py` is the only one that is free. It sends nothing and writes nothing — it exercises the part of
the system that can be *wrong* rather than merely fail.

---

## Restoring after a bad change

**This is the weakest section in this file, and it is weak because the backups are.**

- **The workflow**: restore the JSON you took above with a `PUT`. This works and has been done
  repeatedly. If you did not take one, n8n's own version history is the only option and it is short.
- **The database**: Supabase's automatic backups on the free plan are daily and point-in-time
  recovery is not enabled. A destructive migration is recoverable only to the previous day.
  **There is no tested restore procedure**, because restoring has never been rehearsed on this
  project. Writing one would mean deliberately breaking a copy and recovering it.
- **The front ends**: both Netlify sites deploy from git; roll back by redeploying a prior commit.

The realistic mitigation is the one above: back up the workflow before touching it, and prefer
reversible migrations.

---

*Last executed 2026-08-08.*
