#!/usr/bin/env bash
# Runs the eight live checks in verify.py against the deployed stack, starting from
# the seeded circle and histories: the ranking learns from every page, including the
# ones these checks send, so each run begins from the same known history.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE="${AWS_PROFILE:-hackathon}"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
./seed.sh > /dev/null
exec .venv/bin/python verify.py "$@"
