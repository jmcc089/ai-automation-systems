# Production AI Automation

Three AI intake-automation builds sharing one method: read an unstructured message from a
member of the public, classify it, route it by urgency and service, and refuse to write anything
that fails a contract.

| Build | Domain | What is distinct about it |
|---|---|---|
| `cedar_healthcare/` | Clinic intake triage | Classify and route only — the system never gives clinical advice. Acute cases short-circuit to an urgent alert. |
| `holt_vargas/` | Law firm intake | Grounded on an in-prompt checklist rather than retrieval. |
| `brasa_commerce/` | E-commerce support | True RAG over a product and order corpus. |
| `n8n/` | Shared automation runtime | Build source for the Railway service, and `workflows/` — all three workflow definitions under version control, diffed against the live instance by hand. |

Each build is a static dashboard plus an n8n workflow. All three share one Supabase project and one
n8n instance, isolated by table prefix (`ch_`, `hvl_`, `bc_`) and by webhook path.

**The full case study, architecture decisions and screenshots live in Notion.** This repository is
the code and the operational documentation; it is not the write-up.

---

## What you need before anything runs

Nothing here works standalone — every build depends on the six external services below. There is no
docker-compose that brings the world up locally, and pretending otherwise would waste your first
hour.

| Service | Used for | Free tier sufficient |
|---|---|---|
| Supabase | Postgres, auth, RLS | yes |
| | *front ends use the **publishable** key `sb_publishable_…`, not the legacy `anon` JWT — disabling the legacy `service_role` disables `anon` with it, and that took all four dashboards down on 2026-08-11* | |
| n8n (self-hosted on Railway) | the workflows | Railway is paid; n8n itself is free |
| Deepseek | classification | pay per call, cents |
| Resend | transactional email | yes, but sending to arbitrary addresses needs a verified domain |
| Netlify | the dashboards | yes |
| Tally | the public intake form | yes |

---

## Running a dashboard locally

The dashboards are single-file HTML with no framework and no bundler. The "build" is two string
substitutions. There are **four** front ends: `cedar_healthcare/app`, `holt_vargas/app`,
`brasa_commerce/app` (the agent dashboard) and `brasa_commerce/web` (the public storefront).

```bash
cd brasa_commerce/app                      # or web, or either of the other builds
export SUPABASE_URL="https://<your-project>.supabase.co"
export SUPABASE_KEY="<your anon key>"     # anon, never service_role
npm run build                              # rewrites index.html in place
python3 -m http.server 8000                # then open http://localhost:8000
```

`npm run build` **modifies `index.html` in place**, replacing `__SUPABASE_URL__` and
`__SUPABASE_KEY__`. Do not commit the result — `git checkout index.html` afterwards, **unless you
have uncommitted edits in that file**, in which case build a copy instead. This is deliberate: the
repository holds placeholders so no key is ever committed, and Netlify substitutes them at deploy
time from its own environment variables.

> The build script used to be `sed -i "s|…|…|g" index.html`, which is GNU syntax. On macOS BSD sed
> reads the argument after `-i` as a backup suffix, so it tried to execute `index.html` as the
> script, failed, and left both placeholders in place. It worked on Netlify and could never have
> worked locally. All four front ends now use a portable `sed -e … -e … > file && mv` instead.

The agent dashboards show a login screen. Access requires a Supabase Auth user whose id appears in
the build's own staff table — `ch_practitioners.auth_user_id` for Cedar, `bc_agent.auth_user_id` for
Brasa. There is no local bypass, because the row-level security policies are the access control and
mocking them would test something other than the system. **`brasa_commerce/web` needs no login**: it
is the public storefront and reads only the active product catalog, which is what its anon key is
allowed to see.

## Bringing one up from zero

Ordered, and each step is needed by the next.

1. **Supabase project.** Run the migrations in order. They are listed under *Database → Migrations*
   in an existing project; for Cedar the sequence starts at
   `move_practitioner_from_patients_to_intake_requests` and ends at
   `ch_drop_duplicate_assignment_trigger`.
2. **Seed the reference tables.** `ch_services` must contain at least one row with
   `assignable = false` — the classifier degrades to it and a foreign key depends on it. Then
   `ch_practitioners`, whose `role` must match a `ch_services.name` exactly, because that equality
   *is* the routing rule.
3. **Auth users.** Create one Supabase Auth user per practitioner and write its uuid into
   `ch_practitioners.auth_user_id`. Until you do, that person signs in successfully and sees an
   empty dashboard.
4. **n8n on Railway.** Deploy `n8n/` as a Dockerfile service with a volume and an attached Postgres.
   Set `N8N_ENCRYPTION_KEY` before creating any credential.
5. **Workflow env vars**, as Railway service variables — the workflow reads them as `{{ $env.NAME }}`:
   `SUPABASE_SERVICE_KEY`, `DEEPSEEK_API_KEY`, `RESEND_API_KEY`.
6. **Import and publish the workflow.** Publishing is separate from saving; the running system uses
   the published version.
7. **Resend domain.** Verify a sending domain and press *Verify DNS Records* — publishing the DNS is
   not enough, and this has caused a real outage (see `cedar_healthcare/INCIDENTS.md`).
8. **Tally form.** Point its webhook at `/webhook/cedar-intake`. The four question labels must read
   exactly `Full name`, `Email address`, `Phone number`, `Tell us about your reason for visit` —
   the workflow matches on label text. **Brasa's fourth label is `Message`, not that one**, and its
   webhook is `/webhook/brasa-intake`.
9. **Netlify.** One project per front-end directory, with `SUPABASE_URL` and `SUPABASE_KEY` set.
   Brasa needs **two**: `brasa_commerce/app` and `brasa_commerce/web`.

Verify with the health check in `cedar_healthcare/RUNBOOK.md`, or
[`brasa_commerce/RUNBOOK.md`](brasa_commerce/RUNBOOK.md) — Brasa's costs no email, so prefer it if
the Resend daily quota matters to you that day.

**Brasa differs from the sequence above in three places**, and each one will stop you if you follow
Cedar's steps literally:

- **Three webhooks, not one**: `brasa-intake` (the Tally form), `brasa-checkout` (the storefront) and
  `brasa-approve` (the agent dashboard). All three live in one workflow.
- **Step 2 has no equivalent.** Brasa's reference table is `bc_product_catalog`, and the classifier
  does not degrade to a fallback row — retrieval simply returns nothing and the ticket escalates.
  Seed `bc_agent` instead, then write its `auth_user_id`.
- **Two Postgres functions must exist before the workflow runs**: `search_products` and
  `search_order`. Both are `SECURITY DEFINER`, and `EXECUTE` must be revoked from `PUBLIC` and
  granted to `service_role` only — Postgres grants `EXECUTE` to `PUBLIC` by default, and leaving it
  there published real order data to anyone holding the site's public key. See
  [`brasa_commerce/SECURITY.md`](brasa_commerce/SECURITY.md).

**Holt & Vargas differs from the sequence in two places**, both of which will leave you with a
system that looks correct and is not:

- **All three workflows are in version control**, at [`n8n/workflows/`](n8n/workflows/), as exports
  from the live instance. Holt's went in first because Holt's reverted silently in production and
  nothing noticed for hours (`holt_vargas/INCIDENTS.md`); Cedar's and Brasa's followed because they
  run on the same n8n and were exposed to the same accident with nothing watching.
  **The drift check is manual.** A `sync.py` that pulled, pushed and diffed against the live
  instance was removed on 2026-08-24 along with the older exports; comparing an export against
  production is now something a person does deliberately. Last done 2026-08-25: all three identical
  node for node and parameter for parameter.
- **The failure recorder is on the same canvas**, as it is for Cedar — `Error Trigger` →
  `Classify Failure` → `Record Failure`, with `settings.errorWorkflow` pointing at the workflow
  **itself**, which n8n permits. Until 2026-08-10 it was a separate workflow
  (`ZcVRMF8TacvQ5rQg`, now deactivated), and that split is what once let it sit inactive while
  recording nothing and reporting no problem. Whichever shape it takes, `errorWorkflow` must be
  set: unset means failures are recorded nowhere, silently.
- **Step 2's equivalent is `hvl_attorneys`, and its `specializations` column routes.**
  `hvl_assign_case_staff` matches `new.case_type = any(a.specializations)`, so a value that is not
  one of the firm's five case types can never match: routing silently falls through with no error
  anywhere. `hvl_attorneys_specializations_domain` now refuses those values, but seed the roles
  correctly — a case also cannot be assigned to a paralegal whose `role` is not `Paralegal`.

**Not rehearsed end to end.** These steps are reconstructed from the built system and from the
migration and deployment record, not from a clean-room rebuild. Steps 1–3 and 5 have been verified
against what the code actually reads; the ordering of 4 and 6–9 is derived rather than re-executed.
Treat gaps as likely and report them.

---

## One probe, three builds

The same two payloads — markup in the name, markup plus an attacker link in the message — have been
fired at all three production webhooks and the **delivered** email read back out of Resend. It is
the only comparison in this repository where the three are measured against an identical test.

| | staff email | client email | dashboard |
|---|---|---|---|
| `holt_vargas/` | escaped | escaped | escaped |
| `brasa_commerce/` | escaped | escaped | escaped |
| `cedar_healthcare/` | **not escaped — open** | not escaped | escaped, fixed `47dfe53` |

Cedar's dashboard was fixed after a stored-XSS finding; its emails were not, and a re-probe on
2026-08-11 delivered a live `<a href>` to an attacker domain inside a Cedar-branded email to a
clinician. Brasa passes on the path that was probed and has one unprobed email node with no
escaping, on the unauthenticated checkout path. Details in each build's `ADVERSARIAL.md`.

The probe rows are **kept in the database on purpose**, with their payloads intact. A finding whose
evidence has been tidied into a plausible-looking customer name is a sentence with nothing behind it.

## Brasa Commerce, in more detail

The most thoroughly documented build, and the only one with a public write path that takes money-shaped
input.

- [`brasa_commerce/RUNBOOK.md`](brasa_commerce/RUNBOOK.md) — health check (free: it costs no email and
  no model call), failure triage ordered by measured frequency, the run-status vocabulary, safe
  workflow editing, key rotation, restore.
- [`brasa_commerce/INCIDENTS.md`](brasa_commerce/INCIDENTS.md) — three write-ups, including a build
  that never delivered a single customer email while every record said it had, and an unauthenticated
  endpoint that mailed whoever asked.
- [`brasa_commerce/DECISIONS.md`](brasa_commerce/DECISIONS.md) — what this system must never do, and
  the construct that enforces each prohibition rather than the prompt that requests it.
- [`brasa_commerce/SECURITY.md`](brasa_commerce/SECURITY.md) — what is reachable without credentials,
  what refuses it, and the open gaps with the cost of closing each. Ends with a copy-pasteable script
  that falsifies its own claims.
- [`brasa_commerce/ADVERSARIAL.md`](brasa_commerce/ADVERSARIAL.md) — 31 probes across six families,
  every one fired at production, with before and after.
- [`brasa_commerce/eval/`](brasa_commerce/eval/) — a frozen 19-case classifier suite (`run.py`, sends
  nothing and writes nothing), plus `latency.py`, `load.py` and `sender_probe.py`, which all hit
  production and all cost Resend sends. Read their docstrings before running them.

## Cedar Healthcare, in more detail

- [`cedar_healthcare/RUNBOOK.md`](cedar_healthcare/RUNBOOK.md) — health check, failure triage, stop
  and start, key rotation, adding a service.
- [`cedar_healthcare/INCIDENTS.md`](cedar_healthcare/INCIDENTS.md) — two write-ups, including a
  routing failure that ran in production for two weeks and the regression its fix caused.
- [`cedar_healthcare/SECURITY.md`](cedar_healthcare/SECURITY.md) — what is reachable without
  credentials, what refuses it, and the open gaps with the cost of closing each.
- [`cedar_healthcare/ADVERSARIAL.md`](cedar_healthcare/ADVERSARIAL.md) — what the intake endpoint does
  when sent empty, malformed, oversized, non-English or hostile input. Every row was fired at
  production; the ones that behaved wrongly say so.

---

## A note on this repository's history

The first commit here contains all six applications already built: this monorepo was assembled on
2026-07-16 from six separate repositories, and the development history of each build lives in the
repository it came from. The middle of that period is also thin — a run of commits reading
`Update index.html` and `Update package.json`, made through GitHub's web editor during a hosting
migration, which record that something changed but not what or why.

Commits from 2026-08-01 onward are what the rest of this repository should be judged against: they
name a change and its reason. History cannot be back-dated, so this is recorded rather than
disguised.
