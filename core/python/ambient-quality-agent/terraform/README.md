# Terraform

`modules/aqa/` is the reusable piece, a child module with no provider and no
backend, so any root can call it, including by Git URL from another repository.
`examples/single-project/` is such a root: one AQA deployment in one project. A
root owns the provider and the state. `bootstrap/` is a second root, applied once
per project; it holds the APIs and the Cloud Build grant the whole project
shares in a state no deployment can destroy, and `bootstrap/README.md` says why.

`examples/attach/` is the third root: the triggers that start an investigation
of one attached agent, a Cloud Scheduler job and an audit log sink on the
observed engine's updates. `agents-cli aqua attach --apply` applies it once per
agent, with its own state, from the values `attach` stored and the topic and
caller account AQuA's engine reports, so it needs only the engine's resource
name. It is for a deployment provisioned with no observed agent
(`configure_observed_agent = false`); one that names its agent keeps creating
that agent's triggers itself.

AQA watches an agent that already exists. Create that agent with `agents-cli
scaffold create`, whose own documentation covers the choices it asks for.
Everything below starts from the agent's directory, the one holding
`agents-cli-manifest.yaml`.

Name the project explicitly on every command that reaches the cloud.
`agents-cli` reads an ambient `GOOGLE_CLOUD_PROJECT` before it falls back to
`gcloud config`, so without `--project` the work can land in a project you never
chose.

```bash
agents-cli scaffold enhance --aqua                           # once, adds aqua_path to the manifest
agents-cli infra single-project --apply --project my-project
agents-cli deploy --project my-project                       # the observed agent's own code
```

`enhance --aqua` checks this `terraform/` subtree out into the agent's `.aqua/`.
`infra single-project --apply` then applies three roots: the agent's own
`deployment/terraform/single-project`, which creates the telemetry dataset AQA
reads, then `.aqua/terraform/bootstrap`, then
`.aqua/terraform/examples/single-project`.


`--aqua-origin` chooses what is fetched (from git or local directory) and
`--aqua-path` where it lands (in `.aqua` by default). Keep anything new under
`terraform/` and reachable from those four variables or a default. There is no
`.tfvars` and no wrapper to edit.

Every state file lives in `.aqua/`, and changing `--aqua-origin` means deleting
it. Run `terraform destroy` on both AQA roots first, or the resources are left
with nothing to destroy them from.

Without `agents-cli`, apply `bootstrap`, then `examples/single-project`, then
`examples/attach` by hand; each takes a `terraform.tfvars` copied from its
`terraform.tfvars.example`.
`modules/aqa/README.md` describes the module.

## Running an investigation

POST the engine's `/investigations/schedule` route. Container routes hang off
`reasoningEngines/v1`, and the path after `/api/` is what the container
receives.

```bash
curl -X POST -H "Content-Type: application/json" \
  -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  -d '{}' \
  "https://us-central1-aiplatform.googleapis.com/reasoningEngines/v1/projects/my-project/locations/us-central1/reasoningEngines/<engine_id>/api/investigations/schedule"
```

The response carries the new run record, with a `run_id` and status `pending`;
the investigation itself continues as a durable job. That job writes its result
to `gs://<jobs-bucket>/aqa-jobs/<run_id>.json`. The bucket is
`<project>-<observed_deployment_name>-aqua-jobs`, and the deployment root's `buckets`
output names it.

