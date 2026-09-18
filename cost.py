#!/usr/bin/env python3
"""What one incident costs, and what a subject costs while nothing happens.

Prices are ap-south-1 (Mumbai) list prices read from the AWS Price List API on
17 Sep 2026 (`aws pricing get-products --region us-east-1 --service-code <code>
--filters Type=TERM_MATCH,Field=location,Value="Asia Pacific (Mumbai)"`), free
tiers ignored so the numbers hold at scale. Counts are read off real executions of
this stack, named below. Run it: `python cost.py`. The README's table is its output,
and tests/test_cost_numbers.py fails if the two drift apart.
"""

USD_PER_TRANSITION = 0.0000285      # AmazonStates  APS3-StateTransition
USD_PER_GB_SECOND = 0.0000133334    # AWSLambda     APS3-Lambda-GB-Second-ARM, tier 1
USD_PER_REQUEST = 0.0000002         # AWSLambda     APS3-Request
USD_PER_WRITE_UNIT = 0.00000071     # AmazonDynamoDB APS3-WriteRequestUnits ($0.71 per million)
USD_PER_READ_UNIT = 0.0000001425    # AmazonDynamoDB APS3-ReadRequestUnits ($0.1425 per million)
USD_PER_EMAIL = 0.00015             # AmazonSES     APS3-Message
USD_PER_KMS_REQUEST = 0.000003      # awskms        ap-south-1-KMS-Requests ($0.03 per 10,000)
USD_PER_KMS_KEY_MONTH = 1.0         # awskms        ap-south-1-KMS-Keys
USD_PER_GB_MONTH_DDB = 0.285        # AmazonDynamoDB APS3-TimedStorage-ByteHrs
USD_PER_GB_LOGS = 0.67              # AmazonCloudWatch APS3-DataProcessing-Bytes
INR_PER_USD = 88                    # assumed, for the rupee column only

LAMBDA_GB = 0.125                   # 128 MB, every function
MEAN_BILLED_S = 0.5                 # CloudWatch REPORT lines, 17 Sep: 130-785 ms across functions

# From the live stack, 17 Sep 2026. Transitions and machine invocations are counted
# from the execution history and emails are the notification rows written; page
# polls and DynamoDB request units are estimates, and they are the smallest lines.
SHAPES = {
    "Ravi claims at the first circle": dict(
        execution="v-182122-claim", transitions=13, machine_invocations=7, emails=6,
        web_invocations=20,  # 1 press, ~15 status polls at 3 s until the claim, claim page + press, 2 refreshes
        kms_requests=2, writes=25, reads=45),
    "Nobody claims: three circles, then the fallback": dict(
        execution="cost-182725", transitions=38, machine_invocations=20, emails=18,
        web_invocations=45,  # 1 press, ~40 polls over two minutes, a few opened links
        kms_requests=0, writes=60, reads=120),
}


def incident(s):
    invocations = s["machine_invocations"] + s["web_invocations"]
    return {
        "Step Functions": s["transitions"] * USD_PER_TRANSITION,
        "Lambda": invocations * (MEAN_BILLED_S * LAMBDA_GB * USD_PER_GB_SECOND + USD_PER_REQUEST),
        "DynamoDB": s["writes"] * USD_PER_WRITE_UNIT + s["reads"] * USD_PER_READ_UNIT,
        "SES": s["emails"] * USD_PER_EMAIL,
        "KMS": s["kms_requests"] * USD_PER_KMS_REQUEST,
    }


def idle_per_subject_month():
    # A few KB of rows, a few MB of logs a month; nothing else runs while nothing happens.
    return 0.001 * USD_PER_GB_MONTH_DDB + 0.005 * USD_PER_GB_LOGS


def checkin_per_subject_month(people=6):
    # The weekly check-in: one invocation, one email per person, two row writes and one
    # counter each. 52 weeks over 12 months. Telegram is free.
    per_week = (MEAN_BILLED_S * LAMBDA_GB * USD_PER_GB_SECOND + USD_PER_REQUEST) \
        + people * (USD_PER_EMAIL + 3 * USD_PER_WRITE_UNIT)
    return per_week * 52 / 12


def fmt(usd):
    return f"${usd:.5f}"


def main():
    print("| Incident | Step Functions | Lambda | DynamoDB | SES | KMS | Total |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for name, s in SHAPES.items():
        c = incident(s)
        total = sum(c.values())
        cells = " | ".join(fmt(c[k]) for k in ("Step Functions", "Lambda", "DynamoDB", "SES", "KMS"))
        print(f"| {name} ({s['transitions']} transitions, {s['emails']} emails) | {cells} | "
              f"**{fmt(total)}** (₹{total * INR_PER_USD:.2f}) |")
    print()
    print(f"Idle, per subject per month: {fmt(idle_per_subject_month())}. "
          f"Fixed, whole system: one KMS key, ${USD_PER_KMS_KEY_MONTH:.2f}/month.")
    print(f"The weekly check-in to her six people: {fmt(checkin_per_subject_month())} per subject per month "
          f"(26 emails).")
    first = next(iter(SHAPES.values()))
    thousand = 1000 * (sum(incident(first).values()) + idle_per_subject_month()) + USD_PER_KMS_KEY_MONTH
    with_checkin = thousand + 1000 * checkin_per_subject_month()
    print(f"A thousand people, one incident each a month: about ${thousand:.0f}/month "
          f"(₹{thousand * INR_PER_USD:.0f}); with the weekly check-in, about ${with_checkin:.0f}/month "
          f"(₹{with_checkin * INR_PER_USD:.0f}).")


if __name__ == "__main__":
    main()
