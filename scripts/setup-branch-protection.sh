#!/usr/bin/env bash
#
# Protect the main branch.
#
#   bash scripts/setup-branch-protection.sh
#
# Invoke with `bash` rather than relying on the executable bit -- the bit does
# not survive a commit from Windows, and this repository is cross-platform.
#
# PLAN REQUIREMENT
#
# Branch protection is free on PUBLIC repositories. On PRIVATE repositories it
# requires GitHub Pro, Team, or Enterprise. If this script fails with a 403
# mentioning upgrade, that is why -- either make the repository public or
# upgrade the plan.
#
# WHY AUTOMATE THIS AT ALL
#
# Because "we all agreed not to push to main" fails under deadline pressure,
# and the last week of a two-week project is nothing but deadline pressure.
# Rules that live in a person's memory are suggestions. Rules that live in the
# platform are rules.
#
# It also means the configuration is reviewable, diffable, and reproducible on
# the next repository, instead of being twenty clicks somebody has to remember.

set -euo pipefail

OWNER="${OWNER:-mangohero8}"
REPO="${REPO:-cyber-inventory}"
BRANCH="${BRANCH:-main}"

# These strings must match the `name:` of each job in ci.yml exactly. If a job
# is renamed and this is not updated, protection silently stops requiring it --
# the rule waits for a check that will never report, or ignores it entirely
# depending on configuration. Worth re-checking whenever CI changes.
CHECK_QUALITY="Lint and test"
CHECK_CONTAINER="Build and scan image"

echo "==> Protecting ${OWNER}/${REPO}@${BRANCH}"

# Confirm the checks we are about to require have actually reported at least
# once. Requiring a check that has never run blocks every PR forever, with a
# "Expected — waiting for status to be reported" that never resolves. This is
# a genuinely common way to lock a repository.
echo "==> Verifying those check names exist"
if ! gh api "repos/${OWNER}/${REPO}/commits/${BRANCH}/check-runs" \
     --jq '.check_runs[].name' 2>/dev/null | sort -u | tee /tmp/checks.txt \
     | grep -qx "${CHECK_QUALITY}"; then
  echo
  echo "  WARNING: '${CHECK_QUALITY}' has not reported on ${BRANCH}."
  echo "  Checks seen:"
  sed 's/^/    /' /tmp/checks.txt 2>/dev/null || echo "    (none)"
  echo
  echo "  Requiring a check that never reports blocks every PR permanently."
  echo "  Push a commit so CI runs on ${BRANCH}, then re-run this script."
  exit 1
fi
echo "    both checks confirmed"

# --------------------------------------------------------------------------
# Apply protection
# --------------------------------------------------------------------------
#
# The settings, and why each one is here:
#
# required_status_checks.strict = true
#     A PR must be up to date with main before merging. Prevents the case
#     where two PRs each pass on their own but break main when combined --
#     each was tested against a main that no longer exists.
#
# required_approving_review_count = 1
#     Someone other than the author looks at it. The core of the peer review
#     learning goal.
#
# dismiss_stale_reviews = true
#     New commits invalidate prior approvals. Otherwise "approve early, push
#     anything after" is an open door, accidental or not.
#
# require_code_owner_reviews = true
#     Makes CODEOWNERS binding rather than advisory.
#
# enforce_admins = false
#     Deliberate, and worth a conversation. `true` means even repository
#     admins cannot bypass -- philosophically correct, and it will eventually
#     strand you at 2am with a one-line fix and no way to merge it. `false`
#     keeps a break-glass path. Whichever you choose, decide it on purpose:
#     an admin who bypasses silently is a much worse outcome than one who
#     bypasses visibly.
#
# allow_force_pushes / allow_deletions = false
#     Force-push to a shared branch destroys other people's commits. This is
#     the single most damaging thing someone can do to a repository, and it
#     is usually an accident following a confusing push rejection.
#
# required_conversation_resolution = true
#     Review comments must be resolved before merge, so feedback cannot be
#     merged past silently.
#
# required_linear_history = false
#     We use merge commits (see CONTRIBUTING.md). Set true if you squash.

echo "==> Applying protection rules"
gh api -X PUT "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" \
  --input - <<EOF
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["${CHECK_QUALITY}", "${CHECK_CONTAINER}"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": true,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true,
  "required_linear_history": false
}
EOF

echo
echo "==> Current protection:"
gh api "repos/${OWNER}/${REPO}/branches/${BRANCH}/protection" --jq '{
  required_checks: .required_status_checks.contexts,
  strict: .required_status_checks.strict,
  reviews: .required_pull_request_reviews.required_approving_review_count,
  dismiss_stale: .required_pull_request_reviews.dismiss_stale_reviews,
  code_owners: .required_pull_request_reviews.require_code_owner_reviews,
  force_pushes: .allow_force_pushes.enabled,
  conversation_resolution: .required_conversation_resolution.enabled
}'

cat <<'EOF'

============================================================================
PROTECTION ENABLED

Verify it works by trying to violate it:

  git checkout main
  echo "# direct push test" >> README.md
  git commit -am "test: this should be rejected"
  git push          # <-- expect rejection

  git reset --hard origin/main    # undo the local commit

A rule you have never seen fire is a rule you are only assuming exists.
============================================================================

EOF
