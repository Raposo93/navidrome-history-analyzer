#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

echo "Checking required tools..."

if ! command -v git >/dev/null 2>&1; then
  echo "Error: 'git' is not installed."
  exit 1
fi

PROJECT_PYTHON="python3"
if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  PROJECT_PYTHON="$PROJECT_ROOT/.venv/bin/python"
elif ! command -v "$PROJECT_PYTHON" >/dev/null 2>&1; then
  echo "Error: 'python3' is not installed."
  exit 1
fi

echo
echo "Running Git checks..."

git --no-pager diff --check
bash -n check.sh

if git --no-pager grep -nE '^(<<<<<<< .+|=======|>>>>>>> .+)$'; then
  echo "Error: unresolved merge conflict markers found."
  exit 1
fi

echo
echo "Running tests..."

PYTHONDONTWRITEBYTECODE=1 "$PROJECT_PYTHON" -m unittest discover -v

echo
echo "Running static checks..."

"$PROJECT_PYTHON" -m ruff check .
"$PROJECT_PYTHON" -m ruff format .
"$PROJECT_PYTHON" -m pyright

echo
echo "Checking CLI startup..."

PYTHONDONTWRITEBYTECODE=1 "$PROJECT_PYTHON" navidrome_history_report.py --help >/dev/null

echo
echo "All checks passed."
