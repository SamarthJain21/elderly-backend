#!/usr/bin/env bash
# Deploy the API to Cloud Run (asia-southeast1). Safe to re-run.
# Prereqs: gcloud logged in, project on Blaze.
# Gemini runs on Vertex AI with the service's own identity: no API key, and prompts aren't used for training.
set -euo pipefail

PROJECT=elderly-3e18c
REGION=asia-southeast1
SERVICE=elderly-api
SA="${SERVICE}@${PROJECT}.iam.gserviceaccount.com"        # runs the API
BUILD_SA="elderly-build@${PROJECT}.iam.gserviceaccount.com"  # builds the image (new projects' default can't)
ORIGINS='["https://elderly-3e18c.web.app","https://elderly-3e18c.firebaseapp.com","http://localhost:5173"]'

gcloud config set project "$PROJECT" >/dev/null

# One-time setup (idempotent): APIs and a dedicated, least-privilege service account.
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com identitytoolkit.googleapis.com --quiet
if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SERVICE" --display-name "Elderly API (Cloud Run)"
  sleep 15  # new service accounts take a moment before IAM can see them
fi
for role in roles/datastore.user roles/firebaseauth.admin roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" \
    --condition=None --quiet >/dev/null
done
if ! gcloud iam service-accounts describe "$BUILD_SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create elderly-build --display-name "Elderly API build (Cloud Build)"
  sleep 15  # new service accounts take a moment before IAM can see them
fi
gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$BUILD_SA" --role roles/run.builder \
  --condition=None --quiet >/dev/null

# --max-instances 1: live chat pushes are held in one process (see FOUND-ISSUES).
# --cpu-boost: extra CPU while starting → faster cold starts. MIN_INSTANCES=1 removes cold starts entirely
#   (~6 s on the first request after idle) at the cost of one always-on instance — worth it for the demo.
# --timeout 3600 + session affinity: long-lived WebSockets stay on that instance.
# --allow-unauthenticated: the app checks Firebase ID tokens itself on every request.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --build-service-account "projects/${PROJECT}/serviceAccounts/${BUILD_SA}" \
  --allow-unauthenticated \
  --min-instances "${MIN_INSTANCES:-0}" --max-instances 1 --cpu-boost \
  --timeout 3600 --session-affinity \
  --memory 512Mi --cpu 1 \
  --set-env-vars "^@^CORS_ORIGINS=${ORIGINS}@VERTEX_LOCATION=global" \
  --quiet

gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)'
