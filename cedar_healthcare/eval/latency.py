#!/usr/bin/env python3
"""
Cedar Healthcare — what a request costs in time, measured against production.

    python3 latency.py                  # 20 sequential intakes
    python3 latency.py --n 30           # how many intakes (default 20)
    python3 latency.py --dry-run        # print the payloads, send nothing

THIS HITS PRODUCTION AND HAS SIDE EFFECTS. Each intake spends one DeepSeek
completion and sends two Resend emails — a patient confirmation and a
practitioner alert, unconditionally, on every urgency level (DECISIONS.md
item 5: alerting is uniform, not branched). Every row it creates is
identifiable — `source_event_id` begins with `lat-cedar-` — and the
clean-up SQL is printed at the end.

Every request comes from a DIFFERENT generated patient, and that is not
cosmetic. `ch_patients` upserts on email (ADR-4), so a batch sent from one
address folds onto a single patient row holding twenty intakes — correct
behaviour and an unreadable record, the same lesson Brasa's latency.py
already paid for.

The deliverable address is a plus-alias of the owner's own mailbox —
mcruzcardoza+probe@gmail.com — per request, so each probe gets its own
patient row and nothing can reach a third party. Gmail collapses everything
after the first "+" for delivery, so every alias still lands in one inbox.

Why sequential, and why this is the number that goes on the page
------------------------------------------------------------------
Brasa's and Holt & Vargas's production p50/p95 are both real measurements —
Brasa from this same script's methodology, Holt from 38 accumulated
production runs. Cedar had neither: RUNBOOK.md only carries "4-7 seconds"
from a handful of manual health-check probes, which is an anecdote, not a
percentile, and does not belong on the page next to two measured numbers.

Requests are sent one at a time with no retry, matching Brasa's latency.py:
concurrency here would measure the harness, not the system — which is
exactly why the frozen-eval-set latency figures (run.py) were never used
for this. A non-2xx is recorded and the run continues; a probe that stops
at the first failure cannot measure a tail.
"""
import argparse, json, os, statistics, sys, time, urllib.error, urllib.request

BASE = "https://n8n-production-3503.up.railway.app/webhook/cedar-intake"
OWNER = "mcruzcardoza+probe@gmail.com"   # never a third party — see the docstring


def load_env():
    """Reads .env into os.environ. CH_INTAKE_SECRET must match the live n8n
    workflow's Has Required Fields gate (n8n/workflows/cedar-intake.json) or
    every request is rejected before classification is attempted — this bit
    the first run of this script, and RUNBOOK.md's own probe example is
    stale for the same reason: it predates the secret gate."""
    path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    if not os.environ.get("CH_INTAKE_SECRET"):
        sys.exit("Missing CH_INTAKE_SECRET in cedar_healthcare/eval/.env")

# Realistic intakes spanning the three service lines plus Unknown, varying
# urgency and length so prompt tokens (and therefore completion latency)
# vary the way real traffic does — a single repeated message would measure
# DeepSeek's cache rather than this system.
MESSAGES = [
    "My lower back has hurt for three weeks and it's getting worse when I sit at my desk.",
    "I twisted my ankle running this morning, it's swelling and I can barely put weight on it.",
    "Looking for a sports massage before a half marathon in two weeks, just general tightness.",
    "I've had shoulder pain on and off for months, worse after lifting, nothing acute right now.",
    "Wondering if you do nutrition consults for someone recently diagnosed with type 2 diabetes.",
    "My knee gave out on the stairs an hour ago and now it's swollen and I can't straighten it.",
    "Just moved to Austin and want to establish care for ongoing lower back issues from an old injury.",
    "Do you have anything available this week for a deep tissue massage, no specific injury.",
    "My physical therapist retired and I need someone to continue post-surgical knee rehab.",
    "Chest tightness after exercise the last two times, not sure if that's something you see here.",
    "Interested in a nutrition plan for weight loss, nothing urgent, just exploring options.",
    "Numbness down my left arm since this morning along with some neck stiffness.",
    "My daughter twisted her wrist at gymnastics practice, should we come in today or can it wait.",
    "Been dealing with plantar fasciitis for two months, tried rest and it's not improving.",
    "What's your pricing for a first-time consultation, not sure yet which service I need.",
    "Sharp pain in my lower back that started suddenly while gardening, hard to stand up straight.",
    "Following up on nutrition guidance from a friend's referral, general healthy-eating questions.",
    "My hamstring has felt tight for a week after a hard workout, no sharp pain, just tight.",
    "Post-op from a rotator cuff surgery six weeks ago, my surgeon said to start physio now.",
    "Just some general questions about what conditions you treat before I book anything.",
]

FIRST = ["Marisol", "Tobias", "Neela", "Aurelio", "Fenna", "Cassian", "Ilse",
         "Rafael", "Odette", "Nikolai", "Simone", "Emeka", "Yara", "Lucian",
         "Priya", "Mateo", "Anke", "Solomon", "Junko", "Diego"]
LAST = ["Vasquez", "Brandt", "Okonjo", "Ferreira", "Halloran", "Nakamura",
        "Delgado", "Rousseau", "Sandoval", "Whitlock", "Amankwah", "Bergstrom"]


def person(i):
    """A distinct patient per request. See the docstring on why this matters."""
    name = f"{FIRST[i % len(FIRST)]} {LAST[(i * 5 + i // len(FIRST)) % len(LAST)]}"
    handle = name.lower().replace(" ", ".")
    local, domain = OWNER.split("@")
    return name, f"{local}.{handle}@{domain}"


def intake_body(tag, message, who):
    name, email = who
    return {"eventId": tag, "data": {"submissionId": tag, "fields": [
        {"label": "Full name", "value": name},
        {"label": "Email address", "value": email},
        {"label": "Phone number", "value": "+1 512 555 0147"},
        {"label": "Tell us about your reason for visit", "value": message},
        {"label": "Intake secret", "value": os.environ["CH_INTAKE_SECRET"]}]}}


def fire(body, timeout=90):
    """One request, one wall-clock reading. A failure is a data point, not a stop."""
    req = urllib.request.Request(BASE, method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return {"ms": int((time.time() - t0) * 1000), "code": r.status,
                    "body": r.read(400).decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        return {"ms": int((time.time() - t0) * 1000), "code": e.code,
                "body": e.read(400).decode("utf-8", "replace")}
    except Exception as e:
        return {"ms": int((time.time() - t0) * 1000), "code": 0, "body": repr(e)}


def pct(xs, p):
    return sorted(xs)[min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))]


def report(rows):
    if not rows:
        return
    ms = [r["ms"] for r in rows]
    codes = {}
    for r in rows:
        codes[r["code"]] = codes.get(r["code"], 0) + 1
    print(f"intake     n={len(ms):<3} min {min(ms):>6}  p50 {pct(ms,50):>6}  "
          f"p90 {pct(ms,90):>6}  p95 {pct(ms,95):>6}  max {max(ms):>6}   "
          f"codes {dict(sorted(codes.items()))}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    load_env()
    run = int(time.time())
    plan = [(f"lat-cedar-{run}-i{i}", MESSAGES[i % len(MESSAGES)], person(i))
            for i in range(a.n)]

    if a.dry_run:
        for tag, message, who in plan:
            print(tag, json.dumps(intake_body(tag, message, who))[:160])
        return 0

    print(f"{len(plan)} sequential requests against production · run tag lat-cedar-{run}\n")
    results = []
    for n, (tag, message, who) in enumerate(plan, 1):
        r = fire(intake_body(tag, message, who))
        results.append(r)
        print(f"  {n:>3}/{len(plan)}  {r['code']}  {r['ms']:>6} ms")

    print()
    report(results)

    print(f"""
Compare against the server's own measurement — two clocks, one interval:

  select stage, status, count(*), round(avg(duration_ms)) avg_ms,
         percentile_disc(0.5) within group (order by duration_ms) p50,
         percentile_disc(0.95) within group (order by duration_ms) p95,
         round(avg(total_tokens)) avg_tokens
  from ch_workflow_runs where source_event_id like 'lat-cedar-{run}-%' group by 1,2;

Clean-up, when the evidence is no longer needed:

  delete from ch_intake_requests where source_event_id like 'lat-cedar-{run}-%';
  delete from ch_workflow_runs   where source_event_id like 'lat-cedar-{run}-%';
  delete from ch_patients where email like 'mcruzcardoza.%@gmail.com';
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
