#!/usr/bin/env bash
set -Eeuo pipefail
trap 'status=$?; printf >&2 "\nWorkshop setup failed (exit %s, line %s).\n" "$status" "$LINENO"; exit "$status"' ERR

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

printf '\nInstalling the pinned Python dependencies...\n'
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

printf '\nInstalling the pinned Node dependencies...\n'
npm ci --no-audit --no-fund

printf '\nInstalling matching Chromium and its Linux system libraries...\n'
# Playwright uses the dev container user's normal sudo access for OS packages.
npm run browser:install -- --with-deps

printf '\nPrefetching the Microsoft Copilot SDK runtime (no sign-in or model request)...\n'
.venv/bin/python -m copilot download-runtime --in-process

printf '\nWorkshop setup complete. Sign in with your own account:\n'
printf '  env -u GITHUB_TOKEN -u GH_TOKEN gh auth login --web --git-protocol https\n'
printf 'Then start the local fixture with: npm run demo\n'
