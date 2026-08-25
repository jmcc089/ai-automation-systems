# Brasa Commerce — incidents

Faults that reached production, what was decided, and what each decision cost. Timestamps are UTC
and cross-check against `bc_workflow_runs`, n8n's execution ids and the Supabase migration list.

An incident is here if it ran in production and someone had to give something up to close it. Bugs
found and fixed in the same hour are in the assurance record, not here.

---

## 2026-08-07 · Every email failed, and every record said it had been sent

### Symptom

No customer had ever received a reply from this system. Not "some bounced" — none, for the whole life
of the build.

Nothing showed it. `bc_support_tickets.status` read `sent` on every ticket. The webhook returned
`200` with a `ticket_id` to every caller. The n8n run showed no error. Three independent places all
reported success, and the mail had never left.

It was found by looking in Resend's own log rather than at the system.

### Diagnosis

Two faults stacked, and each one hid the other.

**The sender was Resend's sandbox address**, `onboarding@resend.dev`. That sender is not a
misconfiguration in any obvious sense — it works, it authenticates, it returns a message id. It
delivers to exactly one address: the Resend account owner's own. Every other recipient is refused at
the API with `You can only send testing emails to your own email address`. Every test anyone had ever
run used the owner's address, so it had always worked.

**`Insert Ticket` wrote `status: 'sent'` in the insert itself**, before any send was attempted. So
when the send was refused, the row still said `sent`. The status was not a record of a delivery; it
was a constant.

### What was ruled out

- **The Resend API key** — valid, and the sends that reached the owner's address were genuinely
  delivered.
- **DNS and domain verification** — nothing was wrong, because no verified domain was in use at all.
  This is the one that wasted the most time: the instinct is to check SPF and DKIM for a domain the
  system was not sending from.
- **The workflow's recipient handling** — correct. It read the customer's address and put it in the
  `to` field. The address was right and the sender was wrong.
- **Spam filtering** — the messages were never accepted for delivery, so there was nothing to filter.

### Decision and trade-off

Verify a domain and change the three `from` addresses. That part is not a trade-off, it is the fix.

The second half is the decision. `status` now moves in two phases: `approved` when a reply has been
decided and written, `sent` only after Resend accepts the message. **Nothing writes `sent` before the
send is attempted.**

**What was given up, deliberately: there is no automatic retry.** A ticket whose send failed sits at
`approved` and a human has to act on it. Automatic retry was considered and refused, because a retry
that cannot distinguish *refused* from *delivered* would re-email customers who already have the
reply — and the system had just spent its entire life unable to tell those apart. Visible and stuck
was chosen over automatic and possibly duplicating.

**Also accepted: rows written before the change cannot be repaired.** Tickets created before
17:31 carry `sent` regardless of what happened to the email, and there is no way to tell after the
fact which of them are lying. No backfill was attempted, for that reason. Only rows from 17:31 onward
can be trusted on this field.

### The decision's consequence, six hours later

The two-phase write started telling the truth almost immediately, and the truth was inconvenient.

By 21:00 the assurance pass had exhausted Resend's **free daily quota of 100 sends** with its own
probes. Every subsequent reply failed at the send. Under the old code all of those tickets would have
said `sent` and nobody would ever have known. Under the new code they sat at `approved` — **26 of
them** by the end of the day, plus 47 run rows carrying `HTTP 429` — and the queue showed exactly what
it should: replies that exist and did not go out.

Twenty-six is worth sitting with. That is not a handful of stragglers, it is most of an evening's
work, and every one of them would have been invisible the day before.

Two tickets failing minutes apart on either side of the change is the clearest evidence the fix
works: the one written before recorded `sent`, the one after recorded `approved`, and neither was
delivered.

The second consequence was that **the fix could not be confirmed the same day.** Proving it required a
successful delivery to an address that is *not* the account owner's exact address — precisely what the
sandbox sender used to refuse — and there were no sends left to spend. The verification was carried
openly as an unproven claim for about seven hours rather than being quietly assumed.

### Resolution and confirmation

- The verified domain `mail.mcruz-portfolio.space` replaced the sandbox sender.
- `Mark Intake Sent` and `Mark Ticket Sent` write `sent` after the send, on both paths that email a
  customer.
- **Confirmed 2026-08-08 00:34**, once the quota reset at midnight UTC:
  `brasa_commerce/eval/sender_probe.py --verify` → execution `595`, recipient
  `mcruzcardoza+sender-probe-1786149290@gmail.com`, run `ok` in 8.2 s, ticket `06b66b49` at **`sent`**.
  A plus-alias is not the owner's exact address, which is what makes it the right probe.

### Follow-up

The 26 tickets left at `approved` are still there. The quota has reset, so they are now sendable, but
re-approving is refused by the idempotency guard — clearing them is a deliberate human action, by
design. That is the cost of the decision above, and it is being paid rather than engineered around.

**A backlog of 26 is also the argument for the thing that does not exist**: nothing alerts on it. The
record answers "what happened"; no one is told to go and look. The query is in `RUNBOOK.md` and it is
run by a human who already suspects something.

---

## 2026-08-07 · The approval endpoint mailed whoever asked

### Symptom

`POST /webhook/brasa-approve` sent an agent's reply to the email address **in the request body**.

Any person on the internet who knew the URL could have Brasa's verified sending domain deliver
arbitrary text to an arbitrary recipient, with Brasa's branding on it. The endpoint is
unauthenticated.

This was not reported by anyone. It was found while enumerating the public surface, by reading what
the endpoint actually did with its input rather than what it was for.

### Diagnosis

`Gate Approval` built the `to` field from `$json.customer_email` — a field supplied by the caller. The
ticket being approved has a customer, and that customer has an address, and the workflow never looked
at it.

The endpoint had two other properties that made it worse and were found separately:

- It **sent before checking the ticket existed**, so a reply could be — and was — emailed for a ticket
  id belonging to no ticket.
- It had no idempotency guard, so one approval could be replayed. One reply produced **five emails**.

### What was ruled out

- **Row Level Security** — irrelevant here. n8n holds the `service_role` key and bypasses every
  policy by design; no policy could have stopped this.
- **The published anon key** — not involved. The relay needs no Supabase access at all.
- **The Netlify agent dashboard** — it sends the correct address. The hole is in the endpoint, not in
  the only client that was expected to call it. That distinction is the whole finding: the dashboard
  was not the threat model.

### Decision and trade-off

`Claim Ticket` now embeds the customer, and the recipient is read from **the claimed ticket's own
customer row**. The request's `customer_email` is never read.

**What was given up: the caller can no longer redirect a reply, including legitimately.** If a
customer writes in from one address and asks for the answer at another, the system cannot do it. That
is a real capability, removed. It was removed because there is no way to distinguish that request from
an attacker's while the endpoint is unauthenticated, and the unauthenticated endpoint is the thing
that is not being fixed today.

**A second limit was accepted rather than solved.** The guard refuses an approval when the ticket is
not in `draft` or `escalated` — but it cannot tell the caller *which* of "already answered" and "no
such ticket" applies. Both are cases where nothing may be emailed, which is what the guard is for, so
the ambiguity is acceptable; it is recorded because a caller debugging a 200-with-`not_claimed` cannot
tell those apart either.

### Resolution and confirmation

Confirmed by running it, not by reading the change:

```
execution 387  18:12:23  before  BUILT to = mcruzcardoza+relay-probe@gmail.com
                                 for a ticket whose customer is a different address
execution 389  18:13:45  after   identical request  →  BUILT to = the ticket's real customer
```

The evidence is the recipient the workflow **built**, read out of the saved execution data — not a
delivered message. Both runs then failed at `Send Approved Email` on the exhausted quota, which is
irrelevant to what is being shown here and is stated so nobody reads a green delivery into it.

The idempotency half was confirmed separately: three approvals of one ticket now claim once and send
once.

---

## 2026-08-08 · The model stopped answering and the intake disappeared

### Symptom

One intake in twenty was lost outright.

The caller waited **60.9 seconds** and received **HTTP 200**. No ticket was written, no email was
sent, no customer row was created. A person had written in and the system had no record of what they
said, while telling them it had succeeded.

Found while measuring latency, in a batch of twenty. Not reported — nobody would have reported it,
because the caller was told everything was fine.

### Diagnosis

`DeepSeek API` hung. The node's own 60-second timeout aborted the run, and the workflow ended there.

```
520  brasa-intake  error  DeepSeek API  60 635 ms  "aborted"
     ran: Tally Webhook · Guard Intake · Intake Valid? · Set Fields · Log Run Start ·
          Retrieve Products · Retrieve Order · Build DeepSeek Body · Has Substance? ·
          DeepSeek API (60 306 ms, error)     ← and nothing after it
```

The `200` is a separate mechanism. The webhook uses `responseMode: responseNode`, so a run that dies
before reaching its respond node leaves n8n to answer on its own — and it answers 200 with an empty
body. Earlier work had closed the *validation* causes of that shape; a provider that does not answer
reaches the same place by a different route.

The underlying reason the call hung at all was that **the completion had no ceiling**. Measured on the
same night: one classification produced **4 322 output tokens over 35 seconds** for an ordinary
question about a pan. The model returns a reasoning block that is billed as output and never reaches
the customer, so a runaway is invisible in the reply and unbounded in cost and time.

### What was ruled out

- **Retrieval** — both Supabase calls completed normally, in ~60 ms each.
- **The API key** — the other nineteen requests in the same batch succeeded with it.
- **Rate limiting at DeepSeek** — the requests were sequential; nothing was in flight beside it.
- **n8n resource exhaustion** — the instance served the next request normally.

### Decision and trade-off

The workflow already owned the answer and was not wired to it. `Canned Classification` exists so a
junk message can skip the model; a sibling node, `Model Unavailable`, now sits on `DeepSeek API`'s
error output and emits the same envelope. A model that does not answer produces a **written, escalated
ticket** instead of nothing.

**What was given up:** the customer gets a slower human reply instead of an instant automated one, and
the review queue absorbs the failure. That is the intended trade — the alternative was silence — but
it means a provider outage converts directly into agent workload rather than into an alert.

**The timeout dropped 60 s → 25 s.** The slowest classification ever observed here is ~18 s, so the
other 42 seconds were a browser held open for an answer that was not coming.

**A 2 000-token cap was added, and it costs something measurable.** A truncated reply is not valid
JSON, so it fails the parse and escalates rather than sending — the cap can cost a draft, never send a
truncated one. **About 1 intake in 20 now hits it and goes to a human.** Those are the runaways; before
the cap the same runs took 35+ seconds or vanished entirely. A cap of 1 500 was tried first and
rejected on evidence: it truncated a legitimate reply in the eval set (17/19 against 19/19 at 2 000).

### Resolution and confirmation

Confirmed by an accident rather than by a staged probe, which is the more useful evidence.

About twenty minutes after the fallback was wired, a bad edit sent `max_tokens` to DeepSeek as the
string `"2000"` instead of a number. The provider refused every request. From 00:26:32 until the
correction went live at 00:27:47 the model was, from this system's point of view, completely down —
and six live intakes went through `Model Unavailable`:

```
548–553   00:26:32 → 00:26:39
          ok · complete · 792–1 523 ms · model=skipped · 0 tokens
          escalate_reasons: model_flag,model_unavailable,low_confidence,empty_draft
```

Six tickets written, six customers queued for a human, six callers answered `200` with a real body,
zero auto-sends, zero losses.

The cap was confirmed separately in production: execution `571`, 2 000 completion tokens, truncated,
`parse_failed`, escalated, nothing sent.

### What this changed about how the system is read

`model = 'skipped'` used to mean one thing: the message was junk and not worth a classification. It
now means two, so the record has to say which. `escalate_reasons` carries `model_unavailable` when the
model failed and `no_substance` when the message was empty. Without that distinction, a provider
outage and a batch of junk look identical in the run log.

---

## 2026-08-25 · The agent panel rendered half of every ticket

### Symptom

Reported as "the agent can't do anything here — no buttons, nothing to write a reply in, nowhere to
see the customer's orders."

Opening any ticket rendered the header, the escalation banner and **Customer info**, and then
stopped. The customer's message, the confidence block, the reply editor, the internal notes and the
entire action bar were absent. The queue, the filters, the counters and the search all worked, so
the panel looked alive.

No console error. No failed request. `node --check` on the extracted script passed.

### Diagnosis

`renderDetail` builds the whole detail pane as one template literal. Inside it sat an HTML comment
added the same day:

```html
<!-- Sin `|| 1`: la base mantiene este contador con un trigger desde 2026-08-07 … -->
```

The backticks around `|| 1` closed the template literal at **1 156 characters**, at exactly the point
where the rendering stopped. Everything after that was never part of the string.

What made it survive every check is that the result is still valid JavaScript. The parse reads as

```js
main.innerHTML = `…<!-- Sin ` || 1 `: la base mantiene …`
```

— a template literal, `||`, a number, and then another template literal. No syntax error, so no
exception, so nothing in the console and nothing in a syntax check. The only place the damage is
observable is the DOM.

Introduced by `8ae117e` the same day, in the commit that removed a `|| 1` fallback from the ticket
counter and documented the removal in a comment.

### What was ruled out

- **A stale deploy** — the deployed HTML matched the commit. This was checked first and it is what
  delayed the diagnosis: *deployed equals committed* says nothing about whether the page works.
- **Data and RLS** — tickets, customers and notes all loaded; the list rendered from the same array.
- **Auth** — the session was valid; `bc_agent` resolved and the top bar showed the agent's name.
- **The escalated-ticket lock** — suspected because `8ae117e` had touched it. The lock was correct;
  the buttons it controls were simply never emitted.

### Decision and trade-off

The comment was kept and rewritten with straight quotes, plus an explicit warning in place that a
backtick there truncates the pane. Deleting it was the easier fix and was rejected: the comment
records why the counter has no fallback, which is the kind of thing that gets "helpfully" restored.

**What was given up:** no automated guard was added. This repository has no test runner for the four
front ends — the build is a `sed` — and inventing one to catch a single class of typo was not worth
the surface. The check that found it is a throwaway harness, not committed: the page with the
Supabase SDK and `sb()` replaced by fixtures, served locally and driven in a browser.

**The real lesson is about verification, not about backticks.** A front end whose failure mode is
*renders less HTML than intended* cannot be verified by diffing files or by checking syntax. It has
to be rendered.

### Resolution and confirmation

After the fix, all four ticket states render complete: **6 974 – 7 892 characters** each, against
1 337 before, every one carrying the orders block, the reply editor, the notes block and all three
action buttons.

The other five front ends in this repository were scanned for the same construction — a backtick
inside an HTML comment — and none carry it.

**Limit.** The panel was in this state from `8ae117e` until the fix on 2026-08-25. How much agent
work was lost to it is not recoverable: nothing records a detail pane that failed to draw.

---

*Last verified 2026-08-25.*
