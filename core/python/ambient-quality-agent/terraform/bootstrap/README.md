# Project bootstrap

Prepares one Google Cloud project to host AQA by enabling the APIs the
deployment root and the module call, and by making the Cloud Build grant in
`iam.tf`. Apply it once per project, before the AQA
deployment: a deployment applied against an unprepared project fails on the
first resource that needs an API.

```bash
cp terraform.tfvars.example terraform.tfvars   # then edit it
terraform init
terraform apply
```

Re-applying it is the supported way to pick up a new API: each
`google_project_service` reconciles against what is already enabled, so a second
apply on a prepared project plans no changes.

The wait in `services.tf` gives the enable 60 seconds to propagate before the
deployment root runs. Nothing reports when a service is really ready, so 60
seconds is a guess rather than a guarantee. Apply the deployment root again if
it fails with `403 ... has not been used in project ...`.

`agents-cli infra single-project --apply` applies this root second of three,
after the observed agent's own `deployment/terraform/single-project` and before
the AQA deployment root. That first root enables the APIs it needs itself; see
`../README.md`.

## Why this is a separate root

An enabled API is project-scoped. It belongs to no single AQA deployment and
must not be destroyed with one. Nor may the Cloud Build grant: a
`google_project_iam_member` is non-authoritative, so destroying a deployment
that owned it would revoke it for everything else in the project that relies on
it. A state file of its own is what provides that: a
`terraform destroy` can only delete what is in the state it is run against.
`disable_on_destroy = false` on every service covers the other direction, so
that destroying even this root leaves an API on for workloads that have nothing
to do with AQA. A manual `terraform destroy` of this root does revoke the grant.

The two roots are therefore coupled by ordering and nothing else. This root
exports no outputs and the deployment root reads no `terraform_remote_state`:
what a deployment needs is that these resources exist on the project, not that
Terraform told it so.

## Credentials

The caller needs `roles/serviceusage.serviceUsageAdmin` on the project, which a
deployment does not. That asymmetry is another reason the two are separate: the
person who prepares a project and the person who deploys into it need not be the
same. The grant in `iam.tf` also needs `roles/resourcemanager.projectIamAdmin`.
