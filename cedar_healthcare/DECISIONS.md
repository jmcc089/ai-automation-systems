# Cedar Healthcare — what this system must never do

The prohibitions this build is held to, and the construct that enforces each one. Written 2026-08-08,
after Brasa's assurance pass showed what the absence of this file costs: a rule that lives only in a
prompt is an instruction, not an enforcement, and nothing reports when it stops applying.

This file is the statement. The code and the database are the enforcement. Where the two disagree,
one of them is a bug — that is the point of writing it down.

Every claim below was checked against the running system on 2026-08-08 — by reading `pg_constraint`,
by reading the node that builds each email, or by driving the endpoint — not against the write-up.

---

## 1 · No sentence written by the model is ever sent to a patient

This is the strongest guarantee in the build, it is structural rather than instructed, and it was
undocumented until today.

`Build Confirmation Email` chooses between **three fixed paragraphs** keyed on `urgency_level`:

```js
const messages = { 3: {headline: 'Your case has been flagged as urgent', …},
                   2: {headline: 'Your case has been prioritized',      …},
                   1: {headline: 'Your request is confirmed',           …} };
const m = messages[urgency] || messages[1];
```

**`recommended_action` is never referenced in that node. Neither is `raw_message`.** The model's
entire influence over what a patient reads is *which of three pre-written paragraphs is selected* —
and if the urgency is anything unexpected, `|| messages[1]` picks one anyway.

The consequence worth stating: a totally wrong classification produces a *correct* email with the
wrong tone. It cannot produce clinical advice, a diagnosis, a treatment suggestion, or a hallucinated
appointment time, because there is no path for model text to reach the patient at all.

**This is the inverse of Brasa**, deliberately. Brasa auto-sends model prose to customers and carves
out nine exceptions. Cedar sends none, ever. The domain is the reason: a wrong sentence about a
skillet costs a refund, and a wrong sentence about someone's back costs more than this system is
allowed to risk.

**Limit.** The *practitioner* email is different: it carries the patient's raw message, because a
clinician needs to read what the patient actually wrote. That text is interpolated into HTML without
escaping — accepted, with rationale, at the assurance pass's item 09, and still open.

## 2 · Urgency is a closed set, and an unknown urgency becomes the most urgent

`urgency_level ∈ {1, 2, 3}`, enforced in two places in the database, not one:

```sql
ch_intake_requests.intake_requests_urgency_level_check  CHECK (urgency_level = ANY (ARRAY[1,2,3]))
ch_workflow_runs.ch_workflow_runs_urgency_level_check   CHECK (urgency_level = ANY (ARRAY[1,2,3]))
```

A CHECK is not subject to RLS bypass, so it holds on every write path — the workflow's service role,
direct admin SQL, and a practitioner's authenticated `UPDATE` alike.

**Since 2026-08-08, a value outside that set is repaired rather than rejected, and repaired upwards.**
`Parse Deepseek Response` used to `throw`, which ended the run and lost the intake. It now sets
urgency 3, `needs_review = true`, and records why. Cedar alerts on every level (ADR-2), so
over-banding changes *how urgently someone is told*, never whether — and for a clinic that is the
cheaper direction to be wrong in.

## 3 · A stored service category is always a real service

Two foreign keys, not a validation:

```sql
ch_intake_requests.service_category → ch_services(name)
ch_workflow_runs.service_category   → ch_services(name)
```

`Parse Deepseek Response` maps any value the model invents onto the non-assignable fallback category
and forces `confidence_flag = true`, so an unrecognised category is both storable and visibly flagged.
`complexity` and `status` are closed sets too, by CHECK: `high|medium|low` and
`New|In Progress|Resolved`.

## 4 · Routing is an equality between two constrained columns

`ch_practitioners.role → ch_services(name)` is a foreign key. The routing rule is that a
practitioner's `role` equals an intake's `service_category`, and **both sides are constrained to the
same table**, so a typo cannot silently create a practitioner nobody routes to.

**Limit, and it has bitten.** Matching is on role alone. When two practitioners share a role the
choice is arbitrary, and when none matches, `practitioner_id` is null and the case is unassigned —
which was invisible to everyone until ADR-5 widened the policy. See `INCIDENTS.md`.

## 5 · Every intake produces a practitioner alert

Alerting is uniform; only the banding differs. There is no conditional branch on urgency — all three
levels take the identical path and differ in subject line, badge colour and copy.

This is the deliberate choice, not a fallback. Branching only on acute cases would create a second
class of intake that generates no notification at all, and the classifier's own instability is
measured at 3–6 unstable cases out of 20 across runs.

**Limit.** The alert carries no delivery guarantee: neither Resend node sets `retryOnFail` or
`onError`, so a send failure ends the run after the clinical row is committed. Accepted at ADR-6.
`ch_workflow_runs` now records which node failed, which closes the diagnosis gap, not the delivery
gap.

## 6 · The same submission never produces two intakes, and never two emails

```sql
ch_intake_source_event_id_uniq  UNIQUE (source_event_id)  -- on ch_intake_requests
```

The database refuses the repeat — not the interface, and not a time window. The insert alone is not
the guarantee, because the expensive effect is the email: both `Build Confirmation Email` and
`Build Practitioner Email` open with `if (!_row || !_row.id) return [];`, so a deduplicated insert
reaches them as an empty item and neither sends.

**Limit.** A caller that omits `submissionId` gets no protection — inherent to a client-supplied key.
A repeat returns `200` and does nothing, which is indistinguishable from a silent failure; the
runbook says so.

## 7 · A model that answers badly cannot delete an intake

Added 2026-08-08, carried from Brasa's assurance item 11.

Until then every check on the model's output was a `throw`, and a throw ends the run: no
`ch_intake_requests` row, no alert, `HTTP 500` to Tally, and the patient's message gone. Four distinct
model behaviours could do it — no content, non-JSON text, a missing field, an out-of-range urgency —
and a fifth, the model not answering at all, aborted the run at the node's own 60-second timeout.

Now:

- `Deepseek API` has `onError: continueErrorOutput` wired to `Model Unavailable`, which emits the
  envelope `Parse` expects. Every node downstream reads `$('Parse Deepseek Response')` **by name**,
  which is why the fallback flows through Parse rather than around it.
- `Parse Deepseek Response` degrades on any unusable output: `Unknown` category, urgency 3,
  `needs_review = true`, and the reason recorded in `ch_workflow_runs.error_message`.
- The completion is capped at **3 000 tokens**. Cedar spends about 480 of its ~550 output tokens on
  reasoning the patient never sees, and one production run had already reached 2 796 unbounded.

**The line this draws: provider output degrades, system misconfiguration still throws.** An empty
`ch_services`, or one with no non-assignable fallback row, still raises — writing a guess around a
misconfigured clinic is not a kindness.

**Verified in production, not argued.** Execution `621`: the completion hit the cap, the model
returned no usable content, and the intake was still written — `Unknown / urgency 3 / needs_review
true`, `error_message: "the model returned no content"`. The same message re-driven at the 3 000 cap
(execution `622`) classified normally in 1 356 tokens. Before this change the first of those two would
have been an HTTP 500 with no record that anyone had written in.

**Cost, stated.** A cap that bites turns an auto-triaged intake into one a human must read. It was set
by measurement: 2 000 was tried first and truncated a legitimate ambiguous case (19/20 on the eval
set); 3 000 passed 20/20 on 2026-08-08 and still bounds the tail.

**Re-run 2026-08-12 at the 3 000 cap: 19/20, with 2 of 20 unstable across three repetitions.** The
failing case is `injection-exfiltrate-prompt`, and it is not a truncation: the service and urgency
were classified correctly and `recommended_action` came back carrying text from the system prompt
itself. The cap decision above is unaffected — the leak is a separate, open finding — but the
`20/20` is no longer the current number and is left here dated rather than quietly refreshed.

---

## What is not prohibited here, deliberately

- **All three practitioners can read any untriaged clinical message.** ADR-5 widened `SELECT` to
  include unassigned cases because Cedar has no receptionist and no triage role. A clinic with a front
  desk should hold this visibility in a triage role instead.
- **Practitioner alerts go to one hardcoded inbox**, not to the routed practitioner. ADR-4. The cost
  is that routing correctness is not observable through the notification, which is why a routing
  defect survived in production for around two weeks.
- **A notification that fails is not retried.** ADR-6.
- **Patient text is interpolated unescaped into the practitioner email.** Accepted at item 09 with a
  rationale; the recipient is a single known inbox.

*Last verified 2026-08-08.*
