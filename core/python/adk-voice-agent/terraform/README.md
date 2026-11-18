# Cloud Run deployment (`make deploy`)

`make deploy` runs two steps:

1. `terraform apply` in this directory creates the infrastructure.
2. [`scripts/deploy.py`](../scripts/deploy.py) copies your integration credentials
   from `.env` into Secret Manager, then runs `agents-cli deploy` to build the image
   and ship it to the service, with the settings the computer provider reads
   (`COMPUTER_*`, `APP_URL`) taken from terraform.

First time in a project, run `make onboard` instead: it asks for the project, applies
terraform, builds the computer image, and deploys. After that:

```bash
make deploy                    # project from GOOGLE_CLOUD_PROJECT in .env, else gcloud config
make deploy AUTO_APPROVE=1     # no interactive terraform approval (for agents and CI)
make deploy PROJECT=my-proj REGION=us-central1
make deploy DRY_RUN=1          # terraform plan + agents-cli deploy --dry-run; changes nothing
make computer-image            # build sandbox/Dockerfile, push it, print the tag to roll out
make deploy COMPUTER_IMAGE=us-central1-docker.pkg.dev/my-proj/adk-voice-agent/computer:abc1234
make ssh                       # a shell in your own computer, through IAP
make destroy                   # computers first (home disks only with PURGE=1), then everything below
```

## What terraform creates

| File | Resources |
|---|---|
| `main.tf` | Required APIs: Vertex AI, Cloud Run, Compute Engine, OS Login, Cloud Storage, Cloud Build, Artifact Registry, IAP, Secret Manager, IAM Credentials. Also the Google app's MCP servers (one per Google server in [`.mcp.json`](../.mcp.json): `calendarmcp`, `gmailmcp`, `drivemcp`, `docsmcp`, `sheetsmcp`, `slidesmcp`) and the Calendar, Gmail, Drive, Docs, Sheets and Slides APIs behind them (`enable_workspace_apis`; the Google OAuth client must belong to this project) and Cloud SQL Admin (`enable_cloud_sql`). |
| `iam.tf` | The `<service>` runtime service account (`roles/aiplatform.user`, `roles/logging.logWriter`, `roles/cloudtrace.agent`, and `roles/cloudsql.client` when Cloud SQL is enabled). For per-user connected-app tokens it gets two custom roles: `<service>_user_secret_creator` (create secrets) and `<service>_user_secret_editor` (access, add, and destroy versions), the second only on secrets named `<service>-user-*` through an IAM condition. The default compute account gets the Cloud Build builder role (`grant_cloud_build_builder`) so `--source` deploys work. |
| `secrets.tf` | One Secret Manager secret per deployment-wide credential: each app's OAuth client (ID and secret) in [`config/integrations.yaml`](../config/integrations.yaml), and the GitHub App's URL name (`GITHUB_APP_SLUG`). `make onboard` registers the apps and saves their keys to `.env`; `scripts/deploy.py` copies them into these secrets, which reach Cloud Run as env vars. Only the runtime account can read them. Users' own tokens aren't here: the app keeps them in one `<service>-user-<hash>` secret per user. |
| `cloud_sql.tf` | On by default (`enable_cloud_sql`): a PostgreSQL 16 instance for the thread store and ADK sessions. Its connection URL is stored as the `THREAD_DB_URL` secret. |
| `memory.tf` | The Agent Engine whose Memory Bank keeps each user's memories (no code runs on it). `scripts/deploy.py` reads its `agent_engine` output, sets it on the service as `GOOGLE_CLOUD_AGENT_ENGINE_ID`, and registers the app's profile schema on it with your credentials; `make onboard` also writes it to `.env` for `make dev`. `make destroy` deletes it with its memories (`deletion_policy = "FORCE"`). |
| `cloud_run.tf` | The `adk-voice-agent` service behind IAP, with Direct VPC egress (private ranges only) into the computers' network. |
| `network.tf` | The computers' VPC (`<service>-net`) with two subnets: `<service>-computers` (`computer_subnet_cidr`) and the app's Direct VPC egress range `<service>-app` (`app_subnet_cidr`). Cloud Router and Cloud NAT for outbound traffic (computers have no public IP). Firewall rules on the `adk-computer` tag: IAP's TCP range `35.235.240.0/20` to ports 22 and 8080, and the app's subnet to 8080. |
| `computer.tf` | Everything a user's computer is built from (see below). |

## Users' computers

Terraform owns what is fixed per deployment. The app (`COMPUTER_PROVIDER=gce`) creates
one VM (`<service>-u-<hash>`) and one home disk (`<service>-u-<hash>-home`) per user at
runtime, from these, labeled `goog-adk-computer=true` and `deployment=<service>`
(`COMPUTER_DEPLOYMENT`), so deployments that share a project never touch each other's
computers. GCE names are 63 characters at most, so `service_name` is capped at 39.

| Resource | What it is |
|---|---|
| Artifact Registry repo `<service>` | Holds the computer image (`computer:<tag>`). `make computer-image` builds [`sandbox/Dockerfile`](../sandbox/Dockerfile) into it with Cloud Build. |
| Service account `<service>-computer` (`computer_service_account_id`) | What the coding agents run as: `roles/aiplatform.user`, `roles/logging.logWriter`, pull access to the repo, and `roles/iap.httpsResourceAccessor` on the app so the daemon's MCP relay can reach the bridge. It may sign JWTs as itself (Token Creator on itself only) for that relay. No Secret Manager, Cloud SQL, or compute access. |
| Instance template `<service>-computer-*` | Container-Optimized OS, `computer_machine_type` (default `e2-standard-2`), the `adk-computer` tag, no external IP, shielded VM, OS Login. Its cloud-init ([`computer-cloud-init.yaml.tftpl`](computer-cloud-init.yaml.tftpl)) formats the disk attached as device `home` on first use and mounts it at `/mnt/disks/home`, writes the instance's `computer-env` metadata to the container's env file, and runs `computer.service`: `docker run -p 8080:8080 -v /mnt/disks/home:/home/agent <computer_image>`. Templates are immutable: changing the image makes a new one (`name_prefix`), and its instances carry a new `template` label. |
| Resource policy `<service>-home-snapshots` | Daily snapshots of each home disk, kept 7 days. The app attaches it to every disk it creates. |
| Custom role `<service>_computer_manager` (on the app account) | Project-wide permissions checked before an instance or disk exists: create and list instances from the template, create and list disks, and read zone operations. |
| Custom role `<service>_computer_vm_manager` (on the app account) | Start, stop, resume, and delete instances; set instance metadata, labels, and IAM; and set IAP tunnel IAM (to grant a user SSH to their own computer), only on this deployment's computers (`<service>-u-*`) through an IAM condition. |
| Custom role `<service>_computer_disk_manager` (on the app account) | Read, attach, and update existing disks (`setDiskAutoDelete` checks `compute.disks.update`), only on this deployment's computers' disks (`<service>-u-*`) through an IAM condition. Every Compute call the app makes is checked against these roles by `test_gce_roles_grant_every_call_the_provider_makes` (`tests/unit/test_providers.py`). |
| On the computer account, for the app account | `roles/iam.serviceAccountUser` (computers run as it), and custom role `<service>_computer_sa_access_admin` (get and set this one account's IAM policy, to grant each user `serviceAccountUser` for SSH). |

### How the pieces reach each other

- **App → daemon:** `http://<internal IP>:8080` over Direct VPC egress, with a Google ID
  token for the audience `adk-voice-agent-computer` that the daemon checks.
- **Daemon relay → app (MCP bridge):** through IAP at `APP_URL`. Cloud Run's built-in IAP
  uses a Google-managed OAuth client, which doesn't accept Google-issued ID tokens from
  service accounts ([IAP programmatic authentication](https://cloud.google.com/iap/docs/authentication-howto#authenticate_with_a_service_account_jwt)).
  The relay instead sends a JWT for the computer account, signed with the IAM
  Credentials API (`signJwt`), whose `aud` is `COMPUTER_MCP_AUDIENCE` = `<APP_URL>/*`,
  in `Proxy-Authorization`.
- **User → computer:** `make ssh` = `gcloud compute ssh <service>-u-<hash> --tunnel-through-iap`,
  then `docker exec` into the container as `agent`. The app grants each user IAP tunnel
  access and `roles/compute.osAdminLogin` on their own instance only.

### Rolling out a new image

```bash
make computer-image                       # prints the pushed tag
make deploy COMPUTER_IMAGE=<printed tag>  # or set computer_image in terraform/terraform.tfvars
```

Without `COMPUTER_IMAGE` or a tfvars pin, `make deploy` keeps the image the deployment
already runs; it never falls back to `:latest`.

The apply creates a new template. Each computer is recreated from it the next time it
starts; its home disk is kept.

### Teardown

Computers aren't in terraform's state. `make destroy` first deletes this deployment's
instances (labels `goog-adk-computer=true` and `deployment=<service>`, on `<service>-net`)
with `scripts/deploy.py --delete-computers`, and their disks too with `PURGE=1` (same
labels); when `PURGE=1` is not set, it detaches `<service>-home-snapshots` from kept home
disks first so `terraform destroy` can delete the snapshot schedule. Then it runs
`terraform destroy -auto-approve`. It never asks for confirmation. Kept home disks (without
`PURGE=1`), existing disk snapshots (which expire in 7 days), and per-user connected-app
token secrets (`<service>-user-*`) remain in the project.

If `terraform destroy` fails with `resourceInUseByAnotherResource` on the `<service>-app`
subnet, Cloud Run's Direct VPC egress still holds `serverless-ipv4-*` addresses there;
GCP releases them up to 1-2 hours after the service is deleted. Run `make destroy`
again later.

## Capacity

Cloud Run runs a single instance (`min_instance_count = 1`, `max_instance_count = 1`) with
`max_instance_request_concurrency = 40` and `timeout = "3600s"` ([`cloud_run.tf`](cloud_run.tf),
matched by `scripts/deploy.py`). Active thread state (`ThreadRegistry`, workspace leases,
in-flight `asyncio.Event` permission gates, and `ThreadUpdates`) lives in the process memory
of that instance, so horizontal scaling (`max_instance_count > 1`) would split a user's
WebSocket, REST calls, and MCP relay callbacks across instances that do not share state.

Every open `/run_live` voice WebSocket occupies one of the 40 concurrency slots for up to an
hour alongside UI polling (`/api/v1/state`), REST calls, and MCP relay/gateway requests from
running computers. Plan for roughly **15–25 concurrent active voice + coding users** per
deployment; deploy additional copies (see [State](#state)) for larger teams. Because there is
only one instance, a `make deploy` revision rollout replaces the running container and
interrupts in-flight voice sessions and running thread turns (persisted threads in Cloud SQL
recover on startup and can be resumed with a follow-up message).

## Outputs

`app_url`, `computer_instance_template`, `computer_template_label`, `computer_zone`,
`computer_service_account`, `app_service_account`, `computer_snapshot_policy`,
`computer_image_repository`, `computer_image`, `computer_mcp_audience`, and
`iap_audience`. `deploy` holds what `scripts/deploy.py` reads before the first apply.

## State

State is local by default (`terraform/terraform.tfstate`, gitignored). For shared or CI
deployments, add a GCS backend in `terraform/backend.tf` (for example
`terraform { backend "gcs" { bucket = "<state-bucket>" prefix = "adk-voice-agent" } }`)
and run `terraform -chdir=terraform init -migrate-state`. To deploy a second copy into the
same project, give it its own `service_name` and state (for example
`terraform workspace new <name>`). Everything named per deployment follows `service_name`:
service accounts, custom role ids (`<service>_*`, dashes as underscores), user secret
prefixes (`<service>-user-*`), computers, and their disks.

The make targets take the workspace and a tfvars file of the copy's variables, and pass
them to terraform and `scripts/deploy.py` (which reads the applied `deploy` output):

```bash
make deploy TF_WORKSPACE=staging TFVARS=terraform/staging.tfvars
make computer-image TF_WORKSPACE=staging   # pushes to that copy's repository
```
