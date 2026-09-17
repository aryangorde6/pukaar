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
