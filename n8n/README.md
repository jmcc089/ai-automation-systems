# n8n

Build source for the `n8n` Railway service — the automation backbone all three workflows run on (`brasa-intake`/`brasa-approve`, `hvl-intake`, `cedar-intake`).

## The intake secret

Each Tally-driven webhook checks a shared secret before anything is written or paid for: a hidden
field on the form, compared against a Railway variable. Tally holds the value in its own
configuration and posts server-to-server, so it never reaches a browser.

| build | variable | webhook | Tally field label |
|---|---|---|---|
| Holt & Vargas | `HVL_INTAKE_SECRET` | `hvl-intake` | `Intake secret` |
| Cedar Healthcare | `CH_INTAKE_SECRET` | `cedar-intake` | `Intake secret` |
| Brasa Commerce | `BC_INTAKE_SECRET` | `brasa-intake` | `Intake secret` |

**One per build, never shared.** A leak from one form opens one build, and rotating one does not
force the other two down.

**Each is inert until its variable is set**, and says so on every run — shipping a closed gate
before the Tally field exists would refuse every real intake. An unset secret is not protection.

**Brasa's other three webhooks cannot have one.** `brasa-checkout` and `brasa-approve` are called
by browser JavaScript, and so is `brasa-message` — the storefront's "message about my order" flow,
which until 2026-08-11 shared `brasa-intake` with Tally and imitated its payload shape deliberately.
A secret those pages had to send would be readable by anyone who opens them. They were split so the
Tally path could be closed at all; the public path stays open and its real control is rate limiting,
which needs a proxy or a counter in Postgres and is not a config toggle.

Not a frontend, not one of the three builds — this folder exists only so the service doesn't depend on a third-party GitHub repo. The Dockerfile just pulls the official `n8nio/n8n` image and sets a few defaults; there is no application code here.

Credentials and execution history live in the attached Postgres and `n8n-volume`, not in this repo.
**Workflow definitions do live here**, in [`workflows/`](workflows/), since 2026-08-11 — exported
from the running instance and diffable against it. They went in because one of them silently
reverted in production and nothing noticed for hours; see `holt_vargas/INCIDENTS.md`.
**Checking them is a manual step, not an automated one.** `workflows/sync.py` used to pull, push
and diff; it was removed on 2026-08-24 when the exports were replaced with n8n's own filenames, so
nothing now compares these files to production unless someone asks it to. Last compared 2026-08-25:
all three identical, node for node and parameter for parameter. The exports contain expressions and node structure, never secret values. `N8N_ENCRYPTION_KEY` is a Railway service variable (Variables tab), independent of this source — that's what keeps credentials decryptable across a source change.

Version is pinned in the Dockerfile (`FROM n8nio/n8n:2.19.2`). Bumping it is a deliberate, separate decision — not something that happens by touching this repo.
