#!/usr/bin/env python3
"""Minimal HTTP API for running SeleniumBase inside Cloudflare Containers."""

import json
import os
import subprocess
import time
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


import base64
import re

BASE_DIR = Path("/SeleniumBase").resolve()
PORT = int(os.environ.get("SELENIUMBASE_API_PORT", "8000"))
RESULTS_ROOT = Path(
    os.environ.get("SELENIUMBASE_RESULTS_DIR", "/tmp/seleniumbase-results")
).resolve()
DEFAULT_TEST = "examples/my_first_test.py"
MAX_TIMEOUT_SECONDS = 3600
MAX_BROWSE_TIMEOUT = 120
LOG_TAIL_BYTES = 4000
SAFE_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)


def _json_bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True).encode("utf-8")


def _error_payload(message: str, *, detail: Any = None) -> dict[str, Any]:
    payload = {"ok": False, "error": message}
    if detail is not None:
        payload["detail"] = detail
    return payload


def _read_request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    if length == 0:
        return {}
    raw_body = handler.rfile.read(length)
    if not raw_body:
        return {}
    try:
        body = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    return body


def _resolve_repo_file(relative_path: str) -> tuple[Path, str]:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError('"test" must be a non-empty string')
    candidate = Path(relative_path.strip())
    if candidate.is_absolute():
        raise ValueError('"test" must be relative to /SeleniumBase')
    if ".." in candidate.parts:
        raise ValueError('"test" must not contain ".."')
    resolved = (BASE_DIR / candidate).resolve()
    if BASE_DIR not in resolved.parents and resolved != BASE_DIR:
        raise ValueError('"test" resolves outside /SeleniumBase')
    if not resolved.is_file():
        raise FileNotFoundError(f'test file not found: "{candidate.as_posix()}"')
    return resolved, candidate.as_posix()


def _validated_pytest_args(raw_args: Any) -> list[str]:
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list):
        raise ValueError('"pytest_args" must be a list of strings')
    args: list[str] = []
    for item in raw_args:
        if not isinstance(item, str):
            raise ValueError('"pytest_args" must only contain strings')
        value = item.strip()
        if not value:
            continue
        if "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError('"pytest_args" contains an invalid value')
        args.append(value)
    return args


def _coerce_timeout_seconds(raw_timeout: Any) -> int:
    if raw_timeout in (None, ""):
        return 300
    try:
        timeout = int(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError('"timeout_seconds" must be an integer') from exc
    if timeout < 1 or timeout > MAX_TIMEOUT_SECONDS:
        raise ValueError(
            f'"timeout_seconds" must be between 1 and {MAX_TIMEOUT_SECONDS}'
        )
    return timeout


def _build_pytest_command(
    rel_test_path: str, pytest_args: list[str], junit_path: Path
) -> list[str]:
    command = ["pytest", rel_test_path]
    command.extend(pytest_args)
    if not any(arg.startswith("--browser=") for arg in command):
        command.append("--browser=chrome")
    if not any(arg in ("--headless", "--headed") for arg in command):
        command.append("--headless")
    if not any(arg.startswith("--junitxml") for arg in command):
        command.append(f"--junitxml={junit_path}")
    return command


def _tail_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return value[-LOG_TAIL_BYTES:]


def _write_text(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def _is_safe_id(value: str) -> bool:
    return bool(value) and all(char in SAFE_ID_CHARS for char in value)


def _run_job(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    requested_job_id = payload.get("job_id")
    if requested_job_id is None:
        requested_job_id = f"job-{int(time.time() * 1000)}"
    if not isinstance(requested_job_id, str) or not requested_job_id.strip():
        raise ValueError('"job_id" must be a non-empty string when provided')
    job_id = requested_job_id.strip()
    if not _is_safe_id(job_id):
        raise ValueError('"job_id" must match /^[A-Za-z0-9._-]+$/')

    _, rel_test_path = _resolve_repo_file(payload.get("test") or DEFAULT_TEST)
    pytest_args = _validated_pytest_args(payload.get("pytest_args"))
    timeout_seconds = _coerce_timeout_seconds(payload.get("timeout_seconds"))

    job_dir = RESULTS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    junit_path = job_dir / "junit.xml"
    stdout_path = job_dir / "pytest-stdout.txt"
    stderr_path = job_dir / "pytest-stderr.txt"
    metadata_path = job_dir / "metadata.json"

    command = _build_pytest_command(rel_test_path, pytest_args, junit_path)
    start_time = time.time()
    completed = None
    timed_out = False

    try:
        completed = subprocess.run(
            command,
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        stdout_text = completed.stdout
        stderr_text = completed.stderr
        return_code = completed.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = _tail_text(exc.stdout)
        stderr_text = _tail_text(exc.stderr)
        return_code = 124

    _write_text(stdout_path, stdout_text)
    _write_text(stderr_path, stderr_text)

    finished_at = time.time()
    metadata = {
        "job_id": job_id,
        "test": rel_test_path,
        "command": command,
        "returncode": return_code,
        "passed": return_code == 0,
        "timed_out": timed_out,
        "duration_seconds": round(finished_at - start_time, 3),
        "started_at": start_time,
        "finished_at": finished_at,
        "artifacts": {
            "stdout": f"/jobs/{job_id}/artifacts/pytest-stdout.txt",
            "stderr": f"/jobs/{job_id}/artifacts/pytest-stderr.txt",
            "junit": f"/jobs/{job_id}/artifacts/junit.xml",
            "metadata": f"/jobs/{job_id}/artifacts/metadata.json",
        },
    }
    metadata_path.write_bytes(_json_bytes(metadata))

    status_code = HTTPStatus.OK
    if timed_out:
        status_code = HTTPStatus.GATEWAY_TIMEOUT

    response = {
        "ok": True,
        **metadata,
        "stdout_tail": _tail_text(stdout_text),
        "stderr_tail": _tail_text(stderr_text),
    }
    return response, status_code


URL_PATTERN = re.compile(
    r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE
)

ALLOWED_ACTIONS = {
    "click", "type", "wait", "get_text", "get_attribute",
    "screenshot", "select", "scroll_to", "execute_script",
}


def _validate_url(url: Any) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError('"url" must be a non-empty string')
    url = url.strip()
    if not URL_PATTERN.match(url):
        raise ValueError('"url" must be a valid http/https URL')
    return url


def _validate_actions(raw_actions: Any) -> list[dict[str, Any]]:
    if raw_actions is None:
        return []
    if not isinstance(raw_actions, list):
        raise ValueError('"actions" must be a list')
    validated = []
    for i, action in enumerate(raw_actions):
        if not isinstance(action, dict):
            raise ValueError(f'actions[{i}] must be an object')
        cmd = action.get("action")
        if cmd not in ALLOWED_ACTIONS:
            raise ValueError(
                f'actions[{i}].action must be one of: '
                f'{", ".join(sorted(ALLOWED_ACTIONS))}'
            )
        validated.append(action)
    return validated


def _browse(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    url = _validate_url(payload.get("url"))
    actions = _validate_actions(payload.get("actions"))
    timeout = _coerce_timeout_seconds(
        payload.get("timeout_seconds") or 60
    )
    if timeout > MAX_BROWSE_TIMEOUT:
        timeout = MAX_BROWSE_TIMEOUT
    extract_html = bool(payload.get("html", False))
    extract_text = bool(payload.get("text", True))
    take_screenshot = bool(payload.get("screenshot", True))
    selectors = payload.get("extract", [])
    if not isinstance(selectors, list):
        raise ValueError('"extract" must be a list of CSS selectors')

    job_id = f"browse-{int(time.time() * 1000)}"
    job_dir = RESULTS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = job_dir / "screenshot.png"

    script = _build_browse_script(
        url=url,
        actions=actions,
        extract_html=extract_html,
        extract_text=extract_text,
        take_screenshot=take_screenshot,
        screenshot_path=str(screenshot_path),
        selectors=selectors,
        result_path=str(job_dir / "result.json"),
    )
    script_path = job_dir / "browse_task.py"
    script_path.write_text(script, encoding="utf-8")

    start_time = time.time()
    timed_out = False
    try:
        completed = subprocess.run(
            ["python3", str(script_path)],
            cwd=str(BASE_DIR),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        return_code = completed.returncode
        stderr_text = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = 124
        stderr_text = _tail_text(exc.stderr)

    duration = round(time.time() - start_time, 3)

    result_file = job_dir / "result.json"
    browse_result: dict[str, Any] = {}
    if result_file.is_file():
        try:
            browse_result = json.loads(
                result_file.read_text(encoding="utf-8")
            )
        except json.JSONDecodeError:
            pass

    screenshot_b64 = None
    if take_screenshot and screenshot_path.is_file():
        screenshot_b64 = base64.b64encode(
            screenshot_path.read_bytes()
        ).decode("ascii")

    response: dict[str, Any] = {
        "ok": return_code == 0 and not timed_out,
        "job_id": job_id,
        "url": url,
        "duration_seconds": duration,
        "timed_out": timed_out,
    }
    if browse_result.get("title"):
        response["title"] = browse_result["title"]
    if browse_result.get("text"):
        response["page_text"] = browse_result["text"][:8000]
    if browse_result.get("html"):
        response["page_html"] = browse_result["html"][:50000]
    if browse_result.get("extracted"):
        response["extracted"] = browse_result["extracted"]
    if browse_result.get("action_results"):
        response["action_results"] = browse_result["action_results"]
    if screenshot_b64:
        response["screenshot_base64"] = screenshot_b64
    if return_code != 0:
        response["error"] = stderr_text[-2000:] if stderr_text else ""
    response["artifacts"] = {
        "screenshot": f"/artifacts/{job_id}/screenshot.png",
        "result": f"/artifacts/{job_id}/result.json",
    }

    status = HTTPStatus.OK if response["ok"] else (
        HTTPStatus.GATEWAY_TIMEOUT if timed_out
        else HTTPStatus.INTERNAL_SERVER_ERROR
    )
    return response, status


def _build_browse_script(
    *,
    url: str,
    actions: list[dict[str, Any]],
    extract_html: bool,
    extract_text: bool,
    take_screenshot: bool,
    screenshot_path: str,
    selectors: list[Any],
    result_path: str,
) -> str:
    actions_json = json.dumps(actions)
    selectors_json = json.dumps(selectors)
    return f'''#!/usr/bin/env python3
"""Auto-generated browse task."""
import json
from seleniumbase import SB

url = {url!r}
actions = json.loads({actions_json!r})
selectors = json.loads({selectors_json!r})
result = {{"title": "", "text": "", "html": "", "extracted": {{}}}}
result["action_results"] = []

with SB(uc=True, headless=True, test=True) as sb:
    sb.open(url)
    sb.sleep(1)

    for act in actions:
        cmd = act["action"]
        sel = act.get("selector", "")
        val = act.get("value", "")
        act_result = {{"action": cmd, "selector": sel}}
        try:
            if cmd == "click":
                sb.click(sel)
            elif cmd == "type":
                sb.type(sel, val)
            elif cmd == "wait":
                sb.sleep(float(val) if val else 2)
            elif cmd == "get_text":
                act_result["value"] = sb.get_text(sel)
            elif cmd == "get_attribute":
                attr = act.get("attribute", "href")
                act_result["value"] = sb.get_attribute(sel, attr)
            elif cmd == "screenshot":
                pass  # handled below
            elif cmd == "select":
                sb.select_option_by_text(sel, val)
            elif cmd == "scroll_to":
                sb.scroll_to(sel)
            elif cmd == "execute_script":
                act_result["value"] = sb.execute_script(val)
            act_result["ok"] = True
        except Exception as e:
            act_result["ok"] = False
            act_result["error"] = str(e)
        result["action_results"].append(act_result)

    result["title"] = sb.get_title()
    if {extract_text!r}:
        result["text"] = sb.get_text("body")[:8000]
    if {extract_html!r}:
        result["html"] = sb.get_page_source()[:50000]
    if {take_screenshot!r}:
        try:
            import base64 as _b64
            _metrics = sb.execute_cdp_cmd(
                "Page.getLayoutMetrics", {{}}
            )
            _cw = _metrics["contentSize"]["width"]
            _ch = _metrics["contentSize"]["height"]
            sb.execute_cdp_cmd(
                "Emulation.setDeviceMetricsOverride",
                {{
                    "mobile": False,
                    "width": _cw,
                    "height": _ch,
                    "deviceScaleFactor": 1,
                }},
            )
            _clip = {{
                "x": 0, "y": 0,
                "width": _cw, "height": _ch, "scale": 1,
            }}
            _shot = sb.execute_cdp_cmd(
                "Page.captureScreenshot",
                {{"format": "png", "clip": _clip, "captureBeyondViewport": True}},
            )
            with open({screenshot_path!r}, "wb") as _f:
                _f.write(_b64.b64decode(_shot["data"]))
        except Exception:
            sb.save_screenshot({screenshot_path!r})

    for sel in selectors:
        try:
            result["extracted"][sel] = sb.get_text(sel)
        except Exception:
            result["extracted"][sel] = None

    with open({result_path!r}, "w") as f:
        json.dump(result, f)
'''


class SeleniumBaseRequestHandler(BaseHTTPRequestHandler):
    server_version = "SeleniumBaseCloudflare/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(
            "[worker_api] %s - - [%s] %s"
            % (self.address_string(), self.log_date_time_string(), fmt % args)
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path) -> None:
        body = file_path.read_bytes()
        content_type = "application/octet-stream"
        if file_path.suffix == ".xml":
            content_type = "application/xml; charset=utf-8"
        elif file_path.suffix == ".json":
            content_type = "application/json; charset=utf-8"
        elif file_path.suffix == ".txt":
            content_type = "text/plain; charset=utf-8"

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "seleniumbase-worker-api",
                    "port": PORT,
                    "default_test": DEFAULT_TEST,
                    "routes": {
                        "health": "GET /health",
                        "run": "POST /run",
                        "browse": "POST /browse",
                        "artifacts": "GET /artifacts/<job_id>/<filename>",
                    },
                },
            )
            return

        if parsed.path == "/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "service": "seleniumbase-worker-api",
                    "cwd": str(BASE_DIR),
                    "results_dir": str(RESULTS_ROOT),
                },
            )
            return

        parts = parsed.path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "artifacts":
            job_id, file_name = parts[1], parts[2]
            if not job_id or not file_name or "/" in file_name or ".." in file_name:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    _error_payload("invalid artifact path"),
                )
                return
            artifact_path = (RESULTS_ROOT / job_id / file_name).resolve()
            if RESULTS_ROOT not in artifact_path.parents:
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    _error_payload("artifact path resolves outside results root"),
                )
                return
            if not artifact_path.is_file():
                self._send_json(
                    HTTPStatus.NOT_FOUND,
                    _error_payload("artifact not found"),
                )
                return
            self._send_file(artifact_path)
            return

        self._send_json(HTTPStatus.NOT_FOUND, _error_payload("route not found"))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)

        if parsed.path == "/run":
            handler_fn = _run_job
        elif parsed.path == "/browse":
            handler_fn = _browse
        else:
            self._send_json(
                HTTPStatus.NOT_FOUND, _error_payload("route not found")
            )
            return

        try:
            payload = _read_request_json(self)
            response, status = handler_fn(payload)
            self._send_json(status, response)
        except FileNotFoundError as exc:
            self._send_json(
                HTTPStatus.NOT_FOUND, _error_payload(str(exc))
            )
        except ValueError as exc:
            self._send_json(
                HTTPStatus.BAD_REQUEST, _error_payload(str(exc))
            )
        except Exception as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                _error_payload(
                    "unexpected error",
                    detail={
                        "message": str(exc),
                        "traceback": traceback.format_exc(limit=20),
                    },
                ),
            )


def main() -> None:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), SeleniumBaseRequestHandler)
    print(f"[worker_api] listening on 0.0.0.0:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
