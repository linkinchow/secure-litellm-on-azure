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
    pass


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
            raise ProbeError("Response did not complete within the bounded time")
        message = connection.recv()
        if not isinstance(message, str) or len(message.encode()) > 262144:
            raise ProbeError("Unexpected binary, closed or oversized WebSocket event")
        try:
            event = json.loads(message)
        except ValueError:
            raise ProbeError("WebSocket returned invalid JSON") from None
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise ProbeError("WebSocket returned an invalid event shape")
        kind = event["type"]
        if kind in ("error", "response.failed", "response.incomplete"):
            raise ProbeError("Model response failed or did not complete; inspect private service logs")
        if len(kind) > 100 or not re.fullmatch(r"[a-zA-Z0-9_.-]+", kind):
            raise ProbeError("Invalid event type")
        events.append(kind)
        if kind == "response.completed":
            response = event.get("response", {})
            if not isinstance(response, dict) or response.get("status") != "completed":
                raise ProbeError("Response completion status is invalid")
            identifier = response.get("id")
            if not isinstance(identifier, str) or not identifier or len(identifier) > 2048:
                raise ProbeError("Completed response lacks a bounded continuation ID")
            return identifier, response.get("output", []), {"completed": True, "eventTypes": sorted(set(events))}
    raise ProbeError("Response exceeded the event count bound")


def tool_turn(connection, model):
    request = {"type": "response.create", "model": model, "input": "Call workshop_ping once.",
               "max_output_tokens": 2048, "tools": [{"type": "function", "name": "workshop_ping",
               "description": "Return a fixed local test result; no external actions.",
               "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}],
               "tool_choice": {"type": "function", "name": "workshop_ping"}}
    connection.send(json.dumps(request))
    identifier, output, result = read_completed_response(connection)
    if not isinstance(output, list):
        raise ProbeError("Function response output is invalid")
    calls = [item for item in output if isinstance(item, dict) and item.get("type") == "function_call"]
    if len(calls) != 1 or calls[0].get("name") != "workshop_ping" or not isinstance(calls[0].get("call_id"), str):
        raise ProbeError("Expected local function call was not produced")
    continuation = [{"type": "function_call_output", "call_id": calls[0]["call_id"], "output": "OK"}]
    _, _, second = completed_response(connection, model, identifier, continuation)
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
                report["phase"] = "tool-roundtrip"
                report["turns"] = tool_turn(connection, args.model)
            else:
                report["phase"] = "two-turn-continuation"
                identifier, _, first = completed_response(connection, args.model)
                _, _, second = completed_response(connection, args.model, identifier, "Reply with OK again.")
                report["turns"] = [first, second]
            report["accepted"] = True
            report["phase"] = "completed"
        finally:
            connection.close()
    except (ProbeError, websocket.WebSocketException, OSError) as error:
        report["failureType"] = type(error).__name__
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
