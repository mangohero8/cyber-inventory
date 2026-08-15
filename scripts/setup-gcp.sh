#!/usr/bin/env bash
#
# One-time Google Cloud setup for deploying this service from GitHub Actions.
#
# Run this ONCE, from your laptop, with gcloud authenticated as yourself.
# It is idempotent -- safe to re-run if a step fails partway through.
#
#   ./scripts/setup-gcp.sh
#
# WHAT THIS CREATES
#   1. Enables the APIs the deploy needs
#   2. An Artifact Registry repository to hold container images
#   3. A service account that GitHub Actions will act as
#   4. A Workload Identity Federation pool + provider, so GitHub can
#      authenticate WITHOUT a downloadable key
#
# WHY WORKLOAD IDENTITY FEDERATION INSTEAD OF A KEY FILE
#
# The obvious way to let GitHub deploy to GCP is to create a service account,
# download its JSON key, and paste it into a GitHub secret. Almost every
# tutorial does this. Don't.
#
# That key is a long-lived credential -- it does not expire. Anyone who
# obtains it can act as that service account indefinitely, from anywhere. It
# sits in GitHub, in your shell history, in whatever you pasted it through,
# and in a backup somewhere. Leaked service account keys are one of the most
# common causes of cloud compromise, and they are hard to detect because the
# access looks legitimate.
#
# Workload Identity Federation removes the key entirely. GitHub Actions gets a
# short-lived OIDC token from GitHub that says "I am the repository
# mangohero8/cyber-inventory, running on branch main". Google validates that
# token against a trust policy you define here and, if it matches, mints an
# access token that lives for minutes.
#
# Nothing to leak, nothing to rotate, and access is scoped to a specific
# repository rather than to whoever holds a file.

set -euo pipefail

# --------------------------------------------------------------------------
# CONFIGURATION -- edit these
# --------------------------------------------------------------------------
PROJECT_ID="${PROJECT_ID:-cyber-inventory-$RANDOM}"
REGION="${REGION:-us-central1}"
GITHUB_OWNER="${GITHUB_OWNER:-mangohero8}"
GITHUB_REPO="${GITHUB_REPO:-cyber-inventory}"

SERVICE_NAME="cyber-inventory"
REPO_NAME="containers"            # Artifact Registry repository
POOL_NAME="github-pool"
PROVIDER_NAME="github-provider"
DEPLOYER_SA="github-deployer"     # identity GitHub Actions acts as
RUNTIME_SA="cyber-inventory-run"  # identity the service itself runs as

# --------------------------------------------------------------------------
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$1"; }

say "Using project: ${PROJECT_ID}  region: ${REGION}"
say "Trusting GitHub repo: ${GITHUB_OWNER}/${GITHUB_REPO}"

# --------------------------------------------------------------------------
# 1. Project
# --------------------------------------------------------------------------
if ! gcloud projects describe "${PROJECT_ID}" >/dev/null 2>&1; then
  say "Creating project ${PROJECT_ID}"
  gcloud projects create "${PROJECT_ID}" --name="Cyber Inventory"
else
  say "Project ${PROJECT_ID} already exists"
fi

gcloud config set project "${PROJECT_ID}" >/dev/null

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
say "Project number: ${PROJECT_NUMBER}"

# Billing must be enabled or the API enables below will fail. This is the
# single most common place this script stops.
if ! gcloud beta billing projects describe "${PROJECT_ID}" \
      --format='value(billingEnabled)' 2>/dev/null | grep -q True; then
  cat <<EOF

  ⚠️  BILLING IS NOT ENABLED on ${PROJECT_ID}.

  Cloud Run scales to zero, so a demo service costs approximately nothing --
  but Google still requires a billing account to be linked.

  Link one here, then re-run this script:
    https://console.cloud.google.com/billing/linkedaccount?project=${PROJECT_ID}

EOF
  exit 1
fi

# --------------------------------------------------------------------------
# 2. APIs
# --------------------------------------------------------------------------
say "Enabling APIs (takes a minute)"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  iamcredentials.googleapis.com \
  sts.googleapis.com \
  cloudresourcemanager.googleapis.com

# --------------------------------------------------------------------------
# 3. Artifact Registry
# --------------------------------------------------------------------------
if ! gcloud artifacts repositories describe "${REPO_NAME}" \
      --location="${REGION}" >/dev/null 2>&1; then
  say "Creating Artifact Registry repository"
  gcloud artifacts repositories create "${REPO_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Container images for ${SERVICE_NAME}"
else
  say "Artifact Registry repository already exists"
fi

# --------------------------------------------------------------------------
# 4. Service accounts
# --------------------------------------------------------------------------
# TWO service accounts, deliberately.
#
#   deployer  -- what GitHub Actions acts as. Can push images and deploy.
#   runtime   -- what the running service acts as. Can do almost nothing.
#
# Cloud Run defaults to the Compute Engine default service account, which
# holds project Editor -- meaning a compromised web request would run with
# broad write access to your whole project. A dedicated runtime identity with
# no roles is a one-line fix for a genuinely large blast radius.
for SA in "${DEPLOYER_SA}" "${RUNTIME_SA}"; do
  if ! gcloud iam service-accounts describe \
        "${SA}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    say "Creating service account ${SA}"
    gcloud iam service-accounts create "${SA}" --display-name="${SA}"
  else
    say "Service account ${SA} already exists"
  fi
done

DEPLOYER_EMAIL="${DEPLOYER_SA}@${PROJECT_ID}.iam.gserviceaccount.com"
RUNTIME_EMAIL="${RUNTIME_SA}@${PROJECT_ID}.iam.gserviceaccount.com"

say "Granting deploy permissions"
# Least privilege: exactly what a deploy needs and nothing more.
#   run.admin              -- create and update Cloud Run services
#   artifactregistry.writer-- push images
#   iam.serviceAccountUser -- attach the runtime SA to the service
for ROLE in roles/run.admin roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${DEPLOYER_EMAIL}" \
    --role="${ROLE}" \
    --condition=None >/dev/null
done

# Scoped to the runtime SA specifically, NOT project-wide. Project-wide
# serviceAccountUser would let the deployer impersonate every service account
# in the project, including more privileged ones.
gcloud iam service-accounts add-iam-policy-binding "${RUNTIME_EMAIL}" \
  --member="serviceAccount:${DEPLOYER_EMAIL}" \
  --role="roles/iam.serviceAccountUser" >/dev/null

# --------------------------------------------------------------------------
# 5. Workload Identity Federation
# --------------------------------------------------------------------------
if ! gcloud iam workload-identity-pools describe "${POOL_NAME}" \
      --location=global >/dev/null 2>&1; then
  say "Creating Workload Identity Pool"
  gcloud iam workload-identity-pools create "${POOL_NAME}" \
    --location=global \
    --display-name="GitHub Actions"
else
  say "Workload Identity Pool already exists"
fi

if ! gcloud iam workload-identity-pools providers describe "${PROVIDER_NAME}" \
      --location=global --workload-identity-pool="${POOL_NAME}" >/dev/null 2>&1; then
  say "Creating OIDC provider"
  # THE ATTRIBUTE CONDITION IS THE SECURITY BOUNDARY.
  #
  # Without it, the trust policy says "any token issued by GitHub Actions" --
  # and GitHub Actions issues tokens to every repository on GitHub, including
  # one an attacker creates in thirty seconds. This condition restricts the
  # trust to repositories owned by you. Google now requires a condition for
  # exactly this reason; it was a widespread real-world misconfiguration.
  gcloud iam workload-identity-pools providers create-oidc "${PROVIDER_NAME}" \
    --location=global \
    --workload-identity-pool="${POOL_NAME}" \
    --display-name="GitHub OIDC" \
    --issuer-uri="https://token.actions.githubusercontent.com" \
    --attribute-mapping="google.subject=assertion.sub,attribute.actor=assertion.actor,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
    --attribute-condition="assertion.repository_owner == '${GITHUB_OWNER}'"
else
  say "OIDC provider already exists"
fi

say "Allowing ${GITHUB_OWNER}/${GITHUB_REPO} to impersonate ${DEPLOYER_SA}"
# principalSet scoped to ONE repository. Even though the provider trusts your
# whole GitHub account, only this repo can use this service account.
gcloud iam service-accounts add-iam-policy-binding "${DEPLOYER_EMAIL}" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/attribute.repository/${GITHUB_OWNER}/${GITHUB_REPO}" \
  >/dev/null

WIF_PROVIDER="projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/${POOL_NAME}/providers/${PROVIDER_NAME}"

# --------------------------------------------------------------------------
# Done
# --------------------------------------------------------------------------
cat <<EOF

============================================================================
SETUP COMPLETE

Set these as GitHub repository VARIABLES (not secrets -- none of them are
secret, and variables are visible in logs which makes debugging far easier):

  gh variable set GCP_PROJECT_ID     --body "${PROJECT_ID}"
  gh variable set GCP_REGION         --body "${REGION}"
  gh variable set GCP_WIF_PROVIDER   --body "${WIF_PROVIDER}"
  gh variable set GCP_DEPLOYER_SA    --body "${DEPLOYER_EMAIL}"
  gh variable set GCP_RUNTIME_SA     --body "${RUNTIME_EMAIL}"
  gh variable set GCP_AR_REPO        --body "${REPO_NAME}"

Note there is no key file anywhere in that list. That is the point.
============================================================================

EOF
