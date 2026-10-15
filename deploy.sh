#!/usr/bin/env bash
# Deploy the API to Cloud Run (asia-southeast1). Safe to re-run.
# Prereqs: gcloud logged in, project on Blaze.
# Gemini runs on Vertex AI with the service's own identity: no API key, and prompts aren't used for training.
set -euo pipefail

PROJECT=elderly-3e18c
REGION=asia-southeast1
SERVICE=elderly-api
SA="${SERVICE}@${PROJECT}.iam.gserviceaccount.com"
ORIGINS='["https://elderly-3e18c.web.app","https://elderly-3e18c.firebaseapp.com","http://localhost:5173"]'

gcloud config set project "$PROJECT" >/dev/null

# One-time setup (idempotent): APIs and a dedicated, least-privilege service account.
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com identitytoolkit.googleapis.com --quiet
if ! gcloud iam service-accounts describe "$SA" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SERVICE" --display-name "Elderly API (Cloud Run)"
fi
for role in roles/datastore.user roles/firebaseauth.admin roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" \
    --condition=None --quiet >/dev/null
done

# --max-instances 1: live chat pushes are held in one process (see FOUND-ISSUES).
# --timeout 3600 + session affinity: long-lived WebSockets stay on that instance.
# --allow-unauthenticated: the app checks Firebase ID tokens itself on every request.
gcloud run deploy "$SERVICE" \
  --source . \
  --region "$REGION" \
  --service-account "$SA" \
  --allow-unauthenticated \
  --min-instances 0 --max-instances 1 \
  --timeout 3600 --session-affinity \
  --memory 512Mi --cpu 1 \
  --set-env-vars "^@^CORS_ORIGINS=${ORIGINS}@VERTEX_LOCATION=global" \
  --quiet

gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)'
