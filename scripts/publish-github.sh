#!/usr/bin/env zsh
#
# Publish a clean snapshot of Infracontext to GitHub.
#
# Creates a fresh single-commit repo from the current working tree,
# excluding internal files, credentials, and git history.
#
# Usage: ./scripts/publish-github.sh [--dry-run]

set -euo pipefail

GITHUB_REMOTE="https://github.com/sysinit-at/infracontext.git"
REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
EXPORT_DIR=$(mktemp -d)
SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD)

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

cleanup() { rm -rf "$EXPORT_DIR"; }
trap cleanup EXIT

echo "==> Exporting from $REPO_ROOT ($SOURCE_SHA) to $EXPORT_DIR"

# ── 1. Copy tracked files only (respects .gitignore) ──
git -C "$REPO_ROOT" archive HEAD | tar -x -C "$EXPORT_DIR"

# ── 2. Remove internal/sensitive files ──
rm -rf "$EXPORT_DIR"/.claude
rm -f  "$EXPORT_DIR"/CLAUDE.md
rm -f  "$EXPORT_DIR"/presentation/index.html
rmdir  "$EXPORT_DIR"/presentation 2>/dev/null || true

# ── 3. Verify nothing sensitive leaked ──

# 3a. Project-specific patterns
echo "==> Scanning for project-specific sensitive patterns..."
LEAKED=false
for pattern in "sysinit.at" "gitlab.sysinit" "ssh -l root" "wke/" ".tmp.directory"; do
    if grep -r --include='*.py' --include='*.ts' --include='*.yml' \
         --include='*.yaml' --include='*.json' --include='*.md' --include='*.html' \
         -l "$pattern" "$EXPORT_DIR" 2>/dev/null; then
        echo "  !! Pattern '$pattern' found in files above"
        LEAKED=true
    fi
done
if $LEAKED; then
    echo "ABORT: sensitive content detected. Review files above."
    exit 1
fi
echo "  Clean."

# 3b. Generic secret detection via trufflehog
if command -v trufflehog &>/dev/null; then
    echo "==> Running trufflehog secret scan..."
    if ! trufflehog filesystem --no-verification --fail "$EXPORT_DIR" 2>&1; then
        echo "ABORT: trufflehog detected potential secrets."
        exit 1
    fi
    echo "  Clean."
else
    echo "  WARN: trufflehog not installed, skipping generic secret scan."
fi

# ── 4. Create fresh git repo and push ──
cd "$EXPORT_DIR"
git init -b main
git add -A
git commit -m "Infracontext — infrastructure context for humans and agents

Source: $SOURCE_SHA ($(date -u +%Y-%m-%dT%H:%M:%SZ))"

if $DRY_RUN; then
    echo "==> Dry run complete. Export at: $EXPORT_DIR"
    echo "    Files: $(find . -type f -not -path './.git/*' | wc -l | tr -d ' ')"
    echo "    Size:  $(du -sh . | cut -f1)"
    # Keep the directory for inspection
    trap - EXIT
else
    git remote add origin "$GITHUB_REMOTE"
    git push --force origin main
    echo "==> Published to $GITHUB_REMOTE"
fi
