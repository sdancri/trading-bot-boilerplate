#!/usr/bin/env bash
# ============================================================
# push_github.sh
# ------------------------------------------------------------
# Inițializează repo-ul Git (dacă nu există) și face push pe
# GitHub pe contul `sdancri` (email `lgadresa2000@gmail.com`).
#
# Usage:
#   ./scripts/push_github.sh                 # push pe main
#   ./scripts/push_github.sh "commit msg"    # cu mesaj custom
#
# Prima rulare: creezi repo-ul pe github.com ca `trading-bot-boilerplate`
# (PUBLIC sau PRIVATE — tu decizi), apoi rulezi scriptul.
# ============================================================
set -euo pipefail

GITHUB_USER="sdancri"
GITHUB_EMAIL="lgadresa2000@gmail.com"
REPO_NAME="trading-bot-boilerplate"
BRANCH="main"
COMMIT_MSG="${1:-update: $(date -u +%Y-%m-%d\ %H:%M\ UTC)}"

cd "$(dirname "$0")/.."

# --- 1. Inițializare repo (dacă nu există) ---
if [ ! -d .git ]; then
    echo ">> git init"
    git init -b "${BRANCH}"
fi

# --- 2. Config user local (doar pt acest repo, nu global) ---
git config user.name  "${GITHUB_USER}"
git config user.email "${GITHUB_EMAIL}"

# --- 3. Remote (adaugă sau update) ---
REMOTE_URL="https://github.com/${GITHUB_USER}/${REPO_NAME}.git"
if git remote get-url origin &>/dev/null; then
    echo ">> git remote set-url origin ${REMOTE_URL}"
    git remote set-url origin "${REMOTE_URL}"
else
    echo ">> git remote add origin ${REMOTE_URL}"
    git remote add origin "${REMOTE_URL}"
fi

# --- 4. Add + commit + push ---
git add -A
# Commit doar dacă sunt modificări
if ! git diff --cached --quiet; then
    git commit -m "${COMMIT_MSG}"
else
    echo ">> no changes to commit"
fi

# Push — prima oară forțează up-stream
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' &>/dev/null; then
    git push
else
    echo ">> first push — setting upstream"
    git push -u origin "${BRANCH}"
fi

echo ""
echo "✅ Pushed to https://github.com/${GITHUB_USER}/${REPO_NAME}"
echo ""
echo "Next steps:"
echo "  1. Add DOCKERHUB_USERNAME & DOCKERHUB_TOKEN ca secrets"
echo "     în GitHub repo settings → Secrets and variables → Actions"
echo "  2. Următorul push va trigger-ui CI/CD-ul (.github/workflows/docker-publish.yml)"
echo "     care va face build + push automat pe DockerHub."
echo ""
