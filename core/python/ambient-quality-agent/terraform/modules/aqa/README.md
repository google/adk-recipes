# Ambient Quality Agent — Terraform module

Provisions the Google Cloud infrastructure the Ambient Quality Agent (AQA) runs
on: the storage buckets, the BigQuery dataset and tables its insights and
investigation runs live in, the service account the deployment runs as, the
Agent Runtime itself, and the ambient triggering path that fires it.

One AQA deployment observes one agent deployment and is named after it: every
derived name is `<observed_deployment_name>-aqua`, so a project holds as many AQA
deployments as it has observed ones and none of them collide.

Call this module from a root that owns the provider and the backend;
`../../examples/single-project` is one such root.

## Usage

```hcl
module "aqa" {
  source = "./modules/aqa"

  project_id               = "my-project"
  observed_deployment_name = "my-agent"
  observed_engine_id       = "1234567890"
}
```

`project_id` and `observed_deployment_name` are the required inputs.
`observed_deployment_name` has to be 2 to 18 characters, because the longest
service account id derived from it takes at most 30.

`observed_engine_id` is optional. Leaving it null turns update triggering off
and leaves the schedule as the only trigger — see "Ambient triggering" below.
With `configure_observed_agent = false` it is ignored, along with the schedule
settings.

Inputs and outputs are documented in `variables.tf` and `outputs.tf`; generate a
table with [`terraform-docs`](https://terraform-docs.io) rather than reading a
copy that can rot.

## Deployment name vs agent name

`observed_deployment_name` names infrastructure; `observed_agent_name` is what
AQA matches telemetry on. They differ because an `Agent(name=...)` holds
underscores and a service account id cannot. A wrong agent name matches no rows,
silently.

## The engine

Terraform creates the engine and owns its configuration; `agents-cli deploy`
owns its code. The engine is created with a placeholder archive, because Agent
Engine will not create one with no source at all, and the module ignores `spec`
afterwards so that a deploy can replace the code and add its own environment.

The cost is that the environment the module sets is written at create and never
updated. Any change to it therefore replaces the engine, and the replacement
needs a deploy to put the code back. That is deliberate: the alternative is an
apply that reports success and changes nothing on the running engine.

## Ambient triggering

A Cloud Scheduler job runs on the `investigation_schedule` setting and POSTs to
`/ambient/trigger`. An audit log sink watches `observed_engine_id` and publishes
update events to Pub/Sub. Pub/Sub pushes them to `/ambient/observed-update`. If
`observed_engine_id` is null, the module creates no sink and no publish grant.
The schedule is the only trigger. That is the only option when the observed
agent is not an Agent Runtime. Nothing else writes an `UpdateReasoningEngine`
audit entry. It is also the situation before the observed agent is first
deployed.

That is how a deployment that names its observed agent is triggered. A
deployment with `configure_observed_agent = false` creates neither the job nor
the sink: each attached agent gets its own from `../../examples/attach`, which
`agents-cli aqua attach --apply` applies with the agent's stored settings and
the topic and caller account the engine reports, so attaching needs only the
engine's resource name. The topic, its push subscription, the caller account and
the delay queue stay here in both cases.

Retries of one scheduled tick all carry the same `X-CloudScheduler-ScheduleTime`.
They resolve to one deduplication key. A retry costs nothing extra.
`gcloud scheduler jobs run` sends the next scheduled fire time. A manual run and
the scheduled fire count as the same tick. The engine discards the second one.

When the engine decides to refuse a trigger, it answers HTTP 200 with a note.
Pub/Sub reads HTTP 200 as success. Only the engine's own logs record the
refusal.

`investigation_schedule` is the only setting. Each run checks everything since the
last run that finished, so the schedule can be irregular. A failed run does not
move that point, so the next run checks the same period again. One run never looks back further than
`DATA_LOOKBACK_WINDOW` days, seven by default; if the last run finished longer
ago than that, the older period is skipped and a warning says how much.

A push Pub/Sub cannot deliver is retried five times, then goes to a dead-letter
topic rather than being dropped. Nothing reads that topic. It exists so a
trigger the engine keeps refusing shows up as a backlog, rather than as an
investigation that silently never happened. A message lands there when the
engine is down, when it has no code deployed yet, or when the log entry is
malformed.

## Alerting

The agent writes one structured line when an investigation finishes, and a
log-based alert policy emails the ones that matter.

The agent does not send the email itself. Google Cloud has no API for sending
email from application code, and the one first-party option, the App Engine
Mail API, runs only inside App Engine. Sending directly would need a mail
relay and a stored password, and would give up the deduplication, rate
limiting and recipient list that Cloud Monitoring provides.

`notification_emails` switches it on. Empty is the default: no channel, no
policy, no notification.

Three runs are emailed: one that failed, one that found a new defect, and one
where metrics failed but no defect was recorded at all. The third is not about
clustering dropping a few rubrics, which is normal. It is the case where
nothing clustered, so real failures would go unmentioned. A defect already
tracked is not emailed again, so no email means nothing new rather than
nothing wrong.

Selecting on failed metrics instead would email on every sweep until someone
fixed the defect. Clean runs and deduplicated triggers are silent.

At most one email every five minutes, the lowest a log-based policy allows;
runs finishing closer together share one. Every run writes a log entry, and
one that reached the durable job also writes its result to the jobs bucket.

## APIs

This module creates no API resources. The caller enables them once per project,
in a root whose state no deployment shares; `../../bootstrap` is that root here,
and its README says why.

A newly enabled API takes a moment to work, so a first apply against a
brand-new project can fail once on a bucket and then succeed unchanged on a
re-run.

## Identity

The engine runs as a service account this module creates, and the ambient
trigger hops present a second one; `iam.tf` says why. Attaching a service
account needs `iam.serviceAccounts.actAs` on it. An Owner or Editor has that;
grant `roles/iam.serviceAccountUser` to a least-privileged CI principal before
its first apply.

Binding the two accounts requires `iam.serviceAccounts.setIamPolicy`, a
permission distinct from `actAs`. A project can allow account creation but
withhold it. The apply then fails with a 403 on the caller account, and the
engine cannot enqueue, so the update path produces no investigations.

## Destroy safety

The engine has `deletion_policy = "FORCE"`. Answering one turn leaves a session
behind and the platform refuses to delete an engine that still has one, so
without this an engine becomes undeletable, and unreplaceable, the first time
anything talks to it.

`force_destroy` defaults to `true`. At the default, `terraform destroy` removes
all three buckets, the insight tables and the investigation registry, including
their contents.

## The metrics bucket

Code metrics score each session against Python metric modules the operator
publishes to `<project>-<deployment>-aqua-metrics`, under `current/metrics/`.
The module creates the bucket and grants the engine read.

They are not an analysis mode. `session_review` runs them beside its LLM
reviewer, over the same sessions it has already fetched, and publishing one is
what turns them on.

Write goes to the principals you name in `metrics_writers`, and to nobody
otherwise. Choose them deliberately: what is published there is executed inside
the agent's process on the agent's credentials, so write access to this bucket
is equivalent to deploy access.

A metric the eval config marks remote -- with `remote_custom_function`, or
`execution: remote` beside a `custom_function` or `custom_function_file` -- is
the exception. Its source is read and parsed here but never executed here: the
eval service runs it on its own machines, so it never runs in the agent's
process and never sees the agent's credentials. That costs one service round
trip per session per metric, which is why it is the author's choice.

A remote metric is not a local one moved. Most of the difference is refused at
publish with a message saying why, so write one and read the error. Four things
fail quietly instead, and are worth knowing before you start.

`evaluate` must return a number. The service scores the
`{"score": ..., "explanation": ...}` mapping a local metric may return as 0.0,
which against the default threshold of 1.0 marks every session a defect. A
metric whose own `return` is that mapping is refused at publish, but one that
builds it in a helper is not, and fails this way instead.

The instance is the service's own. The agent's answer is at
`instance["response"]["contents"]["gemini_contents"][0]["parts"][0]["text"]`,
where a local metric reads `instance["response"]`, and the conversation is
under `agent_eval_data` rather than `agent_data`. Read the wrong one and the
metric scores every session identically, with nothing in the run to say so.

`EXPECTED` must be a plain string literal. The remote side reads it out of the
parsed source instead of importing the module, so a computed one silently falls
back to a label derived from the metric's name, splitting one defect across two
insights.

The service returns no explanation for a code metric, so every remote finding
carries the same sentence in place of one. `EXPECTED` is all that distinguishes
one remote defect from another -- write it to stand alone.

Publishing a remote metric also makes an investigation depend on the service.
If no remote metric reaches a verdict on any session, the run fails and the
window does not advance, even where the reviewer and the local metrics scored
cleanly.

A metric that carries a `prompt_template` and no function is judged by the eval
service's model with the same config and placeholders as `agents-cli eval run`;
the [agents-cli evaluation
guide](https://google.github.io/agents-cli/guide/evaluation/) covers writing
one. Each session is judged as a case: `{prompt}` is set only on a single-turn
session, as agents-cli sets it only on a single-turn case. What AQuA adds:

- **Cost.** One model call per session per metric (`judge_model_sampling_count`
  calls when set), on the deployment's Gemini platform quota, up to
  `DATA_EVALUATION_CAP` sessions. The run summary and `aqua metrics publish`
  both state it.
- **Findings.** Without a `threshold`, a judge reports scores only, as in
  agents-cli. With one, each session scoring below it becomes a finding whose
  text is the judge's explanation, filed under `expected`.

The bucket is versioned and keeps overwritten objects for 14 days, because a bad
publish is normally found by a failing sweep rather than by whoever published
it.

An empty bucket is a no-op. No code metric scores anything and the session
review runs alone, which is the run an operator who published nothing would
have got. A library that could not be read reaches the same outcome, and is
told apart only by the warning the run summary carries.

## Trace dumps

The agent exports its own spans as gzipped NDJSON into the jobs bucket, under
`aqa-telemetry/spans/dt=<date>/<hour>/`, so an operator can dump a window of
them and send it to the AQA team. The engine resolves the bucket itself and
takes no setting for it, which is why the module passes no environment variable
for the export and so does not replace the engine to enable it.

`trace_retention_days` bounds how long those objects live, 30 days by default,
as a lifecycle rule on the `aqa-telemetry/` prefix. The spans carry the full
conversation content of the turns they cover.

Apply this module before deploying the agent. A deployed agent exports spans
immediately, while the lifecycle rule exists only once the module is applied, so
deploying first leaves those conversations in the bucket unbounded.

`trace_readers` names the principals allowed to download them, and grants them
`roles/storage.objectViewer` alone: `dump-traces` lists and reads over the GCS
JSON API, so `storage.objects.list` and `storage.objects.get` are all it uses.
Being read-only, a dump does not require the write access the engine holds over
investigation state.

Bucket-level IAM takes no object prefix, so these principals read the whole jobs
bucket — investigation task state and the chat transcripts under `tasks/`
included, not only `aqa-telemetry/`. Name them with that in mind.
