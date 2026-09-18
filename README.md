# Pukaar

**One press. The three people most likely to answer are paged in the same instant; the first to say "I'm going" wins; everyone else is told who is coming; if nobody answers in a minute the circle widens.** An older person living alone should not have to work through a phone list while she is on the floor.

- **The one design decision:** the escalation is a Step Functions state machine, not a loop in a server. A parallel `Map` pages a whole circle at once, a conditional DynamoDB write decides the race between answerers, and the machine *waits for a task token* — a claim or a cancel wakes it in about a second instead of at the next timer.
- **What an incident costs:** about **$0.0014 (₹0.12)** when the son answers from the first circle, **$0.0039 (₹0.35)** when nobody answers and it widens to everyone. Idle is a fraction of a cent per person per month plus one $1/month key; a thousand people with one incident each come to about $6/month, $10 with the weekly check-in. Numbers from [`cost.py`](cost.py), list prices, counted off real executions.
- **Live:** https://jseoe3z3uew46fyd6zbceyry6u0ebdgt.lambda-url.ap-south-1.on.aws/ — pressing it pages six test mailboxes and one Telegram, all mine.
- **Demo video:** *added at submission.*
- **Proof it works:** [`verify.sh`](verify.sh) runs eighteen checks against the live stack, each asserting on rows and execution history, never on a status code. Last run 18/18. The unit tests and `terraform validate` run on every push: [![ci](https://github.com/aryangorde6/pukaar/actions/workflows/ci.yml/badge.svg)](https://github.com/aryangorde6/pukaar/actions/workflows/ci.yml). Every break during the build is in [`LEARNING-LOG.md`](LEARNING-LOG.md) with the commit that fixed it.

Built solo, in the open, during Bharat Builds Tour — First Commit, 17–20 September 2026, ap-south-1.

---

## The problem

An older person living alone falls, or feels something is wrong, and has a phone. What she does with it is call one number. If it does not answer she calls the next, and each try costs a minute she is not sure she has. The son is in a meeting; the neighbour across the hall is home but was never called; nobody knows that nobody is coming.

Ten conversations, not a study: neighbours in one building, in their late sixties to eighties, most of whom said they had the same problem. One woman said that the last time she needed someone urgently she called at least three or four people before one answered — she does not remember the exact number. Her son first, then relatives nearby; a nephew about two kilometres away came, in fifteen to twenty minutes, which she called lucky. Her nearest neighbours were out at work on weekdays; once, she called the building's watchman, and he helped. None of this is measured; it is what she said, and she agreed to be mentioned without her name.

Pukaar replaces the sequence with a fan-out. One press pages the three people most likely to answer *right now*, in parallel. The first to say "I'm going" wins, everyone else is told who is coming, and if nobody answers within a minute the circle widens. She can call it off herself with one more press, even after someone is on their way.

## What happens when she presses

1. **Her screen** (`GET /`) is one button. It already says who will be told — *"Vaishali, Ravi and Anil will be told straight away"* — read from the same ranking the machine will use. It installs on her home screen as *Pukaar* (`/manifest.webmanifest`, two PNG icons served by the same function), so the button is an icon, not an address to type. It speaks her language: `?lang=mr` once, at setup, and every word on her screen is Marathi — *मला मदत हवी आहे*; the phone remembers, and the one pill on the page switches between English and her language, never a menu. Ten languages besides English: Marathi and Hindi were read by someone who speaks them; Gujarati, Tamil, Telugu, Kannada, Bengali, Malayalam, Punjabi and Odia are drafts checked by machine translation only, waiting for a reader — which is one reason English stays one tap away on every screen. A language is twenty-four strings in [`web.py`](lambdas/web.py). Only her page is translated; the people paged are younger, and their pages and emails stay English.
2. **The press** (`POST /trigger`) starts one execution of `pukaar-escalation`, named after the incident, so a retried request cannot start the same incident twice; the button disables itself on press. A second press while an alert is running — a double tap, a reload, the app reopened — joins that alert instead of starting another, and her page reopened during one shows it.
3. **The circle is paged at once.** `SelectTier` ranks everyone on her list and takes the top three not yet reached; a parallel `Map` sends each of them an email with a one-time link — and the same link on Telegram, as a button, to anyone who has started her bot (the son, in the demo). In the execution history the three sends carry the same timestamp (`13:22:59.690` on the first live run).
4. **The machine waits for a task token**, parked on the incident row. If nobody answers in `wait_s` seconds it wakes by timeout, checks the row, and widens to the next three. At the last circle everyone is paged, then a final *no one has reached her* email goes to the whole list.
5. **Someone taps "I'm going now."** One conditional `UpdateItem` — `status IN (OPEN, FALLBACK)` — decides the race; the loser's page says *"Ravi is already on the way"* by name, read from the row after the write. The winning write returns the parked token, `SendTaskSuccess` wakes the machine, and everyone reached is told who is coming. Measured: cancel → *Cancelled* in **1.0 s**, claim → everyone told in **2.8 s** including the emails.
6. **The winner's page opens her sealed medical notes** — blood group, medication, allergy, a daughter's number — decrypted from a KMS customer-managed key for that one person, and her screen says *"Ravi has your medical notes."* If he can't go after all, one more tap on that page says so (`POST /release/<token>`, his link alone, within half an hour of the claim): the alert reopens with the step-back on its row, everyone else contacted is told with a fresh link, the machine picks the alert up again at the next circle — `ResumeIncident`, not `CreateIncident`, with the timers it began with — and her screen says *"Ravi can't come after all"* and who is told now (check 17).
7. **Her screen updates by itself** (`GET /status`, polled every 3 s): *✓ Ravi is coming*. Each state after the press is also read aloud in her language by the phone's own voice — she does not have to read it — and silent where the phone has no voice for that language. A Cancel button stays on it; she may be fine after all.
8. **Where she is.** She may not be at home. Her page asks once, at setup, whether helpers may see where she is (a small line under the button, gone once answered either way); after that every press sends her phone's position — after the page is already paged, so the alert never waits on GPS — and the page a responder opens says *Her phone, at 2:41 pm: within 20 m of this spot*, with a maps link, beside her home address. It is written only on a live alert (`POST /location`, check 16) and nothing else trusts it.
9. **Afterwards, what happened, in order** (`GET /incident/<id>`, linked from every settled page a responder sees): the press, each circle's sends with one timestamp, who answered, who went, who opened her notes, the ending — every line a row this system wrote, nothing inferred, refreshing itself while the alert is open. For her family, and for anyone who was paged and wants to know.

<p align="center">
  <img src="docs/01-her-button.png" width="19%" alt="Her screen: one button, I NEED HELP, and who will be told">
  <img src="docs/02-the-alert.png" width="19%" alt="The alert a contact opens: EMERGENCY, her name, address, Open in maps, I'm going now">
  <img src="docs/03-youre-going.png" width="19%" alt="After claiming: You're going, the address, and her sealed medical notes">
  <img src="docs/04-already-on-the-way.png" width="19%" alt="The loser of the race: Ravi is already on the way">
  <img src="docs/05-cancelled.png" width="19%" alt="After she cancels: Sunita cancelled this alert">
</p>
<p align="center">
  <img src="docs/06-her-button-marathi.png" width="19%" alt="Her screen in Marathi: मला मदत हवी आहे, and who will be told">
  <img src="docs/07-cant-come.png" width="19%" alt="Her screen after the one who claimed stepped back: Ravi can't come after all, and who has been told now">
  <img src="docs/09-checkin.png" width="19%" alt="The weekly check-in a contact opens: NOT AN EMERGENCY, Sunita is fine, I'd be reachable now">
  <img src="docs/08-timeline.png" width="38%" alt="The timeline of one alert: the press, three told at once, Ravi is going, notes opened, Ravi can't go after all, the next circle paged, cancelled, everyone told">
</p>

*Top: her button · the alert (with where her phone is) · you're going (the notes, and "I can't go after all") · already on the way · cancelled. Bottom: her button in Marathi · her screen after Ravi stepped back · the weekly check-in · one alert's timeline, every line a row. All from real alerts on the live stack, 18 Sep. Every text colour on these screens is at least 7:1 against its background, the AAA line; the button is 240 px tall, every other target at least 48 px.*

### Correctness properties, stated

| Property | How |
| --- | --- |
| A circle is paged in the same instant, not in sequence | `NotifyTier` is a `Map`, not a chain of tasks |
| Two people answering at once produce exactly one winner, and the loser learns who | one conditional write on the incident row; the page renders from the row after the write, never from what the request hoped |
| Re-running a send never pages twice | the notification row is written first with `attribute_not_exists`; a retry that finds it delivered sends nothing |
| A second press during an alert does not start a second alert | `POST /trigger` returns the alert already running for her — open, widened, or claimed within the last half hour — instead of starting another; her page, reopened, shows that alert (check 15) |
| A link that is prefetched by a mail scanner claims nothing | **GET never writes.** Claim links render on GET and claim on POST; the button is the claim |
| A circle that reached nobody fails loudly | one bad address is contained to its iteration; a whole circle with zero deliveries goes to `RecordFailure`, the execution fails, and a metric fires — no silent minute of waiting |
| A claim or a cancel is acted on now, not at the tier boundary | `WaitForClaim` is `dynamodb:updateItem.waitForTaskToken`; the same write that wins the race returns the token |
| She can cancel at any point, including after someone claimed | after a claim the machine has finished, so the web function asks the broadcast function for the false alarm directly |
| The one who claimed can step back, and nobody is left believing help is coming | `POST /release` is conditional on `status = CLAIMED AND claimed_by = me AND claimed_at` recent; it reopens the row, starts a second execution that resumes at the circle reached, and tells everyone contacted — with a fresh link — before the next circle is paged (check 17) |
| The medical notes are ciphertext at rest and open for one person | KMS CMK, `EncryptionContext={subject_id}`, decrypt permitted to the web role only and only with a context; every opening logged |
| A check-in link can never claim an incident | a check-in row has no incident; `/claim` answers 404 to it, and `/checkin` counts an answer once, within ten minutes of the send, never later (check 13) |
| Someone who leaves her list is never paged, asked or named again | `left_at` on the contact row; `ranking.her_list()` is the one reading of the list that the machine, the broadcasts, the check-in and her screen share (check 18) |

## Architecture

```mermaid
flowchart LR
  P["Her phone<br/>GET /  ·  POST /trigger"] --> W[web Lambda<br/>Function URL]
  W -->|StartExecution| SM
  subgraph SM [pukaar-escalation — Step Functions Standard]
    direction TB
    ST{Start} -->|press| C[CreateIncident] --> S[SelectTier<br/>availability ranking]
    ST -->|someone stepped back| RI[ResumeIncident] --> S
    S --> M{{NotifyTier<br/>Map, parallel}}
    M --> T[TallyNotified] --> D{NotifyDecision}
    D -->|0 reached| RF[RecordFailure] --> F((Failed))
    D --> WT[WaitForClaim<br/>updateItem.waitForTaskToken]
    WT --> CC[CheckClaim] --> CD{ClaimDecision}
    CD -->|claimed| B1[BroadcastClaim] --> DN((Done))
    CD -->|cancelled| B2[Cancelled] --> DN
    CD -->|none, more circles| NT[NextTier] --> S
    CD -->|none, last circle| B3[FinalFallback] --> DN
  end
  M -->|SES| R["Ravi · Vaishali · Anil<br/>one-time links"]
  R -->|GET / POST /claim/token| W
  W -->|conditional UpdateItem<br/>+ SendTaskSuccess| WT
  SCH[EventBridge Scheduler<br/>weekly] --> CK[checkin Lambda<br/>not an emergency: could you go now?] -->|SES / Telegram| R
  W -->|kms:Decrypt, subject_id context| K[(KMS CMK)]
  W --- DDB[(DynamoDB: incidents, contacts,<br/>notifications, response_stats, subjects)]
```

- **Compute:** eight Python 3.13 Lambdas on arm64, 128 MB, one zip. Six run inside the machine and the weekly check-in runs from an EventBridge Scheduler cron, all under role `pukaar-lambda` (DynamoDB, SES, one metric namespace — **no KMS**); `web` runs under `pukaar-web` (DynamoDB, start/wake the machine, invoke `broadcast`, `kms:Decrypt` — **no SES**).
- **Data:** five on-demand DynamoDB tables. `notifications` stores only the **SHA-256 of the link token** (GSI `token_hash-index`); the plaintext exists in the email alone. `subjects.record` is a Binary ciphertext.
- **Edge:** one Lambda Function URL, eleven routes, no API Gateway, no login (the button is hers; the links are one-time, per incident, per person).
- **Channels:** email through SES, always; Telegram through the Bot API for a contact row that carries a chat id — the same message and the same link, so a person counts as reached if either channel took it. The bot token is a sensitive Terraform variable in `terraform.tfvars` (gitignored), passed only to the paging functions; the chat ids come from the same file through `seed.sh`, never from the repo.
- **Infra:** Terraform, AWS provider 6.x, log retention 7 days, everything in [`main.tf`](main.tf).

## Availability ranking — who is in the first circle

The circle is a *membership*, not an order. Inside a parallel `Map` order is invisible, so the only thing the ranking can change is **who is paged at all in the first minute**. [`lambdas/ranking.py`](lambdas/ranking.py):

```
score = 0.6 · answers + 0.2 · fast + 0.2 · near

answers  share of pages answered, smoothed by a prior worth two pages
         (one answered if usually home during the day, half if not);
         pages in the current hour bucket (e.g. 14#weekday) count twice
fast     1 / (1 + mean answer time in minutes); 0.5 unknown; 0 if paged and never answered
near     1 / (1 + metres / 100)
```

So a known answerer beats an unknown, an unknown beats a known non-answerer, and distance decides only between people the history cannot separate. Every page (`notify`) adds to `pages_sent` for that person and hour; every tap on a link (`web`) adds one response and its latency — once per page, win or lose, because a lost race still says they were reachable.

**Between emergencies, a weekly check-in** ([`lambdas/checkin.py`](lambdas/checkin.py), EventBridge Scheduler, Wednesday 6 pm IST by default) pages everyone on her list once, on the same channels, with a message that says first that it is not an emergency and asks one thing: *if you could go to her right now, tap.* A tap within ten minutes counts as an answered page for that hour; no tap counts as a page that went unanswered — which is the honest reading of "could you go right now?" — and a tap after ten minutes counts nothing. So the ranking learns who is reachable at which hour from six low-stakes questions a week instead of from emergencies alone. The check-in's link resolves to a notification row with no incident behind it; it cannot claim anything.

**What it changes, on the seeded circle** (`seed.sh`; the histories are seeded, the counters written since are real; scores as at any hour but the seeded 2 pm one, when those rows count twice):

| | Nearest three by distance | First circle by score |
| --- | --- | --- |
| paged at once | **Meena** (8 m, has never answered a 2 pm page), Vaishali (40 m), Anil (60 m) | **Vaishali** (5/5, 0.775), **Ravi** (son, 4 km, 4/4, 0.569), **Anil** (no history, usually home, 0.525) |
| a minute later | Ravi, Sunil, Prakash | Sunil (0.491), Prakash (0.257), **Meena** (0.223) |

`SelectTier`'s output and log line carry the full ranking with a *basis* per person (`"5/5 answered"`, `"no history; usually home"`, `"0/6 answered, 0/6 this hour"`), so the choice is inspectable in the execution history. Check 7 of `verify.sh` asserts the nearest non-answerer is out of the first circle and paged in the second, and the best answerer is in the first.

## The sealed record

Her medical notes are encrypted at seed time with `kms:Encrypt` under `alias/pukaar-record`, a customer-managed key, with `EncryptionContext={"subject_id": "sunita"}`, and stored as a DynamoDB Binary — 251 bytes, no envelope, nothing in the tables in the clear. Decrypting with another subject's id, or none, is `InvalidCiphertextException` (check 9). The only principal allowed `kms:Decrypt` is the web role, and only when the call carries a `subject_id` context (`Null` condition in IAM); `simulate-principal-policy` says `implicitDeny` for the paging role, and for the web role without a context.

The record renders on exactly one page: the claim page in its *You're going* state. Before the claim, on the loser's page, on a cancelled or finished alert, it is absent (checks 9 and 11). Every opening writes one log line — `{"event": "record_released", "incident_id": "3d1e4f4cac0a", "contact_id": "ravi", "subject_id": "sunita", "at": "2026-09-17T12:40:41+00:00"}` — and the opening that comes with the claim is written on her incident so her screen can say who has them. CloudTrail carries the matching `Decrypt` event, id `5d264b7a-3763-49ae-8daa-08f3e8f69bf4`, 18:10:41 IST, role `pukaar-web`, context `{subject_id: sunita}`.

## Cost

ap-south-1 list prices from the AWS Price List API on 17 Sep 2026, free tiers ignored; transitions, machine invocations and emails counted off real executions of this stack (`v-163958-claim` on 18 Sep; `cost-182725` on 17 Sep, plus the one `Start` transition every execution has had since 18 Sep). [`cost.py`](cost.py) prints this table and [`tests/test_cost_numbers.py`](tests/test_cost_numbers.py) fails if the README drifts from it.

| Incident | Step Functions | Lambda | DynamoDB | SES | KMS | Total |
| --- | --- | --- | --- | --- | --- | --- |
| Ravi claims at the first circle (14 transitions, 6 emails) | $0.00040 | $0.00003 | $0.00002 | $0.00090 | $0.00001 | **$0.00136** (₹0.12) |
| Nobody claims: three circles, then the fallback (39 transitions, 18 emails) | $0.00111 | $0.00007 | $0.00006 | $0.00270 | $0.00000 | **$0.00394** (₹0.35) |

Idle, per subject per month: $0.00364. Fixed, whole system: one KMS key, $1.00/month.
The weekly check-in to her six people: $0.00396 per subject per month (26 emails).
A thousand people, one incident each a month: about $6/month (₹527); with the weekly check-in, about $10/month (₹876).

Decisions made for cost: **Standard, not Express** workflows — a parked `waitForTaskToken` costs nothing per second, and the wait is the whole product; **a Function URL, not API Gateway** — eleven routes, no auth layer to pay for; **arm64** Lambdas at 128 MB; **DynamoDB on-demand** — near-zero traffic between incidents; **no VPC**, so no NAT Gateway; **log retention 7 days**; **email, not SMS** — SES is about ₹0.013 a message against ₹0.20+ for Indian SMS, and sender-ID SMS needs a registration this weekend does not have; Telegram costs nothing. The largest line in the whole bill is the $1 key.

## What I learned

One sentence separates the two lists: **the first list was practised before kickoff and is here for honesty; only the second was learned during the four days**, and each of its entries links the commit that fixed it and the log entry written within thirty minutes.

**Practised before kickoff, not claimed:** Step Functions `Map`, `Choice` and `Wait`; passing state between tasks with `ResultPath`; Terraform for Lambda + DynamoDB + IAM; SES production access on a verified domain; that a circle which reached nobody must be a failure of the machine, not a timed wait (found 10 Sep on the plan, before any code); that AWS-managed KMS keys cannot be called from your own code and that Python's standard library has no AES (read in the KMS developer guide on 12 Sep, so the record uses a customer-managed key and direct `Encrypt`/`Decrypt` with no envelope).

**Learned during the four days** — from [`LEARNING-LOG.md`](LEARNING-LOG.md):

1. **A public Function URL is not public with `authorization_type = NONE` alone.** Since October 2025 it also needs `lambda:InvokeFunction` conditioned on `lambda:InvokedViaFunctionUrl`; the URL resource's own `InvokeFunctionUrl` grant was already there and every request still got 403. The argument does not exist in the AWS provider 5.x, so the provider moved to 6.x. Fix [`fae8b5d`](https://github.com/aryangorde6/pukaar/commit/fae8b5d), entry 13:15.
2. **`git log --reverse --max-count=1` returns the newest commit**, because the limit is applied before the reversal — so the log's own test was green on an empty log and red on the first real entry. `git rev-list --max-parents=0` names the root commit. Fix [`e166ce0`](https://github.com/aryangorde6/pukaar/commit/e166ce0), entry 13:19.
3. **The wait was a timer, so "immediate" was a tier late.** A `Wait` + poll acted on a cancel 15.4 s after she pressed it on a 20 s test timer — up to a minute in production. `.waitForTaskToken` composes with a plain DynamoDB SDK integration: the *write that parks the token* is a conditional `updateItem`, `TimeoutSecondsPath` keeps the tier timeout as an input, and the two exits (`States.Timeout`, `DynamoDb.ConditionalCheckFailedException`) are caught into the same `CheckClaim`. A stale token raises `TaskTimedOut`, not `InvalidToken`. After: 1.0 s. Fix [`9a1cf95`](https://github.com/aryangorde6/pukaar/commit/9a1cf95), entry 13:52.
4. **One ignored page erased five answered ones.** The first ranking used this-hour evidence if any existed, else any-hour evidence: one unanswered test page at 17:56 turned a 5/5 answerer into "0/1 this hour" and two strangers to the history were paged before her — and `verify.sh` still passed, because check 7 never asked whether the best answerer was in the circle. Pooled counts with a two-page prior, this hour counted twice; check 7 now asks. Fix [`6eb8ad0`](https://github.com/aryangorde6/pukaar/commit/6eb8ad0), entry 18:00.
5. **The Cancel button on "Ravi is coming" did nothing.** Cancel accepted `OPEN` and `FALLBACK` only, and once someone has claimed the machine has already broadcast and finished, so there was no token to wake either. Cancel now accepts `CLAIMED` and the web function invokes `broadcast` for the false alarm itself; the web role gained that one `lambda:InvokeFunction`. Fix [`4ebb86b`](https://github.com/aryangorde6/pukaar/commit/4ebb86b), entry 18:18.
6. **Reseeding before the checks protected the checks, not the button.** The pages the eleven checks send are the last thing written, and the ranking learns from every page, so from the end of a run until the next reseed the live button ranked on test pages — it read *Vaishali, Anil and Ravi*, and twice that evening *Vaishali, Sunil and Ravi*, while this README quotes *Vaishali, Ravi and Anil*. `verify.sh` now reseeds after the checks as well as before; presses made by hand still need `./seed.sh` after them. Fix [`f6512d6`](https://github.com/aryangorde6/pukaar/commit/f6512d6), entry 23:10.

Also new to me this week, without a break to log: a KMS encryption context as the thing that binds a ciphertext to one person; the IAM `Null` condition on `kms:EncryptionContext:subject_id`; `aws iam simulate-principal-policy` as evidence you can paste; that CloudTrail's `Decrypt` events for a Lambda's own environment variables sit next to yours, with `aws:lambda:FunctionArn` as their context.

## What it does not do

- **No SMS or calls.** Email, and Telegram for whoever on her list has started her bot; no SMS, because Indian sender-ID SMS needs a registration a weekend does not have, and no calls. A Telegram bot cannot message someone first, so the son has to tap *Start* once at setup — that is the consent step this channel gets for free.
- **Dispatch, not delivery.** SES accepts the message; where Gmail files it is unobservable from this side and drifts. Every check here asserts on the send, the row and the message id.
- **The histories in the demo are seeded.** `seed.sh` writes the 2 pm records that make the ranking visible and, on re-run, deletes everything counted since; between runs the counters are real (a real claim at 5:57 pm added `responses 1, 46 000 ms` to Ravi's `17#weekday` row, until the next reseed), and the weekly check-in below grows them without waiting for an emergency — one hour a week, so a year covers about fifty hours of the week, not all 168.
- **Consent and caps for contacts are the operator's, not built in.** Nothing can add a stranger's address: subjects and contacts enter through a script the operator runs. There is no self-serve form, precisely because an open form here is an open relay. Leaving *is* built: the foot of every message that carries a link is the way off her list (`/leave/<token>` — asks on GET, writes on POST), and from then on the ranking, the broadcasts, the check-in and her screen all read the same list and skip that person (check 18).
- **The unconscious case is not covered.** She has to press. A passive check-in backstop was cut first.
- **One subject.** The web function serves one person's button (`SUBJECT_ID`); many subjects is a routing change, not a design change.
- **The record opening is logged, not gated by a second factor.** Whoever holds a winning link sees the notes. The link is one-time, per incident, per person, hashed at rest, and dies with the incident.

## Prior art

Commercial systems converge on this shape — [Alerto](https://alertotech.com/), [HelpQR](https://helpqr.org/blog/sos-app-india) — and that convergence validates the approach; nothing here is claimed as novel. What is mine is an AWS-native implementation with *stated* correctness properties: no double page under retry, acknowledgement races resolved by one atomic write, the wait as a callback rather than a timer, a first circle chosen by who actually answers, and sealed data released only on a claim and logged when it is.

## Run it

```bash
terraform init && terraform apply          # AWS_PROFILE and region in variables.tf
./seed.sh                                  # Sunita, her six contacts, their histories, her sealed notes
./verify.sh                                # eighteen live checks; reseeds before and after
.venv/bin/pytest -q                        # ranking, the learning log, the cost numbers, every string and message, the check count
```

`variables.tf` holds the sender identity (SES production access on a verified domain is assumed), `wait_s` (60 in production; every test above passes 3–25) and `max_tier`. Telegram is optional: `terraform.tfvars` (gitignored) with `telegram_bot_token` from @BotFather and `telegram_chat_ids = { ravi = "…" }`; without it, email alone. `checkin_schedule` is the weekly check-in's cron in IST (Wednesday 6 pm by default; empty disables it); `aws lambda invoke --function-name pukaar-checkin` sends one now.

---

AI tools used in this build: Claude Code (Anthropic), as a coding assistant.
