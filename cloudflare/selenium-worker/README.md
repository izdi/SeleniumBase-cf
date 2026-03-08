# SeleniumBase Cloudflare Worker

This scaffold runs SeleniumBase inside a Cloudflare Container and exposes a minimal Worker API for triggering tests.

## Files

- `wrangler.jsonc`: Cloudflare Worker + Container configuration
- `src/index.ts`: Worker routing and container lifecycle
- `../../integrations/cloudflare/entrypoint.sh`: Container entrypoint (starts Xvfb + Python API)
- `../../integrations/cloudflare/worker_api.py`: Python HTTP API that runs `pytest`
- `../../Dockerfile`: Existing SeleniumBase image, reused as the container image

## Prerequisites

- Node.js 22+
- Wrangler 4.x
- Docker is required for local development (`wrangler dev`) and local deploys. Not needed for CI deploys.

## Install

```bash
cd cloudflare/selenium-worker
npm install
```

## Local development (requires Docker)

```bash
npm run dev
```

The repo `Dockerfile` exposes port `8000` so `wrangler dev` can connect to the container locally. The container entrypoint starts Xvfb (needed for headless Chrome) and then the Python API.

### Run the example SeleniumBase test

```bash
curl -X POST http://localhost:8787/run \
  -H 'Content-Type: application/json' \
  -d '{"test":"examples/my_first_test.py"}'
```

### Run a specific job id

```bash
curl -X POST http://localhost:8787/jobs/demo-job/run \
  -H 'Content-Type: application/json' \
  -d '{"test":"examples/my_first_test.py","timeout_seconds":300}'
```

### Fetch an artifact

```bash
curl http://localhost:8787/jobs/demo-job/artifacts/junit.xml
```

## Deploy

### Via GitHub Actions (no local Docker needed)

Push to `master` or trigger the workflow manually. The GitHub Actions runner has Docker pre-installed and handles the full build+deploy.

Required repository secrets:

- `CLOUDFLARE_API_TOKEN` — API token with Workers + R2 permissions
- `CLOUDFLARE_ACCOUNT_ID` — your Cloudflare account ID

See `.github/workflows/cloudflare-deploy.yml`.

### Locally (requires Docker)

```bash
npm run deploy
```

The first deploy is slower because Wrangler builds and pushes the Docker image before provisioning the container.

## Current limitations

- Artifacts are stored on the container filesystem under `/tmp/seleniumbase-results`, so they are ephemeral.
- The scaffold returns test results synchronously. For longer suites, add R2 + Queue or Workflows.
- The Worker reuses the main repo `Dockerfile` and overrides the runtime entrypoint to start the Python API.
