# ETL reusable workflows

`etl-build.yaml` and `etl-deploy.yaml` build and run the `etl-*` fleet's Cloud Run jobs. They implement etl-template #47 (CI), #50 (RUN) and #52 (ALERT).

- The composite actions in this repo are separate, and their `@main` callers are unaffected.
- Pin both workflows by full SHA with a `# vX.Y.Z` comment (CI-04/05).
- Examples are in [`examples/`](examples/): ews (in-place adoption), stormglass (collapsing 11 instances) and cmems maps (two resource tiers plus `next`).
- This repo is public, so it holds nothing environment-specific. Everything below that is per project or environment comes from the caller's vars and secrets.

## Inputs

| | `etl-build` | `etl-deploy` |
|---|---|---|
| `environment` (required) | `staging`/`prod`, must equal `github.ref_name` | same in `mode: deploy`; any branch in `mode: drift` |
| `declaration` | default `deploy/runtime.yaml` | same |
| `mode` | – | `deploy` (default) or `drift` |
| `dry_run` | – | `true` prints every gcloud command and the orphan list, and executes nothing |
| outputs | `image` (`…:<sha>`), `digest` | – |

Callers pass `secrets: inherit`. Each name below is looked up in vars, then in secrets. Environment-level values work, because both jobs set `environment: <env>`.

| name | used by |
|---|---|
| `GCP_PROJECT_ID`, `GCP_REGION` | both |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | both (WIF only) |
| `GCP_ARTIFACT_REGISTRY_WRITER_SA` | build |
| `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_WORKFLOW_SERVICE_ACCOUNT`, `GCP_SCHEDULER_SERVICE_ACCOUNT` | deploy |
| `ETL_OPS_NOTIFY_URL` | deploy: the ba-ops-notify URL for this env, set as the Workflow's `NOTIFY_URL` |
| `ETL_ALERT_CHANNEL` | deploy: the notification channel of `#etl-errors-<env>` (`projects/…/notificationChannels/…`) |
| the declaration's `job.service_account_var` and `env` names | deploy |

- **Permissions:** the calling job grants `contents: read` and `id-token: write`, and nothing else.
- **Concurrency:** `<mode>-<env>-<declaration>` (CI-03).
  - It is keyed per flow, so one push to a multi-flow repo doesn't cancel a queued flow.
  - Drift runs never block a deploy.

## Declaration (RUN-03)

There is one file per flow. A flow is one job, one Workflow and one set of resources. Flows may share an image, e.g. CPU/memory tiers such as `maps` and `maps-large`.

```yaml
flow: maps-large              # optional; part of the RUN-02 default name
name: …                       # optional base name, default RUN-02 (below); job = <name>-<env>
workflow_name: …              # optional, default <name>;               workflow = <workflow_name>-<env>
scheduler_name: …             # optional, default "<name>-{instance}"; scheduler = <scheduler_name>-<env>
dockerfile: Dockerfile.maps
image: map                    # AR image name and buildx cache scope
job:
  service_account_var: GCP_MAP_JOB_SERVICE_ACCOUNT_EMAIL
  cpu: 2
  memory: 8Gi
  timeout: 1200s
env:                          # each value is looked up in vars, then secrets
  - AUTH0_DOMAIN              # NAME           = var/secret NAME
  - GCP_BUCKET_NAME=GCP_MAPS_BUCKET   # NAME=SOURCE
  - SFTP_BANNER_TIMEOUT?      # optional: if unset it is omitted; otherwise the deploy fails
time_zone: America/Santiago   # required here or per instance
retry: {count: 16, backoff_seconds: 1800}   # optional: retries of a failed execution
next: etl-ingestion-cmems-timeseries        # optional: Workflow (minus -<env>) started on success, same input
instances:                    # one Scheduler each; name = [a-z0-9-], also the `instance` label
  - {name: chl-02, schedule: "15 8 * * *", args: [--ts-id, CHL-02, --publish-arrival, --source, nrt]}
legacy:                       # globs of the old resources this flow replaces ({env} substituted)
  - "*-map-ingestion-chl-02-*{env}"
```

`ENVIRONMENT` and `GCP_PROJECT_ID` are always set on the job. Each deploy sets the full list, because `run jobs deploy` replaces the env vars.

## Naming (RUN-02)

- **Repos that collapse per-instance resources** take the default names:
  - job and workflow: `<repo-short>[-<flow>]-<env>`
  - scheduler: `<repo-short>[-<flow>]-<instance>-<env>`
  - `repo-short` is the repo name without a trailing `-gcp`.
  - Example: stormglass becomes `etl-ingestion-stormglass-staging` and `etl-ingestion-stormglass-wh-01-staging`.
- **Existing single-job repos** declare their current names with `name`, `workflow_name` and `scheduler_name`. Adoption is then an in-place update with no cutover. ews's plan is `runtime 0 create, 3 update; 0 orphans`.
- The AR repo is unchanged: `<repo>-<env>`.
- Job and workflow names are capped at 63 characters.

## Execution path (RUN-01, ALERT-01)

```
Scheduler (per instance) ── POST …/workflows/<wf>/executions
   {"argument": "{\"args\":[…],\"labels\":{\"instance\":…[,\"flow\":…]}}", "labels": {"instance": …}}
Workflow etl/workflow.yaml (generic, one per flow)
   for attempt in 1..RETRY_COUNT+1:  jobs.run(JOB_NAME, containerOverrides[0].args = args)
      failure → ba-ops-notify typed v2 event (then sleep RETRY_BACKOFF_SECONDS)
   all attempts failed → raise
   success and NEXT_WORKFLOW → executions.create(NEXT_WORKFLOW, same argument, same labels)
```

- **Labels** are native Workflows *execution* labels (`gcloud workflows executions list --filter=labels.instance=wh-01`). Cloud Run's `jobs.run` has no per-execution labels.
- **Every failed attempt is notified** (ALERT-01). The event is `{schema_version: 2, source: {job_name, project, region, environment}, event: {event_type: job_failed, severity: ERROR, title, summary, context, log_uri}}`:
  - `title` is `<job> failed (exit N)`.
  - `summary` is the error message.
  - `context` is the instance labels plus `exit_code` (the first task's `lastAttemptResult.exitCode`), `execution`, `attempt` and `args`.
  - `log_uri` is the execution's console logs.
  - A notifier outage is only logged; the job failure is still raised.
- **Manual run:**
  - `gcloud workflows run <wf> --data='{"args":["--start-dt","…"]}' --labels=instance=manual`
  - The argument must be a map.

## Deploy steps (`dry_run` prints them only)

`etl/live.sh` takes a read-only snapshot of jobs, workflows, schedulers, log metrics and alert policies. `etl/render.py` turns the declaration plus that snapshot into a bash plan, where each resource is a `create` or an `update`:

1. `artifacts docker images describe <image>:<sha>`
2. `artifacts repositories set-cleanup-policies <repo>-<env>` (**RUN-05**):
   - keep the 10 most recent versions of each image
   - keep every version a live job in this AR repo runs, including orphans not yet deleted
   - delete all other tagged versions
   - delete untagged versions older than 7 days
3. `run jobs deploy <job>`
4. `workflows deploy <wf> --source=etl/workflow.yaml`. `etl-deploy` checks this repo out at its own commit (OIDC `job_workflow_sha`). Env: `JOB_NAME`, `NOTIFY_URL`, `RETRY_*`, `NEXT_WORKFLOW`.
5. `scheduler jobs create|update http`, once per instance.
6. **Heartbeat (ALERT-03):**
   - log metric `etl-heartbeat-<job>`: the job's `Container called exit(0).` system log line
   - alert policy of the same name: sum < 1 over a window of 3× the largest gap between scheduled runs, at least 1h and at most 25h (the alerting limit)
   - missing data counts as a breach (`EVALUATION_MISSING_DATA_ACTIVE`, the ba-infra pattern)
   - it notifies `ETL_ALERT_CHANNEL`
7. **Orphans (RUN-04):** live resources that match `legacy`, plus schedulers that target this flow's workflow but aren't declared (removed instances).
   - ENABLED ones are **paused** in the same deploy, so two schedulers never fire.
   - All of them are printed.
   - Nothing is deleted. Delete them by hand after prod is verified.
   - When one push deploys several flows that replace the same legacy set, deploy the narrower-`legacy` flow first (see `examples/cmems/cicd_maps.yaml`).

Self-check: `python3 etl/test_render.py` (needs `yq`). Proof mode: `python3 etl/test_render.py live.json`.

## Drift check (RUN-06)

`mode: drift` runs on a schedule (see [`examples/ews/drift.yaml`](examples/ews/drift.yaml)). It prints two lists and exits 1 when either is non-empty:

- `MISSING`: declared resources that aren't live, including the heartbeat.
- `UNDECLARED`: orphans that haven't been deleted.

## IAM (verify in phase 2)

| SA | roles |
|---|---|
| deploy (`GCP_DEPLOY_SERVICE_ACCOUNT`) | `roles/run.developer`, `roles/workflows.editor`, `roles/cloudscheduler.admin` (pause included), `roles/logging.configWriter`, `roles/monitoring.alertPolicyEditor`, `roles/artifactregistry.admin` on each `<repo>-<env>` AR repo (`repositories.update` for cleanup policies; `repoAdmin` lacks it), `roles/iam.serviceAccountUser` on the job, workflow and scheduler SAs. Possibly also `roles/monitoring.notificationChannelViewer`, if attaching the channel needs `notificationChannels.get` |
| workflow (`GCP_WORKFLOW_SERVICE_ACCOUNT`) | what it has today (`run.jobs.runWithOverrides`, `run.invoker` on ba-ops-notify), plus `roles/run.viewer` (`run.executions.get`, `run.tasks.list` for the exit code) and `roles/workflows.invoker` (for `next`) |
| build (`GCP_ARTIFACT_REGISTRY_WRITER_SA`) | unchanged: `roles/artifactregistry.writer` plus `artifactregistry.repositories.create` |
