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
