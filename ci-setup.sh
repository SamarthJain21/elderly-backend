#!/usr/bin/env bash
# One-time CI/CD setup: a deploy-only service account and push-to-main triggers for both repos.
# Prereq: the GitHub connection "elderly-github" exists and both repos are linked (see README / Claude notes).
set -euo pipefail
PROJECT=elderly-3e18c
REGION=asia-southeast1
CI_SA="elderly-ci@${PROJECT}.iam.gserviceaccount.com"
API_SA="elderly-api@${PROJECT}.iam.gserviceaccount.com"
gcloud config set project "$PROJECT" >/dev/null

if ! gcloud iam service-accounts describe "$CI_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create elderly-ci --display-name "Elderly CI/CD (Cloud Build triggers)"
  sleep 15
fi
# Least privilege: build, push images, deploy Cloud Run + Hosting, write logs.
for role in roles/run.developer roles/artifactregistry.writer roles/logging.logWriter \
            roles/firebasehosting.admin roles/serviceusage.serviceUsageViewer roles/storage.objectViewer; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$CI_SA" --role "$role" \
    --condition=None --quiet >/dev/null
done
# Deploying Cloud Run "as" the API's identity requires actAs on that one account only.
gcloud iam service-accounts add-iam-policy-binding "$API_SA" --member "serviceAccount:$CI_SA" \
  --role roles/iam.serviceAccountUser --quiet >/dev/null

for repo in elderly-backend elderly-frontend; do
  gcloud builds triggers describe "deploy-${repo}" --region "$REGION" >/dev/null 2>&1 && continue
  gcloud builds triggers create github \
    --name "deploy-${repo}" --region "$REGION" \
    --repository "projects/${PROJECT}/locations/${REGION}/connections/elderly-github/repositories/${repo}" \
    --branch-pattern '^main$' --build-config cloudbuild.yaml \
    --service-account "projects/${PROJECT}/serviceAccounts/${CI_SA}"
done
gcloud builds triggers list --region "$REGION" --format 'table(name,repositoryEventConfig.push.branch,serviceAccount)'
