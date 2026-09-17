#!/usr/bin/env bash
# Runs the six live checks in verify.py against the deployed stack.
set -euo pipefail
cd "$(dirname "$0")"
export AWS_PROFILE="${AWS_PROFILE:-hackathon}"
export AWS_REGION="${AWS_REGION:-ap-south-1}"
exec .venv/bin/python verify.py "$@"
