"""Bounded public WSS verification: auth rejection and two completed turns."""

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import ssl
import sys
import time
from urllib.parse import urlsplit

import websocket


class ProbeError(ValueError):
    def __init__(self, message, code="probe-error", diagnostics=None):
        super().__init__(message)
        self.code = code
        self.diagnostics = diagnostics or {}


def response_diagnostics(event):
    response = event.get("response")
    payload = response if isinstance(response, dict) else event
    details = {}
    error = payload.get("error")
    if isinstance(error, dict):
        allowed = {
            "code": {"invalid_api_key", "model_not_found", "model_not_supported",
                     "unsupported_parameter", "invalid_value", "invalid_request",
                     "invalid_request_error", "previous_response_not_found",
                     "rate_limit_exceeded", "insufficient_quota", "server_error",
                     "content_filter", "context_length_exceeded"},
            "type": {"invalid_request_error", "authentication_error", "permission_error",
                     "not_found_error", "rate_limit_error", "server_error", "api_error"},
            "param": {"model", "input", "tools", "tool_choice", "previous_response_id",
                      "max_output_tokens", "stream", "store"},
        }
        for field, values in allowed.items():
            value = error.get(field)
            if value is not None:
                details["error" + field.title()] = value if isinstance(value, str) and value in values else "unrecognized"
    incomplete = payload.get("incomplete_details")
    if isinstance(incomplete, dict) and incomplete.get("reason") is not None:
        reason = incomplete["reason"]
        details["incompleteReason"] = reason if isinstance(reason, str) and reason in (
            "max_output_tokens", "content_filter") else "unrecognized"
    status = event.get("status")
    if type(status) is int and 400 <= status <= 599:
        details["upstreamStatus"] = status
    return details


def event_label(kind):
    if kind in {"error", "response.created", "response.queued", "response.in_progress",
                "response.completed", "response.failed", "response.incomplete",
                "response.output_item.added", "response.output_item.done",
                "response.content_part.added", "response.content_part.done",
                "response.output_text.delta", "response.output_text.done",
                "response.refusal.delta", "response.refusal.done",
                "response.function_call_arguments.delta", "response.function_call_arguments.done",
                "response.reasoning_summary_part.added", "response.reasoning_summary_part.done",
                "response.reasoning_summary_text.delta", "response.reasoning_summary_text.done",
                "response.reasoning_text.delta", "response.reasoning_text.done"}:
        return kind
    return "other"


def endpoint(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.port not in (None, 443)
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ProbeError("Use an authorized HTTPS origin without path, query or credentials")
    return "wss://" + parsed.netloc + "/v1/responses"


def connect(url, key=None, direct=False):
    options = {"timeout": 45, "redirect_limit": 0, "suppress_origin": True,
               "sslopt": {"cert_reqs": ssl.CERT_REQUIRED, "check_hostname": True}}
    if key:
        options["header"] = {"Authorization": "Bearer " + key}
    if direct:
        options["http_no_proxy"] = ["*"]
    return websocket.create_connection(url, **options)


def rejected(url, key=None, direct=False):
    connection = None
    try:
        connection = connect(url, key, direct)
        raise ProbeError("Missing/invalid credential unexpectedly completed a WebSocket handshake")
    except websocket.WebSocketBadStatusException as error:
        if error.status_code not in (401, 403):
            raise ProbeError("Authentication probe returned an unexpected handshake status") from None
        return {"status": error.status_code, "denied": True}
    finally:
        if connection is not None:
            connection.close()


def completed_response(connection, model, previous=None, input_value="Reply with OK."):
    request = {"type": "response.create", "model": model, "input": input_value,
               "max_output_tokens": 2048}
    if previous is not None:
        request["previous_response_id"] = previous
    connection.send(json.dumps(request))
    return read_completed_response(connection)


def read_completed_response(connection):
    deadline = time.monotonic() + 120
    events = []
    for _ in range(256):
        if time.monotonic() >= deadline:
            raise ProbeError("Response did not complete within the bounded time", "response-deadline")
        message = connection.recv()
        if not isinstance(message, str) or len(message.encode()) > 262144:
            raise ProbeError("Unexpected binary, closed or oversized WebSocket event", "invalid-frame")
        try:
            event = json.loads(message)
        except ValueError:
            raise ProbeError("WebSocket returned invalid JSON", "invalid-json") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProbeError("WebSocket returned an invalid event shape", "invalid-event")
        kind = event["type"]
        if kind in ("error", "response.failed", "response.incomplete"):
            raise ProbeError("Model response failed or did not complete; inspect private service logs",
                             kind, {"eventTypes": sorted(set(events + [kind])),
                                    **response_diagnostics(event)})
        if len(kind) > 100 or not re.fullmatch(r"[a-zA-Z0-9_.-]+", kind):
            raise ProbeError("Invalid event type", "invalid-event-type")
        events.append(event_label(kind))
        if kind == "response.completed":
            response = event.get("response", {})
            if not isinstance(response, dict) or response.get("status") != "completed":
                raise ProbeError("Response completion status is invalid", "invalid-completion-status")
            identifier = response.get("id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 2048:
                raise ProbeError("Completed response lacks a bounded continuation ID", "invalid-continuation-id")
            return identifier, response.get("output", []), {"completed": True, "eventTypes": sorted(set(events))}
    raise ProbeError("Response exceeded the event count bound", "event-count-limit")


def tool_turn(connection, model, progress=None):
    request = {"type": "response.create", "model": model, "input": "Call workshop_ping once.",
               "max_output_tokens": 2048, "tools": [{"type": "function", "name": "workshop_ping",
               "description": "Return a fixed local test result; no external actions.",
               "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}],
               "tool_choice": {"type": "function", "name": "workshop_ping"}}
    if progress is not None:
        progress["phase"] = "tool-request"
        progress["turns"] = []
    connection.send(json.dumps(request))
    identifier, output, result = read_completed_response(connection)
    if progress is not None:
        progress["turns"].append(result)
        progress["phase"] = "tool-call-validation"
    if not isinstance(output, list):
        raise ProbeError("Function response output is invalid", "invalid-tool-output")
    calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") != "workshop_ping" or not isinstance(calls[0].get("call_id"), str):
        raise ProbeError("Expected local function call was not produced", "expected-tool-call-missing")
    continuation = [{"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "OK"}]
    if progress is not None:
        progress["phase"] = "tool-result-continuation"
    _, _, second = completed_response(connection, model, identifier, continuation)
    if progress is not None:
        progress["turns"].append(second)
    return [result, second]


def run(args):
    if not args.confirm_authorized_target:
        raise ProbeError("Confirm permission with --confirm-authorized-target")
    url = endpoint(args.api_url)
    if args.output_json.exists() or args.output_json.is_symlink():
        raise ProbeError("Report already exists; choose a new filename")
    key = getpass.getpass("Restricted LiteLLM v-key (hidden): ")
    if not key or any(character.isspace() or ord(character) < 32 for character in key):
        raise ProbeError("Invalid local v-key format")
    report = {"operation": "bounded-responses-wss", "observedAt": datetime.now(timezone.utc).isoformat(),
              "endpoint": url, "accepted": False, "phase": "missing-key",
              "credentialLogged": False, "rawResponseLogged": False}
    try:
        report["missingKey"] = rejected(url, direct=args.direct)
        report["phase"] = "invalid-key"
        report["invalidKey"] = rejected(url, "sk-intentionally-invalid-wss-probe", args.direct)
        report["phase"] = "authorized-handshake"
        connection = connect(url, key, args.direct)
        try:
            report["handshakeStatus"] = connection.getstatus()
            if report["handshakeStatus"] != 101:
                raise ProbeError("Authorized handshake did not return 101")
            if args.tool_roundtrip:
                report["turns"] = tool_turn(connection, args.model, report)
            else:
                report["phase"] = "first-response"
                report["turns"] = []
                identifier, _, first = completed_response(connection, args.model)
                report["turns"].append(first)
                report["phase"] = "second-response-continuation"
                _, _, second = completed_response(connection, args.model, identifier, "Reply with OK again.")
                report["turns"].append(second)
            report["accepted"] = True
            report["phase"] = "completed"
        finally:
            connection.close()
    except (ProbeError, websocket.WebSocketException, OSError) as error:
        report["failureType"] = type(error).__name__
        if isinstance(error, ProbeError):
            report["failureCode"] = error.code
            report["failureDetails"] = error.diagnostics
        elif isinstance(error, websocket.WebSocketTimeoutException):
            report["failureCode"] = "websocket-timeout"
        else:
            report["failureCode"] = "transport-error"
        raise ProbeError("WSS verification failed; check sanitized report and private service logs") from None
    finally:
        descriptor = os.open(args.output_json, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(report, target, indent=2)
            target.write("\n")
    print(json.dumps(report))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--confirm-authorized-target", action="store_true")
    parser.add_argument("--direct", action="store_true")
    parser.add_argument("--tool-roundtrip", action="store_true")
    parser.add_argument("--output-json", type=Path, required=True)
    try:
        run(parser.parse_args())
    except (ProbeError, OSError, ValueError) as error:
        detail = str(error) if isinstance(error, ProbeError) else type(error).__name__
        print(f"WSS probe failed: {detail}. No credential or response body printed.", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
