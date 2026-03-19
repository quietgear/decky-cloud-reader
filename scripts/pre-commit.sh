#!/bin/sh
# =============================================================================
# Pre-commit hook — runs Python and TypeScript linting inside Docker.
# =============================================================================
#
# Installation (one-time):
#   cp scripts/pre-commit.sh .git/hooks/pre-commit
#   chmod +x .git/hooks/pre-commit
#
# This hook blocks commits that fail linting. Use `git commit --no-verify`
# to skip if absolutely necessary (not recommended).

set -e

echo "Pre-commit: running Python lint..."
docker compose -f docker/docker-compose.dev.yml run --rm lint-python

echo "Pre-commit: running TypeScript lint..."
docker compose -f docker/docker-compose.dev.yml run --rm lint-ts

echo "Pre-commit: all checks passed!"
