#!/usr/bin/env bash
# Runs the twelve live checks in verify.py against the deployed stack, starting from
# the seeded circle and histories and leaving them so: the ranking learns from every
# page, including the ones these checks send, so each run begins from the same known
# history and the live button is not left ranking on test pages.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE="${AWS_PROFILE:-hackathon}"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
./seed.sh > /dev/null
rc=0
.venv/bin/python verify.py "$@" || rc=$?
./seed.sh > /dev/null
exit "$rc"
