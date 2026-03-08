import { Container } from "@cloudflare/containers";

interface ContainerStub {
  fetch(request: Request): Promise<Response>;
  startAndWaitForPorts(options?: {
    startOptions?: {
      envVars?: Record<string, string>;
    };
  }): Promise<void>;
}

interface ContainerBinding {
  getByName(name: string): ContainerStub;
}

interface Env {
  SELENIUMBASE_RUNNER: ContainerBinding;
}

interface RunRequestPayload {
  job_id?: string;
  test?: string;
  pytest_args?: string[];
  timeout_seconds?: number;
}

const SAFE_ID_PATTERN = /^[A-Za-z0-9._-]+$/;

export class SeleniumBaseContainer extends Container {
  defaultPort = 8000;
  sleepAfter = "10m";
  enableInternet = true;
  entrypoint = ["/SeleniumBase/integrations/cloudflare/entrypoint.sh"];
  envVars = {
    SELENIUMBASE_API_PORT: "8000",
    SELENIUMBASE_RESULTS_DIR: "/tmp/seleniumbase-results",
  };

  override onStart() {
    console.log("SeleniumBase container started");
  }

  override onError(error: unknown) {
    console.error("SeleniumBase container error", error);
  }
}

function json(
  body: Record<string, unknown>,
  init: ResponseInit = {},
): Response {
  const headers = new Headers(init.headers);
  headers.set("content-type", "application/json; charset=utf-8");
  return new Response(JSON.stringify(body, null, 2), {
    ...init,
    headers,
  });
}

async function parseJson<T>(request: Request): Promise<T> {
  const contentType = request.headers.get("content-type") || "";
  if (contentType && !contentType.includes("application/json")) {
    throw new Error('Expected "Content-Type: application/json"');
  }
  try {
    return (await request.json()) as T;
  } catch {
    throw new Error("Request body must be valid JSON");
  }
}

function buildContainerRequest(
  request: Request,
  pathname: string,
  init?: RequestInit,
): Request {
  const target = new URL(request.url);
  target.pathname = pathname;
  target.search = "";
  return new Request(target, init);
}

async function runJob(request: Request, env: Env, forcedJobId?: string) {
  const payload = await parseJson<RunRequestPayload>(request);
  const jobId =
    forcedJobId ||
    payload.job_id ||
    request.headers.get("x-seleniumbase-job-id") ||
    crypto.randomUUID();
  if (!SAFE_ID_PATTERN.test(jobId)) {
    return json(
      {
        ok: false,
        error: 'job_id must match /^[A-Za-z0-9._-]+$/',
      },
      { status: 400 },
    );
  }

  const container = env.SELENIUMBASE_RUNNER.getByName(jobId);
  await container.startAndWaitForPorts({
    startOptions: {
      envVars: {
        SELENIUMBASE_CONTAINER_ID: jobId,
      },
    },
  });

  const upstreamRequest = buildContainerRequest(request, "/run", {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-seleniumbase-job-id": jobId,
    },
    body: JSON.stringify({ ...payload, job_id: jobId }),
  });

  const response = await container.fetch(upstreamRequest);
  const headers = new Headers(response.headers);
  headers.set("x-seleniumbase-job-id", jobId);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

function containerFromJob(env: Env, jobId: string): ContainerStub {
  return env.SELENIUMBASE_RUNNER.getByName(jobId);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    try {
      const url = new URL(request.url);
      const segments = url.pathname.split("/").filter(Boolean);

      if (url.pathname === "/") {
        return json({
          ok: true,
          service: "seleniumbase-cloudflare-worker",
          endpoints: {
            health: "GET /health",
            run: "POST /run",
            browse: "POST /browse",
            runWithId: "POST /jobs/:jobId/run",
            jobHealth: "GET /jobs/:jobId/health",
            artifact: "GET /jobs/:jobId/artifacts/:filename",
          },
          examples: {
            run_test: [
              "curl -X POST <worker>/run \\",
              "  -H 'Content-Type: application/json' \\",
              "  -d '{\"test\":\"examples/my_first_test.py\"}'",
            ].join("\n"),
            browse: [
              "curl -X POST <worker>/browse \\",
              "  -H 'Content-Type: application/json' \\",
              '  -d \'{"url":"https://example.com",',
              '       "extract":["h1","p"],',
              '       "screenshot":true}\'',
            ].join("\n"),
          },
        });
      }

      if (url.pathname === "/health") {
        return json({
          ok: true,
          service: "seleniumbase-cloudflare-worker",
        });
      }

      if (request.method === "POST" && url.pathname === "/run") {
        return runJob(request, env);
      }

      if (request.method === "POST" && url.pathname === "/browse") {
        const payload = await parseJson<Record<string, unknown>>(
          request,
        );
        const browseJobId = `browse-${Date.now()}`;
        const container =
          env.SELENIUMBASE_RUNNER.getByName(browseJobId);
        await container.startAndWaitForPorts();
        const upstream = buildContainerRequest(request, "/browse", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        });
        return container.fetch(upstream);
      }

      if (segments.length >= 2 && segments[0] === "jobs") {
        const jobId = segments[1];
        if (!SAFE_ID_PATTERN.test(jobId)) {
          return json(
            {
              ok: false,
              error: 'job_id must match /^[A-Za-z0-9._-]+$/',
            },
            { status: 400 },
          );
        }
        const container = containerFromJob(env, jobId);

        if (
          request.method === "POST" &&
          segments.length === 3 &&
          segments[2] === "run"
        ) {
          return runJob(request, env, jobId);
        }

        if (
          request.method === "GET" &&
          segments.length === 3 &&
          segments[2] === "health"
        ) {
          return container.fetch(
            buildContainerRequest(request, "/health", { method: "GET" }),
          );
        }

        if (
          request.method === "GET" &&
          segments.length === 4 &&
          segments[2] === "artifacts"
        ) {
          const artifactName = segments[3];
          return container.fetch(
            buildContainerRequest(
              request,
              `/artifacts/${encodeURIComponent(jobId)}/${encodeURIComponent(artifactName)}`,
              { method: "GET" },
            ),
          );
        }
      }

      return json(
        {
          ok: false,
          error: "route not found",
        },
        { status: 404 },
      );
    } catch (error) {
      if (error instanceof Response) {
        return error;
      }
      return json(
        {
          ok: false,
          error: error instanceof Error ? error.message : "unexpected error",
        },
        { status: 400 },
      );
    }
  },
} satisfies ExportedHandler<Env>;
