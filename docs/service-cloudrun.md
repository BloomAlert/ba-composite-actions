# Service reusable workflow

`.github/workflows/service-cloudrun.yaml` is CI/CD for the Python Cloud Run services (ba-infra#47). The `build-docker` / `deploy-cloudrun` composites stay as they are for their `@main` callers.

| event | runs |
|---|---|
| `pull_request` | `test` (ruff check, ruff format --check, pytest), then `build` without push |
| `push` / `workflow_dispatch` on `staging` or `prod` | `test` → `build` (push `:<github.sha>`) → `deploy` |

- Auth is WIF only: org secrets `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_ARTIFACT_REGISTRY_WRITER_SA` (test + build) and `GCP_DEPLOY_SERVICE_ACCOUNT` (deploy). No JSON key.
- bloompy: the test job uses keyring with the WIF credentials. The build passes the WIF credentials file as the BuildKit secret `gar_service_account`, so existing Dockerfiles that `cp /run/secrets/gar_service_account` work unchanged.
- The AR repo, the image and the Cloud Run service are all named `<repo>-<environment>`.
- `${NAME}` in `cloudrun-flags` / `cloudrun-env-vars` resolves from the environment's GitHub vars, because a caller job cannot read environment vars. A name with no var fails the deploy before it starts.
- `deploy` runs one at a time per repo and environment. Each job has a timeout, and every action is pinned by SHA.
- The check to require in a ruleset is `<caller job id> / test` (for example `cicd / test`).

## Caller

```yaml
on:
  pull_request:
    branches: [staging, prod]
  push:
    branches: [staging, prod]
  workflow_dispatch: # deploys the branch it runs on

permissions: {}

jobs:
  cicd:
    uses: BloomAlert/ba-composite-actions/.github/workflows/service-cloudrun.yaml@<sha> # vX.Y.Z
    permissions:
      contents: read
      id-token: write
    secrets: inherit
    with:
      environment: ${{ github.base_ref || github.ref_name }}
      # test-database-url: postgresql+asyncpg://postgres:test@localhost:5432/postgres
      cloudrun-flags: |
        --service-account=${GCP_SERVICE_ACCOUNT_EMAIL}
        --min-instances=${MIN_INSTANCES}
      cloudrun-env-vars: |
        ENVIRONMENT=${{ github.ref_name }}
```

Tests run with no app env file or secrets. A repo whose config needs values at import gives them offline defaults in `conftest.py`.

## Known gaps

- Dependabot PRs get no secrets, so `test` fails at auth (ba-infra#44).
- Dependabot uv updates fail with 401 on the bloompy index (design below, not built).

### Design: Dependabot uv updates with bloompy

1. Land ba-infra#39 first (requires-python range).
2. Add a `registries:` entry in each service's `dependabot.yml` and list it under the uv update:
   ```yaml
   registries:
     bloompy:
       type: python-index
       url: https://us-west2-python.pkg.dev/ba-basic/bloompy/simple/
       username: _json_key_base64
       password: ${{ secrets.BLOOMPY_READER_KEY_B64 }}
   updates:
     - package-ecosystem: uv
       registries: [bloompy]
   ```
3. Credential: if Dependabot supports OIDC for Google Artifact Registry, use that. Otherwise create a dedicated SA that has only `roles/artifactregistry.reader` on the `bloompy` repository, and store its key as one org Dependabot secret. It is the only key allowed, it is read-only on one repository, and it is rotated yearly. Never use `cicd-github-actions@`.
4. Keep `ignore: bloompy` until bloompy has semver releases.
