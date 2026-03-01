#!/usr/bin/env zsh
#
# Publish a clean snapshot of Infracontext to GitHub.
#
# Default (append): clones the public repo and commits changes on top,
# preserving public git history for changelogs and diffs.
#
# --force: replaces the entire public repo with a single squashed commit.
# --dry-run: export and scan but do not push.
#
# Usage: ./scripts/publish-github.sh [--dry-run] [--force]

set -euo pipefail

GITHUB_REMOTE="https://github.com/sysinit-at/infracontext.git"
REPO_ROOT="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
EXPORT_DIR=$(mktemp -d)
SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD)
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)

DRY_RUN=false
FORCE=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --force)   FORCE=true ;;
        *)         echo "Unknown option: $arg"; exit 1 ;;
    esac
done

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
for pattern in "gitlab.sysinit" "ssh -l root" "wke/" ".tmp.directory"; do
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

# ── 4. Prepare git repo and push ──
COMMIT_MSG="Update from $SOURCE_SHA ($TIMESTAMP)"

if $FORCE; then
    echo "==> Mode: force (single-commit snapshot)"
    cd "$EXPORT_DIR"
    git init -b main
    git add -A
    git commit -m "Infracontext — infrastructure context for humans and agents

Source: $SOURCE_SHA ($TIMESTAMP)"
else
    echo "==> Mode: append (incremental commit)"
    CLONE_DIR=$(mktemp -d)
    git clone --depth=1 "$GITHUB_REMOTE" "$CLONE_DIR" 2>/dev/null || {
        echo "  Remote is empty or unreachable, falling back to force mode."
        FORCE=true
        cd "$EXPORT_DIR"
        git init -b main
        git add -A
        git commit -m "Infracontext — infrastructure context for humans and agents

Source: $SOURCE_SHA ($TIMESTAMP)"
        rm -rf "$CLONE_DIR"
    }

    if ! $FORCE; then
        # Replace working tree contents with our clean export
        # Remove all existing files (except .git)
        find "$CLONE_DIR" -mindepth 1 -maxdepth 1 -not -name .git -exec rm -rf {} +
        # Copy clean export in
        cp -a "$EXPORT_DIR"/. "$CLONE_DIR"/
        cd "$CLONE_DIR"
        git add -A

        if git diff --cached --quiet; then
            echo "==> No changes to publish."
            exit 0
        fi

        git commit -m "$COMMIT_MSG"
        # Swap EXPORT_DIR to CLONE_DIR for dry-run output
        EXPORT_DIR="$CLONE_DIR"
    fi
fi

if $DRY_RUN; then
    echo "==> Dry run complete. Export at: $(pwd)"
    echo "    Files: $(find . -type f -not -path './.git/*' | wc -l | tr -d ' ')"
    echo "    Size:  $(du -sh . | cut -f1)"
    git log --oneline -5
    trap - EXIT
else
    if $FORCE; then
        git remote add origin "$GITHUB_REMOTE"
        git push --force origin main
    else
        git push origin main
    fi
    echo "==> Published to $GITHUB_REMOTE"
fi
