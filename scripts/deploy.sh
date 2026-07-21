#!/usr/bin/env bash
#
# Deploy the Document-Chase Agent infrastructure.
#
#   ./scripts/deploy.sh            # defaults to dev
#   ./scripts/deploy.sh dev
#   AWS_PROFILE=my-profile ./scripts/deploy.sh dev
#
# Bootstraps the account on first run, then deploys every stack for the stage.

set -euo pipefail

STAGE="${1:-dev}"
REGION="us-east-1"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"

# AWS_PROFILE is honoured by both the AWS CLI and the CDK CLI via the
# environment, but pass it explicitly so the log makes the target unambiguous.
PROFILE_ARGS=()
if [[ -n "${AWS_PROFILE:-}" ]]; then
  PROFILE_ARGS=(--profile "${AWS_PROFILE}")
  echo "==> Using AWS profile: ${AWS_PROFILE}"
else
  echo "==> Using default AWS credentials (no AWS_PROFILE set)"
fi

cd "${INFRA_DIR}"

if [[ ! -d node_modules ]]; then
  echo "==> Installing infra dependencies"
  npm install
fi

echo "==> Verifying credentials"
ACCOUNT_ID="$(aws sts get-caller-identity "${PROFILE_ARGS[@]}" --query Account --output text)"
echo "    Account: ${ACCOUNT_ID}   Region: ${REGION}   Stage: ${STAGE}"

# CDK v2 bootstrap creates a stack named CDKToolkit. If it is not there, this is
# a first run and the account needs bootstrapping before anything can deploy.
echo "==> Checking CDK bootstrap"
if aws cloudformation describe-stacks \
      --stack-name CDKToolkit \
      --region "${REGION}" \
      "${PROFILE_ARGS[@]}" \
      >/dev/null 2>&1; then
  echo "    Already bootstrapped."
else
  echo "    Not bootstrapped. Running cdk bootstrap..."
  npx cdk bootstrap "aws://${ACCOUNT_ID}/${REGION}" "${PROFILE_ARGS[@]}"
fi

echo "==> Synthesizing"
npx cdk synth -c "stage=${STAGE}" "${PROFILE_ARGS[@]}" --quiet

# Default to prompting on any IAM/security change -- this script is meant to be
# run by a human at a terminal. Override for CI or a non-interactive session:
#   CDK_REQUIRE_APPROVAL=never ./scripts/deploy.sh dev
REQUIRE_APPROVAL="${CDK_REQUIRE_APPROVAL:-any-change}"

echo "==> Deploying all stacks for stage '${STAGE}' (--require-approval ${REQUIRE_APPROVAL})"
npx cdk deploy --all -c "stage=${STAGE}" "${PROFILE_ARGS[@]}" --require-approval "${REQUIRE_APPROVAL}"

echo ""
echo "==> Done. Deployed stacks:"
echo "    Ida-$(tr '[:lower:]' '[:upper:]' <<< "${STAGE:0:1}")${STAGE:1}-{Shared,State,Ingestion,Understanding,Agent}"
