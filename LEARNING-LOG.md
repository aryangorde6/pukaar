# Learning log

What was new, what was hard, and what was wrong first. Written within thirty
minutes of each fix, never batched at the end. Every entry names the commit that
fixed it; `tests/test_learning_log.py` checks that each hash resolves and that
each timestamp falls inside the event window.

Entries use one fixed template so the log cannot drift into a diary:

```
## <YYYY-MM-DD HH:MM IST> — <one line: what this was about>
Tried:            what I was doing
Broke:            the error, pasted verbatim
Wrong assumption: what I believed that was not so
Fix:              <commit hash> — what changed
Evidence:         a command and its output, an ARN, a screenshot path
```

Things practised before the event (Step Functions Map/Choice/Wait, the Terraform
build order, the Lambda/IAM wiring) are not entries here. They are listed in the
README under *practised before kickoff*, so nothing rehearsed is claimed as
learned during the four days.

---

## 2026-09-17 13:15 IST — a public Function URL that answered 403 to everyone
Tried:            Serve the help button from a Lambda Function URL with `authorization_type = "NONE"`.  
Broke:            `HTTP/1.1 403 Forbidden` · `x-amzn-ErrorType: AccessDeniedException` ·
                  `{"Message":"Forbidden. For troubleshooting Function URL authorization issues, see:
                  https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html"}`, on GET / and POST /trigger.  
Wrong assumption: That `NONE` means public. It only means Lambda skips IAM authentication; the function's
                  resource policy still decides. I then assumed the missing grant was `lambda:InvokeFunctionUrl`
                  and added it. Still 403. The policy already had that statement (the URL resource adds it).
                  Since October 2025 a public URL also needs `lambda:InvokeFunction`, conditioned on
                  `lambda:InvokedViaFunctionUrl = true`. That was the one missing.  
Fix:              fae8b5d — `aws_lambda_permission.web_url_public_invoke` with `invoked_via_function_url = true`.
                  The argument does not exist in AWS provider 5.100, so the provider moved to 6.x (6.65.0);
                  the plan after the upgrade was the one permission and nothing else.  
Evidence:         `curl -s -o /dev/null -w "%{http_code}" $URL` → `200`; `aws lambda get-policy` on `pukaar-web`
                  lists `FunctionURLAllowPublicAccess` and `AllowPublicFunctionUrlInvoke`. The docs page's note:
                  *"Starting in October 2025, new function URLs will require both lambda:InvokeFunctionUrl and
                  lambda:InvokeFunction permissions."* The practice stack never had a Function URL.

## 2026-09-17 13:19 IST — the log's own test was green on nothing and red on the first entry
Tried:            Run `tests/test_learning_log.py` after writing the entry above.  
Broke:            `AssertionError: 2026-09-17 13:15 is outside Thu Sep 17 13:15:39 2026 .. Sun Sep 20 20:00:00 2026`  
Wrong assumption: That `git log --reverse --format=%cI --max-count=1` prints the first commit. It prints the
                  newest one (`--max-count` is applied before `--reverse`), so the "window" started at the
                  commit I had just made. With zero entries the test had nothing to check and passed anyway;
                  the first real row exposed it. That is the bug class this project audits for (a verdict
                  from absent evidence), and it was in the auditor.  
Fix:              e166ce0 — `git rev-list --max-parents=0 HEAD` for the root commit, floor truncated to the minute.  
Evidence:         `4 passed`; the root commit is `271deb7 2026-09-17T12:56:53+05:30`, the entry above is 13:15.

## 2026-09-17 13:52 IST — the wait was a timer, so "immediate" was a tier late
Tried:            The spine as designed: `WaitForClaim` as a `Wait` state with `SecondsPath`, then `CheckClaim` polls the
                  row. It worked, and it was what the practice stack ran. Then I measured it.  
Broke:            Nothing threw. Execution `t4c-133806`: `WaitForClaim` entered 13:38:09.250, `POST /cancel` landed at
                  13:38:14.124, the `Cancelled` state (the one that tells the neighbours it was a false alarm) was
                  entered at 13:38:29.548. **15.4 s** after she pressed cancel, on a 20 s test timer; on the 60 s
                  production timer that is anything up to a minute of three people getting ready to walk over.
                  Correctness property 3 in the design says *immediate*. The machine could not know the row had
                  changed until its timer ran out.  
Wrong assumption: That a Wait + poll was "immediate enough" because the poll is right after the wait. It is
                  immediate only at the tier boundary. The fix is the one Step Functions feature the practice
                  stack never used, `.waitForTaskToken`, and I did not know, before today, that it composes with
                  a plain DynamoDB SDK integration: the *write that parks the token* is a conditional `updateItem`
                  (`status = OPEN`), `TimeoutSecondsPath` keeps the tier timeout as an input, and the two exits
                  (`States.Timeout`, `DynamoDb.ConditionalCheckFailedException`) are caught ahead of `States.ALL`
                  into the same `CheckClaim`. I also assumed a stale token would raise `InvalidToken`; it raises
                  `TaskTimedOut` (from the log: `{"event": "wake_skipped", "error": "TaskTimedOut"}`).  
Fix:              9a1cf95 — `WaitForClaim` is now `arn:aws:states:::aws-sdk:dynamodb:updateItem.waitForTaskToken`;
                  the claim's and the cancel's conditional `UpdateItem` use `ReturnValues=ALL_OLD` so the token comes
                  back in the same write that won the race, and `SendTaskSuccess` is best-effort.  
Evidence:         Same test, after: cancel → `Cancelled` entered **1.0 s** later; claim → `BroadcastClaim` done in
                  **2.8 s** including the three emails (`t27-claim-*`, `t27-cancel-*`). Timeout path still widens
                  (`t27a-134849`: `TaskTimedOut States.Timeout` at +5.07 s, then `FinalFallback`). `./verify.sh`
                  6/6 (`v-135012-*`). The IAM it needed: `dynamodb:UpdateItem` on the machine's role,
                  `states:SendTaskSuccess` on the web function's.

## 2026-09-17 18:00 IST — one ignored page erased five answered ones
Tried:            Availability ranking. First version: use the response history for *this hour bucket* if
                  the person has ever been paged in it, else their history at any hour, else a declared prior.  
Broke:            Nothing threw. The first live run after deploy (`931591413163`, 17:56, bucket `17#weekday`)
                  paged Vaishali, Ravi and Anil and wrote one page each into `17#weekday`; Ravi claimed. The
                  next run two minutes later (`v-175839-*`) chose tier 1 = `['ravi', 'sunil', 'prakash']`:
                  Vaishali (seeded 5/5 at 2 pm, 40 m away) was now "0/1 this hour" and lost to two people
                  with no history at all. `verify.sh` still said 8/8, because check 7 only asked that the
                  nearest non-answerer stay out of tier 1.  
Wrong assumption: That evidence from the same hour is always better than evidence from any hour. One page is
                  not evidence of anything; a cliff between "this hour" and "any hour" let a single unanswered
                  page outrank a whole history. And a passing check that never asked whether the best answerer
                  was paged is the 4.7b bug class again: a verdict from absent evidence.  
Fix:              6eb8ad0 — pooled counts with a prior worth two pages (one answered if usually home, half if not);
                  this hour's pages count twice, so the hour matters without deciding alone. Check 7 now also
                  requires the best seeded answerer in tier 1; `verify.sh` reseeds first, because the ranking
                  learns from the checks' own unanswered pages. `test_one_unanswered_page_does_not_erase_history`.  
Evidence:         Same shape, after: 5/5 plus one ignored page this hour scores 0.661 against an unknown's 0.525
                  (unit test). Live `v-180259-fanout`: tier 1 `['vaishali', 'ravi', 'anil']` vs nearest three
                  `['meena', 'vaishali', 'anil']`; SelectTier's log line carries every score and its basis
                  (`vaishali 0.775 "5/5 answered"` … `meena 0.223 "0/6 answered"`). `./verify.sh` 8/8.

## 2026-09-17 18:18 IST — the Cancel button on "Ravi is coming" did nothing
Tried:            The UI pass, walking her screens on a phone-sized viewport with a real claimed incident
                  (`2d328bcc7fa1`, Ravi claimed at 6:17 pm).  
Broke:            `POST /cancel {"incident_id": "2d328bcc7fa1"}` → `{"status": "CLAIMED", ...}`. No error, no
                  change. The *Coming* screen shows **Cancel — I'm OK** ("she may be fine after all, and she is
                  allowed to say so"), and pressing it left the row `CLAIMED` and nobody told.  
Wrong assumption: That a cancel only matters before anyone claims. The conditional write accepted `OPEN` and
                  `FALLBACK` only, and by the time someone has claimed the machine has already broadcast and
                  finished; there is no parked token to wake, so even widening the condition would have
                  changed the row and told no one. The button was shipped from the copy without a path behind it.  
Fix:              4ebb86b — cancel accepts `CLAIMED`; when the old status was `CLAIMED` the web function invokes
                  `pukaar-broadcast` (`kind = false_alarm`) directly instead of waking the machine. The web role
                  gains `lambda:InvokeFunction` on that one function and still cannot send mail itself.  
Evidence:         Same incident, after: `{"status": "CANCELLED", ..., "cancelled_at": "6:20 pm"}`; the row's
                  `broadcast` = `{"kind": "false_alarm", "told": ["vaishali", "anil", "ravi"]}`; Ravi's claim link
                  now renders "Sunita cancelled this alert" with no record. `./verify.sh` check 11, 11/11
                  (`v-182122-*`).

## 2026-09-17 23:10 IST — reseeding before the checks protected the checks, not the button
Tried:            The cold re-read of the README against the live stack, claim by claim. Line 23 quotes her
                  screen: *"Vaishali, Ravi and Anil will be told straight away"*.  
Broke:            The live page read *"Vaishali, Anil and Ravi"*: same circle, Anil above Ravi. Earlier in the
                  evening, twice, it had read *"Vaishali, Sunil and Ravi"*: Anil out altogether. Nothing
                  threw; `verify.sh` was 11/11 twenty minutes before. `pukaar-response-stats` held the pages
                  the last run had sent and nobody had answered: `ravi 22#weekday 5/0`, `anil 22#weekday 3/0`,
                  plus the `verify` rows, next to the three seeded ones.  
Wrong assumption: That reseeding *before* a run was the whole fix (entry 18:00). It gives the checks a known
                  history; it does nothing for what the checks leave behind. The ranking learns from every page
                  and the checks' pages are the last thing written, so from the end of a run until the next
                  reseed the live button (the thing a judge opens) ranked on test pages. The README quotes a
                  sentence the button was not showing.  
Fix:              f6512d6 — `verify.sh` reseeds after the checks as well as before, whatever the exit code, so a
                  run leaves the circle as seeded. Manual presses and screenshot incidents still leave their
                  pages behind; the rule for those stays `./seed.sh` before every take and every idle look.
                  The same commit fixes `verify.sh`'s header ("eight" checks; there are eleven), states the
                  contrast floor as the computed minimum (7.28:1 → "7:1, the AAA line"), and notes in the
                  README that the ranking table's scores are the off-hour ones and that a reseed deletes the
                  counters written since.  
Evidence:         Before: `aws dynamodb scan --table-name pukaar-response-stats` → 11 rows, and `curl` of the
                  Function URL → "Vaishali, Anil and Ravi will be told straight away". After `./verify.sh`
                  (11/11, `v-230629-*`): the scan → exactly the three seeded rows (`vaishali 14#weekday 5/5`,
                  `ravi 4/4`, `meena 0/6`); `curl` → "Vaishali, Ravi and Anil will be told straight away".
## 2026-09-18 19:41 IST — the incident id was all a cancel needed, and the timeline had just put it in every inbox
Tried:            Choosing the next thing to build after `5dad4c2`, which had added *What happened, in order:
                  …/incident/<id>* to the two messages that ask nothing of anyone (*Ravi is going*, *False
                  alarm*), on email and Telegram, so a family could follow an alert without opening a link.  
Broke:            Nothing threw, and 18/18 was green. `POST /cancel {"incident_id"}` and `POST /location
                  {"incident_id", lat, lon}` asked for the id and nothing else, and the id was now in the
                  inbox of everyone reached, on Telegram, and on every settled claim page since the timeline
                  shipped (`573494d`). Anyone paged could call off her alert (six *False alarm* messages, her
                  screen *Cancelled*) or move her phone's position to a spot of their choosing while
                  responders were reading it. Reproduced: `curl -X POST …/cancel -d '{"incident_id":"…"}'`
                  → `200 {"status": "CANCELLED"}` from a shell that had never seen her page.  
Wrong assumption: That an id safe to *read* by (`GET /status/<id>`, `GET /incident/<id>`) was safe to *act*
                  by. Her page had been the only thing that knew the id, so the id stood in for her page; the
                  moment the id was shared for reading, the writes it guarded were shared too. The README's
                  "the links are one-time, per incident, per person" described the responders' side only;
                  her side had no credential at all beyond the URL of the button.  
Fix:              0dac038 — a `random_password` in Terraform, `HER_KEY` in the web function's environment,
                  rendered into her page's script and nowhere else; `/cancel` and `/location` refuse a body
                  without it (403, `hmac.compare_digest`), before any write. The boundary is now stated in the
                  README: her page cancels and places her; a responder's link claims, steps back, leaves; a
                  timeline id reads. Check 6 cancels first without the key (403, row still OPEN), check 16
                  sends a position without it (403, nothing written); `verify.sh` and `shoot.sh` read the key
                  from `terraform output`.  
Evidence:         `curl -X POST …/cancel -d '{"incident_id":"019d22d6b6e2"}'` → `403 {"error": "only her page
                  can cancel"}`; the same with `"key":"x"` → 403; `…/location` without it → `403 {"error":
                  "only her page can say where she is"}`; `curl …/` → `var key = "` once. A press and a
                  cancel from her page in the browser (`577303261acd`, 19:47) → *Cancelled*, web log
                  `trigger`, `cancel`, `woke`. `./verify.sh` 18/18, `v-195011-*`.

## 2026-09-19 01:05 IST — "refreshing itself" meant a meta refresh, and axe calls that critical
Tried:            An accessibility pass on every page a person can open, with axe-core 4.10.2 loaded into
                  the live pages from the browser, every rule set it has including best-practice; the README
                  now states the page's design rules, so each one should survive a tool.  
Broke:            Her page: `region [moderate]: .foot`. The *Not working? Call 112* line sat outside any
                  landmark, and the responder pages had no landmark at all (`<body>` → content → `.foot`).
                  The timeline: `meta-refresh [critical]: meta[http-equiv="refresh"]`. The page reloaded
                  itself every five seconds while the alert was open, which throws a screen reader back to
                  the top each time (WCAG 2.2.1 / 3.2.5). It also stopped one refresh too early: the row
                  closes on the claim or the cancel, and the line *everyone was told* is written a second or
                  two later, so the last thing the open page ever showed was one line short.  
Wrong assumption: That "the page refreshes itself" was a harmless way to keep a timeline live, and that
                  landmarks were a formality on a one-screen page. A reload is a navigation; for someone
                  listening to the page rather than looking at it, it is the whole page again, every five
                  seconds, for as long as the alert runs.  
Fix:              750968b — `<header>`, `<main>`, `<footer>` on every page (`_page`, her page, the 404); the
                  timeline fetches itself every 5 s and appends only the lines it does not have (the card
                  and the list are `aria-live="polite"`), stops when the row settles, and takes one last look
                  5 s later for the broadcast line.  
Evidence:         Same document, no reload: a marker set on `window` at load survived a claim: three lines
                  appended, the card flipped to *✓ Ravi went*; and survived a cancel on a second alert; the
                  false-alarm line landed on the last look (`shot-010403-axe`, `shot-011028-axe2`). axe after
                  the fix: 0 violations on her page in all six states, the alert, *You're going*, *already on
                  the way*, *cancelled*, the check-in ask and counted pages, the leave page, the timeline open /
                  claimed / cancelled, the 404. `./verify.sh` 18/18, `v-011325-*`.

## 2026-09-19 12:48 IST — her screen, reopened during an alert, named a man who was never told
Tried:            Screenshots of her page through one real alert for the README, the one set of screens
                  it did not have: after the press, when someone answers. Each taken by opening the page
                  fresh while the alert ran, as she would if she put the phone down and picked it up.  
Broke:            Her page said **Vaishali, Sunil, Ravi and Anil have been told.** The alert `d05a6e6338ce`
                  had three delivered rows: `anil#1`, `ravi#1`, `vaishali#1`. Sunil was never paged.
                  (`docs/evidence-12-37-four-names.png`; `aws dynamodb query --table-name pukaar-notifications
                  --key-condition-expression 'incident_id = :i'` → three rows, none for sunil.)  
Wrong assumption: That "who a press right now would page" is the same list as "who this alert paged", so
                  the reopened page could start from the ranking and let the poll add names. It is never the
                  same list once the alert has started: the ranking learns from every page, so the three who
                  were just paged and have not answered drop, Sunil rises, and the page opened on
                  *Vaishali, Sunil, Ravi* and then unioned the row's *Vaishali, Ravi, Anil*. Check 15 opened
                  the page too, before the rows had landed, so it never saw the difference.  
Fix:              821a19e — reopened during an alert, `trigger_page` starts from `told_names(running)`
                  (the delivered rows, minus anyone who stepped back), falling back to the ranking only
                  while no row exists yet. Check 15 now waits for the rows and asserts the page's names
                  equal the reached set exactly, no more, no fewer.  
Evidence:         Same alert shape after the fix (`8ed1e8d50174`): *Anil, Ravi and Vaishali have been told*
                  (`docs/11-help-is-being-called.png`), three rows, three names. `./verify.sh` 19/19,
                  `v-124207-*`: check 15 "names ['Anil', 'Ravi', 'Vaishali'] vs reached ['Anil', 'Ravi',
                  'Vaishali']". The property in the README reads *never says less than the row knows*; it
                  now says *never more, either*.

## 2026-09-19 20:39 IST — a default set on the sender, no code changed, and no page could be sent
Tried:            Making a bounced page reach the operator without touching a Lambda: an SES configuration
                  set with an SNS event destination, made the *default* of the sending identity
                  `aryangorde.com`, so every page carries it and nothing in `notify.py` changes. Applied
                  with the other rails (point-in-time recovery, deletion protection, the hour's cap, the
                  alarm on `pukaar-web`), then `./verify.sh`.  
Broke:            Check 1 failed at once: *fan-out writes three delivered rows: 1 tier-1 rows delivered,
                  message ids ['']*, and the run crashed on the empty id. `pukaar-notify`'s log, one line
                  per contact: `AccessDeniedException: User '...assumed-role/pukaar-lambda/pukaar-notify'
                  is not authorized to perform 'ses:SendEmail' on resource
                  'arn:aws:ses:ap-south-1:787565887708:configuration-set/pukaar'`. The same apply had also
                  stopped twice on its way: *Every policy statement must have a unique ID* (three
                  statements on the topic policy, no `Sid`s) and the SES destination refusing to be created
                  until the topic's policy let SES publish, which Terraform was setting after it.  
Wrong assumption: That a permission written for "the identity" covers whatever the identity does. It
                  covered the identity. A default configuration set is a second resource that rides on
                  every send from it, and SES authorises both, so an account-level default reached into
                  eight functions without any of them changing and the policy written for the old shape
                  denied all of them. "No code changed" is not "nothing changed".  
Fix:              be18209 — the paging role's `ses:SendEmail` statement names the configuration set's ARN
                  beside the identity's; `Sid`s on the three statements; `depends_on` from the SES
                  destination to the topic policy. And check 20, which sends one page to SES's bounce
                  simulator *without* naming a set and asserts the bounce is counted and published, so
                  the default is proven on every run, not assumed.  
Evidence:         Before: 18/20 with the fix eight seconds old (IAM was still propagating: a broadcast at
                  20:43 reached two of three, `"failed": [{"contact_id": "anil", "reason":
                  "AccessDeniedException"}]`). After: **20/20**, `v-204528-*`, check 20 *SES Bounce 1,
                  topic published 4 (was 1)*; `simulate-principal-policy` for `pukaar-lambda` on the
                  configuration-set ARN → allowed.

## 2026-09-19 21:55 IST — a second reader found what twenty checks by one author could not
Tried:            Handing the repo to a reviewer that had never seen it (Cursor, read-only, told to report
                  `file:line` with a way to confirm and to skip style and feature ideas) while the code was
                  frozen and the film waited for 2 am.  
Broke:            Five real things, every one on a path the checks never walked. A claim or a cancel after the
                  last circle: the machine is past its last wait, so `wake()` handed back a stale token and
                  nobody else was told (`web.py` `claim`, `cancel`). Worse, a claim landing in the second
                  between `CheckClaim`'s read and `FinalFallback`'s conditional write threw, the spine
                  `Catch` ran `RecordFailure`, and its unconditional write turned a won alert into `FAILED`
                  with her screen stuck on *waiting*. Two presses in the same instant: `trigger` read
                  `open_incident`, started a machine, then wrote the pointer with no condition, so both
                  read "nothing running" and both paged her circle. `POST /cancel` with a body that was not
                  JSON, or a key that was not ASCII, was an unhandled exception (`json.loads` bare,
                  `hmac.compare_digest` on `str`), and since 20:35 an unhandled exception on `pukaar-web`
                  pages the operator. A step-back that started no machine would have left the alert `OPEN`
                  with nobody waiting on it. (And `json.dumps` into her page's `<script>` does not escape
                  `</`; only seeded names reach it, so harmless, but fixed.)  
Wrong assumption: That the checks covered the machine's endings because they covered its states. Every claim
                  and cancel in checks 3, 6 and 11 happened mid-wait, with a live token; check 15 pressed
                  twice in sequence, never at once; check 6 sent a well-formed body with a wrong key, never a
                  malformed one. I tested the paths I had imagined, and I had imagined the ones I built.  
Fix:              09e67c2 — a claim or a cancel on a `FALLBACK` row invokes `broadcast` itself (the machine
                  will not); `broadcast` treats a failed `no_one_reached` condition as "the row moved on" and
                  sends what actually happened (`someone_going` or `false_alarm`) instead of throwing;
                  `record_failure` sets `FAILED` only over `OPEN`/`FALLBACK` and records the reason beside a
                  claim otherwise; her page shows the 112 screen on `FAILED`. `trigger` reads her row once,
                  takes it with a condition on that same value before any machine starts, and a pointer to a
                  row that does not exist yet counts as a running press for thirty seconds (the first version
                  of the fix read twice and still made two alerts: 20/21, `v-220803-*`). `cancel` answers a
                  bad body with 400; `hers()` compares bytes. `release` puts the claim back if
                  `StartExecution` fails. `js()` escapes `</` in anything rendered into a script. Check 21
                  does all of it on the live stack: a late claim on the `FALLBACK` alert from check 18 and a
                  cancel on the one from check 4, both broadcast to everyone reached; two presses from two
                  threads → one alert; `not-json` → 400, `"é"` → 403.  
Evidence:         The reviewer's own offline snippet before the fix: `cancel: JSONDecodeError`, `hers
                  non-ascii: TypeError`; after: 400 and `False`. First deploy 22:08: **20/21**, check 21
                  *two presses at once -> 2 alert (0 joined)*. Second deploy 22:13: **21/21**, `v-221423-*`,
                  check 21 *late claim on FALLBACK … broadcast someone_going told 5 vs reached 5; cancel from
                  FALLBACK … false_alarm told 5; two presses at once -> 1 alert (1 joined); bad body -> 400,
                  non-ascii key -> 403*. Not a finding: the reviewer's claim that a second step-back leaves the
                  first resumed execution waiting — a `CLAIMED` row means the claim already woke it.

---

## What the eleven have in common (written 19 Sep 12:35 IST after the eighth entry; the ninth added 12:55, the tenth 20:50, the eleventh 22:19)

Seven of the eleven were a word I trusted: `NONE` "means public", a poll after a Wait is
"immediate", a meta refresh is "the page refreshing itself", an id that is safe to *read*
by is "safe to act by", reseeding *before* a run "protects the button", the people "told" are the people a press "would tell", a permission on "the identity"
covers what the identity does. In each case the
word described what I wanted and not what the system does, and the fix began with one
command that showed the difference (`curl` against the URL, the cancel's timestamp beside
the tier boundary, a marker on `window`, the timeline link in a stranger's inbox, the
ranking after a run, three rows beside four names, one denied send in a Lambda's log). Three were about the checks themselves: a test that was green on an
empty log, a reseed that protected the checks and not the thing they were checking, and twenty-one
checks that walked only the paths their author had imagined,
which is where the rule at the top of `verify.py` comes from: a check that would pass
against nothing is not a check. One was statistics: a single ignored page is not
evidence, and a ranking needs a prior before it needs a cliff.

What I would carry to the next build: a word like *public*, *immediate* or *safe* does
not go into the README until a command has shown it; the check is written before the
feature when the feature is a promise about behaviour; and the tools that found three of
the eleven (axe, Lighthouse, and a screenshot taken the way she would open the page) run on the first day, not the last night. The tenth is the case for the live checks: a change that touched no code broke the one path that matters, and check 1 said so inside a minute. The eleventh is the case for a second reader: five defects in an hour, on paths the checks never walked, from someone who had not built them.
