# Learning log

What was new, what was hard, and what was wrong first — written within thirty
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
                  https://docs.aws.amazon.com/lambda/latest/dg/urls-auth.html"}` — on GET / and POST /trigger.
Wrong assumption: That `NONE` means public. It only means Lambda skips IAM authentication; the function's
                  resource policy still decides. I then assumed the missing grant was `lambda:InvokeFunctionUrl`
                  and added it — still 403. The policy already had that statement (the URL resource adds it).
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
                  newest one — `--max-count` is applied before `--reverse` — so the "window" started at the
                  commit I had just made. With zero entries the test had nothing to check and passed anyway;
                  the first real row exposed it. That is the bug class this project audits for (a verdict
                  from absent evidence), and it was in the auditor.
Fix:              e166ce0 — `git rev-list --max-parents=0 HEAD` for the root commit, floor truncated to the minute.
Evidence:         `4 passed`; the root commit is `271deb7 2026-09-17T12:56:53+05:30`, the entry above is 13:15.

## 2026-09-17 13:52 IST — the wait was a timer, so "immediate" was a tier late
Tried:            The spine as designed: `WaitForClaim` as a `Wait` state with `SecondsPath`, then `CheckClaim` polls the
                  row. It worked, and it was what the practice stack ran. Then I measured it.
Broke:            Nothing threw. Execution `t4c-133806`: `WaitForClaim` entered 13:38:09.250, `POST /cancel` landed at
                  13:38:14.124, the `Cancelled` state — the one that tells the neighbours it was a false alarm — was
                  entered at 13:38:29.548. **15.4 s** after she pressed cancel, on a 20 s test timer; on the 60 s
                  production timer that is anything up to a minute of three people getting ready to walk over.
                  Correctness property 3 in the design says *immediate*. The machine could not know the row had
                  changed until its timer ran out.
Wrong assumption: That a Wait + poll was "immediate enough" because the poll is right after the wait. It is
                  immediate only at the tier boundary. The fix is the one Step Functions feature the practice
                  stack never used — `.waitForTaskToken` — and I did not know, before today, that it composes with
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
                  Vaishali — seeded 5/5 at 2 pm, 40 m away — was now "0/1 this hour" and lost to two people
                  with no history at all. `verify.sh` still said 8/8, because check 7 only asked that the
                  nearest non-answerer stay out of tier 1.
Wrong assumption: That evidence from the same hour is always better than evidence from any hour. One page is
                  not evidence of anything; a cliff between "this hour" and "any hour" let a single unanswered
                  page outrank a whole history. And a passing check that never asked whether the best answerer
                  was paged is the 4.7b bug class again — a verdict from absent evidence.
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
Broke:            `POST /cancel {"incident_id": "2d328bcc7fa1"}` → `{"status": "CLAIMED", ...}` — no error, no
                  change. The *Coming* screen shows **Cancel — I'm OK** ("she may be fine after all, and she is
                  allowed to say so"), and pressing it left the row `CLAIMED` and nobody told.
Wrong assumption: That a cancel only matters before anyone claims. The conditional write accepted `OPEN` and
                  `FALLBACK` only, and by the time someone has claimed the machine has already broadcast and
                  finished — there is no parked token to wake, so even widening the condition would have
                  changed the row and told no one. The button was shipped from the copy without a path behind it.
Fix:              4ebb86b — cancel accepts `CLAIMED`; when the old status was `CLAIMED` the web function invokes
                  `pukaar-broadcast` (`kind = false_alarm`) directly instead of waking the machine. The web role
                  gains `lambda:InvokeFunction` on that one function and still cannot send mail itself.
Evidence:         Same incident, after: `{"status": "CANCELLED", ..., "cancelled_at": "6:20 pm"}`; the row's
                  `broadcast` = `{"kind": "false_alarm", "told": ["vaishali", "anil", "ravi"]}`; Ravi's claim link
                  now renders "Sunita cancelled this alert" with no record. `./verify.sh` check 11, 11/11
                  (`v-182122-*`).
