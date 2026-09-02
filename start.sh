#!/usr/bin/env bash
# VIO test platform launcher. Usage: bash start.sh   (PORT=1234 default)
# Delegates to run.sh, which auto-checks/installs deps and guarantees the
# results/ + state/ dirs, then starts the web service.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
exec bash "$DIR/run.sh" "$@"
