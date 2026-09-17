#!/usr/bin/env python3
"""Seed one subject and her circle. This is the only way people get into the tables.

    python seed.py --prefix pukaar --subject sunita --name Sunita \
        --address "B-304, Shanti Sadan, Dadar West" --phone "+91 98xxx" \
        --contact "ravi|Ravi|son|ravi@example.com|1|4200|no" \
        --contact "meena|Meena|neighbour, same floor|meena@example.com|2|8|no"

Contact fields: id|name|relation|email|tier_hint|proximity_m|home_during_day(yes/no)

Addresses are arguments on purpose: the consent flow that would let anyone add an
address is not built, so nothing here can be pointed at a stranger.

--record "Blood group B+ | Allergic to penicillin" is her medical notes ("|" starts a
new line). They are encrypted here, under the stack's key with her subject_id as the
encryption context, and stored as ciphertext; nothing in the tables holds them in the
clear. Only the web function can open them, and only for whoever is going.

--history seeds response_stats so the ranking has something to rank on:
    --history "meena|14#weekday|6|0|0"       id|bucket|pages_sent|responses|total_latency_ms
The counters are otherwise written by real pages and claims. --reset-history first
deletes every stats row of the listed contacts, so a reseed starts from the seeded
histories alone and not from what test runs have counted since.
"""

import argparse
import time

import boto3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="pukaar")
    ap.add_argument("--profile", default="hackathon")
    ap.add_argument("--region", default="ap-south-1")
    ap.add_argument("--subject", required=True, help="subject_id, e.g. sunita")
    ap.add_argument("--name", required=True)
    ap.add_argument("--address", required=True)
    ap.add_argument("--phone", default="")
    ap.add_argument("--record", default="", help="medical notes, '|' between lines; sealed with KMS")
    ap.add_argument("--contact", action="append", default=[], help="id|name|relation|email|tier_hint|proximity_m|home_during_day")
    ap.add_argument("--history", action="append", default=[], help="id|bucket|pages_sent|responses|total_latency_ms")
    ap.add_argument("--reset-history", action="store_true", help="delete the listed contacts' stats rows first")
    args = ap.parse_args()

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ddb = session.client("dynamodb")
    now = str(int(time.time()))

    item = {
        "subject_id": {"S": args.subject},
        "name": {"S": args.name},
        "address": {"S": args.address},
        "phone": {"S": args.phone},
        "created_at": {"N": now},
    }
    if args.record:
        sealed = session.client("kms").encrypt(
            KeyId=f"alias/{args.prefix}-record",
            Plaintext="\n".join(part.strip() for part in args.record.split("|")).encode(),
            EncryptionContext={"subject_id": args.subject},
        )
        item["record"] = {"B": sealed["CiphertextBlob"]}
    ddb.put_item(TableName=f"{args.prefix}-subjects", Item=item)
    print(f"subject  {args.subject}: {args.name}" + (f", record sealed ({len(item['record']['B'])} bytes)" if args.record else ""))

    for raw in args.contact:
        cid, name, relation, email, tier, prox, home = [p.strip() for p in raw.split("|")]
        ddb.put_item(
            TableName=f"{args.prefix}-contacts",
            Item={
                "subject_id": {"S": args.subject},
                "contact_id": {"S": cid},
                "name": {"S": name},
                "relation": {"S": relation},
                "email": {"S": email},
                "tier_hint": {"N": tier},
                "proximity_m": {"N": prox},
                "home_during_day": {"BOOL": home.lower() == "yes"},
                "created_at": {"N": now},
            },
        )
        print(f"contact  {cid:<10} {name:<10} tier {tier}  {prox:>5} m  {email}")

    if args.reset_history:
        stats = f"{args.prefix}-response-stats"
        for raw in args.contact:
            cid = raw.split("|")[0].strip()
            rows = ddb.query(TableName=stats, KeyConditionExpression="contact_id = :c",
                             ExpressionAttributeValues={":c": {"S": cid}})["Items"]
            for r in rows:
                ddb.delete_item(TableName=stats, Key={"contact_id": r["contact_id"], "bucket": r["bucket"]})
            print(f"reset    {cid:<10} {len(rows)} stats row(s) deleted")

    for raw in args.history:
        cid, bucket, sent, responses, latency = [p.strip() for p in raw.split("|")]
        ddb.put_item(
            TableName=f"{args.prefix}-response-stats",
            Item={
                "contact_id": {"S": cid},
                "bucket": {"S": bucket},
                "pages_sent": {"N": sent},
                "responses": {"N": responses},
                "total_latency_ms": {"N": latency},
                "seeded": {"BOOL": True},
            },
        )
        print(f"history  {cid:<10} {bucket:<12} {responses}/{sent} answered")


if __name__ == "__main__":
    main()
