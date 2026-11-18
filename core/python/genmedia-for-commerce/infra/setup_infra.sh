#!/bin/bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# The Terraform steps of make setup-infra, with the TF_VAR_ variables of
# config.env exported (TF_VARS in the Makefile):
#   1. The state is in the bucket <project>-genmedia-tfstate (created when
#      missing), so a run from a new folder, or a second run, finds it.
#   2. The resources with a fixed name that exist but are not in the state
#      (made by a run whose state is lost) are imported, not created again.
#   3. The APIs of terraform/main.tf are enabled first, then the script
#      waits until Artifact Registry and IAM answer: a new project's APIs
#      refuse calls (403 SERVICE_DISABLED) for a minute or two after.
#   4. terraform plan, then "Apply these changes ...? [Y/n]" on a terminal
#      (without one, TF_AUTO_APPROVE=1 is needed), then terraform apply of
#      that plan. A fresh project's new APIs and service agents take a minute
#      to be usable: up to 3 attempts, without asking again.
# "setup_infra.sh init" runs steps 1 and 2 only (make tf-plan, tf-destroy).

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT="${TF_VAR_project_id:?TF_VAR_project_id is not set}"
REGION="${TF_VAR_region:-us-central1}"
SERVICE="${TF_VAR_service_name:-genmedia-for-commerce}"
STATE_BUCKET="$PROJECT-genmedia-tfstate"

tf() { bash "$HERE/terraform.sh" -chdir="$HERE/terraform" "$@"; }

# True when the gcloud command succeeds (the resource exists); never asks.
exists() {
    gcloud "$@" --project="$PROJECT" --quiet </dev/null >/dev/null 2>&1
}

init() {
    if ! exists storage buckets describe "gs://$STATE_BUCKET"; then
        echo "Creating the Terraform state bucket gs://$STATE_BUCKET..."
        gcloud storage buckets create "gs://$STATE_BUCKET" \
            --project="$PROJECT" --location="$REGION" \
            --uniform-bucket-level-access --public-access-prevention --quiet
        gcloud storage buckets update "gs://$STATE_BUCKET" --versioning --quiet
    fi
    tf init -input=false -reconfigure -backend-config="bucket=$STATE_BUCKET"
}

# Import $1 with the id $2 when it is not in the state and the gcloud
# command in the other arguments finds it.
adopt_one() {
    local address="$1" id="$2"
    shift 2
    if grep -qxF "$address" <<<"$IN_STATE" || ! exists "$@"; then
        return 0
    fi
    echo "Adding the existing $id to the Terraform state..."
    tf import -input=false "$address" "$id"
}

adopt() {
    local media="${TF_VAR_media_bucket:-$PROJECT-gfc-media}" sa email shown
    IN_STATE=$(tf state list 2>/dev/null || true)
    # MEDIA_BUCKET names another bucket: forget the one in the state (never
    # deleted, it keeps its files) and adopt or create the one it names.
    if grep -qxF google_storage_bucket.gfc_media <<<"$IN_STATE"; then
        shown=$(tf state show -no-color google_storage_bucket.gfc_media || true)
        if ! grep -qxE " *name *= *\"$media\"" <<<"$shown"; then
            echo "Media bucket: $media (the state's one is kept, not deleted)"
            tf state rm google_storage_bucket.gfc_media
            IN_STATE=$(tf state list 2>/dev/null || true)
        fi
    fi
    adopt_one google_storage_bucket.gfc_media "$media" \
        storage buckets describe "gs://$media"
    for sa in build cloudrun web; do
        email="genmedia-$sa@$PROJECT.iam.gserviceaccount.com"
        adopt_one "google_service_account.$sa" \
            "projects/$PROJECT/serviceAccounts/$email" \
            iam service-accounts describe "$email"
    done
    adopt_one google_artifact_registry_repository.docker \
        "projects/$PROJECT/locations/$REGION/repositories/$SERVICE-repo" \
        artifacts repositories describe "$SERVICE-repo" --location="$REGION"
}

# Step 3: at most 5 minutes; on a re-run the APIs answer at once.
apis() {
    local list n
    list=$(grep -o '"[a-z.]*\.googleapis\.com"' "$HERE/terraform/main.tf" |
        tr -d '"')
    # shellcheck disable=SC2086 # one argument per API
    gcloud services enable $list --project="$PROJECT" --quiet
    for n in $(seq 30); do
        if exists artifacts repositories list --location="$REGION" &&
            exists iam service-accounts list; then
            return 0
        fi
        if [ "$n" = 1 ]; then
            echo "Waiting for the new APIs to be ready (a new project" \
                "takes a minute or two)..."
        fi
        sleep 10
    done
    echo "The APIs are still not ready: Terraform may fail and try again."
}

# Returns when the plan may be applied, else exits.
approve() {
    local answer
    if [ -t 0 ] && [ -t 1 ] && [ -z "${CI:-}" ]; then
        read -r -p "Apply these changes to project $PROJECT? [Y/n] " answer ||
            answer=n
        case "$answer" in
        "" | [yY] | [yY][eE][sS]) return 0 ;;
        esac
        echo "Nothing changed."
        exit 1
    fi
    [ "${TF_AUTO_APPROVE:-}" != 1 ] || return 0
    echo "Nothing changed: no terminal to confirm the Terraform plan" \
        "(set TF_AUTO_APPROVE=1 to apply it without asking)."
    exit 1
}

plan_and_apply() {
    local attempt status asked=""
    PLAN=$(mktemp "${TMPDIR:-/tmp}/genmedia-tfplan.XXXXXX")
    trap 'rm -f "$PLAN"' EXIT
    for attempt in 1 2 3; do
        status=0
        tf plan -input=false -detailed-exitcode -out="$PLAN" || status=$?
        [ "$status" != 0 ] || return 0 # nothing to change
        if [ "$status" = 2 ]; then
            [ -n "$asked" ] || { approve && asked=1; }
            tf apply -input=false "$PLAN" && return 0
        fi
        [ "$attempt" -lt 3 ] || break
        echo "Terraform failed (attempt $attempt of 3): new APIs and" \
            "permissions can take a minute to propagate. Trying again in" \
            "30 s (press Ctrl+C to stop)..."
        sleep 30
    done
    return 1
}

# Plain commands (not after && or ||): set -e stops at a failure inside.
case "${1:-}" in
"") apis; init; adopt; plan_and_apply ;;
init) init; adopt ;;
*) echo "Usage: $0 [init]" >&2 && exit 2 ;;
esac
