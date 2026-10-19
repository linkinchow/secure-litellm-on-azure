#!/usr/bin/env python3
"""Bounded HTTPS gateway checks, not a load test or DDoS acceptance test."""

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise argparse.ArgumentTypeError("Use an approved HTTPS origin without credentials, path or query")
    try:
        port = parsed.port
    except ValueError:
        raise argparse.ArgumentTypeError("Invalid HTTPS origin port") from None
    if port not in {None, 443}:
        raise argparse.ArgumentTypeError("Only HTTPS port 443 is supported")
    return value.rstrip("/")


def cases(admin_url=None, baseline_model=None):
    items = [
        {"name": "missing-key", "method": "POST", "path": "/v1/responses",
         "body": {"model": "security-probe-no-model", "input": "synthetic validation"},
         "expected": [401, 403], "goal": "authentication"},
        {"name": "invalid-key", "method": "POST", "path": "/v1/responses",
         "body": {"model": "security-probe-no-model", "input": "synthetic validation"},
         "syntheticKey": True, "expected": [401, 403], "goal": "authentication"},
        {"name": "api-ui-isolation", "method": "POST", "path": "/ui/",
         "expected": [403, 404, 405], "goal": "path-isolation"},
        {"name": "api-user-info-isolation", "method": "POST", "path": "/user/info",
         "expected": [403, 404, 405], "goal": "path-isolation"},
        {"name": "api-model-list-isolation", "method": "POST", "path": "/v1/models",
         "expected": [403, 404, 405], "goal": "path-isolation"},
        {"name": "api-get-method", "method": "GET", "path": "/v1/responses",
         "expected": [403, 404, 405], "goal": "method-restriction"},
        {"name": "api-trace-method", "method": "TRACE", "path": "/v1/responses",
         "expected": [403, 404, 405], "goal": "method-restriction"},
        {"name": "waf-xss-sample", "method": "POST",
         "path": "/v1/responses?security_probe=%3Cscript%3Ealert%281%29%3C%2Fscript%3E",
         "body": {"model": "security-probe-no-model", "input": "synthetic validation"},
         "expected": [401, 403], "goal": "waf-log-correlation"},
        {"name": "waf-sqli-sample", "method": "POST",
         "path": "/v1/responses?security_probe=%27%20OR%20%271%27%3D%271",
         "body": {"model": "security-probe-no-model", "input": "synthetic validation"},
         "expected": [401, 403], "goal": "waf-log-correlation"},
    ]
    if admin_url:
        items.extend([
            {"name": "admin-ui-access", "origin": admin_url, "method": "GET", "path": "/ui/",
             "expected": [200, 301, 302, 303, 307, 308, 403], "goal": "admin-source-observation"},
            {"name": "admin-trace-method", "origin": admin_url, "method": "TRACE", "path": "/ui/",
             "expected": [403, 404, 405], "goal": "method-restriction"},
        ])
    if baseline_model:
        items.append({"name": "authorized-baseline", "method": "POST", "path": "/v1/responses",
                      "body": {"model": baseline_model, "input": "Reply with OK.",
                               "max_output_tokens": 64},
                      "realKey": True, "expected": [200], "goal": "authorized-inference"})
    return items


def classification(case, status, blocked_page):
    goal = case["goal"]
    if goal == "waf-log-correlation":
        return "edge-rejection-suspected-log-required" if blocked_page else "waf-evidence-required"
    if goal == "admin-source-observation":
        return "admin-source-and-rule-log-required"
    if status not in case["expected"]:
        return "unexpected-status"
    if blocked_page:
        return "edge-rejection-suspected-log-required"
    if goal == "authentication":
        return "authentication-denial-observed-layer-unverified"
    if goal == "authorized-inference":
        return "authorized-http-success-observed"
    return "rejection-observed-layer-unverified"


def request_case(opener, origin, case, key=None):
    headers = {"Content-Type": "application/json", "User-Agent": "LLMGW-BoundedSecurityProbe/1.0"}
    headers.update(case.get("headers", {}))
    if case.get("syntheticKey"):
        headers["Authorization"] = "Bearer sk-security-probe-intentionally-invalid"
    if case.get("realKey"):
        if not key:
            raise ValueError("Authorized baseline requires a local restricted Virtual Key")
        headers["Authorization"] = "Bearer " + key
    data = json.dumps(case["body"]).encode() if "body" in case else None
    request = Request(case.get("origin", origin) + case["path"], data=data,
                      headers=headers, method=case["method"])
    started = datetime.now(timezone.utc).isoformat()
    result = {"name": case["name"], "goal": case["goal"], "method": case["method"],
              "path": case["path"].split("?", 1)[0], "startedAt": started,
              "expectedStatuses": case["expected"], "status": None,
              "trackingReference": None, "result": "transport-error"}
    try:
        try:
            response = opener.open(request, timeout=15)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            body = response.read(8192)
            reference = response.headers.get("X-Azure-Ref", "")
            if re.fullmatch(r"[A-Za-z0-9_+/=-]{1,200}", reference):
                result["trackingReference"] = reference
            blocked_page = b"the request is blocked" in body.lower() and b"<" in body
            result.update(status=status, expectedStatusObserved=status in case["expected"],
                          result=classification(case, status, blocked_page))
    except (URLError, OSError, TimeoutError):
        # TLS, proxy and network errors may contain URLs or credentials: retain no raw exception.
        result["error"] = "HTTPS request failed; verify TLS, DNS, proxy and connectivity"
    result["completedAt"] = datetime.now(timezone.utc).isoformat()
    return result


def write_report(path, document):
    if path.exists() or path.is_symlink():
        raise ValueError("Report path already exists; choose a new path")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(document, target, indent=2)
        target.write("\n")


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--api-url", required=True, type=base_url)
    result.add_argument("--admin-url", type=base_url)
    result.add_argument("--confirm-authorized-target", action="store_true",
                        help="Confirm permission to run this bounded suite against these targets")
    result.add_argument("--baseline-model", help="Opt in to ONE billed inference with a local restricted v-key")
    result.add_argument("--key-env", default="LITELLM_API_KEY")
    result.add_argument("--prompt-key", action="store_true", help="Read the baseline v-key using a hidden prompt")
    result.add_argument("--output-json", required=True, type=Path)
    result.add_argument("--direct", action="store_true", help="Ignore environment proxies (not VPN/TUN routing)")
    return result


def run(args):
    if not args.confirm_authorized_target:
        raise ValueError("Explicit authorized-target confirmation is required")
    if args.prompt_key and not args.baseline_model:
        raise ValueError("A key is only used for the optional authorized baseline")
    if args.baseline_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.baseline_model):
        raise ValueError("Baseline model must be an approved plain model-group name")
    if args.admin_url and args.api_url == args.admin_url:
        raise ValueError("API and Admin origins must be separate")
    if args.output_json.exists() or args.output_json.is_symlink():
        raise ValueError("Report path already exists; choose a new path")
    key = None
    if args.baseline_model:
        key = getpass.getpass("Restricted LiteLLM Virtual Key: ") if args.prompt_key else os.environ.get(args.key_env)
        if not key or len(key) > 25000 or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise ValueError("Baseline requires a nonempty, valid local v-key; never use the Master Key")
    handlers = [NoRedirect()]
    if args.direct:
        handlers.append(ProxyHandler({}))
    opener = build_opener(*handlers)
    started = datetime.now(timezone.utc).isoformat()
    observations = []
    selected = cases(args.admin_url, args.baseline_model)
    for index, case in enumerate(selected):
        if index:
            time.sleep(2)
        observation = request_case(opener, args.api_url, case, key)
        observations.append(observation)
        print(json.dumps({"case": observation["name"], "status": observation["status"],
                          "result": observation["result"]}), flush=True)
        if observation["status"] is None or observation["status"] == 429 or (
                observation["status"] is not None and observation["status"] >= 500):
            print("Stopped after transport, throttling or server failure; no retry.", file=sys.stderr)
            break
    report = {"schemaVersion": 1, "operation": "bounded-edge-security-observations",
              "startedAt": started, "completedAt": datetime.now(timezone.utc).isoformat(),
              "apiOrigin": args.api_url, "adminOrigin": args.admin_url,
              "requestCount": len(observations), "plannedRequestCount": len(selected),
              "completed": len(observations) == len(selected), "observations": observations,
              "wafMode": "not-queried-use-live-policy-and-correlated-logs",
              "rateLimitTested": False, "ddosTested": False,
              "credentialLogged": False, "rawResponseLogged": False,
              "notes": ["HTTP status does not establish the enforcing layer.",
                        "Detection-mode WAF matches are not proof of prevention.",
                        "Small samples do not establish managed-rule coverage or DDoS protection.",
                        "Admin results depend on actual browser/proxy/IPv4/IPv6 egress.",
                        "Authorized baseline is optional and may incur model charges."]}
    write_report(args.output_json, report)
    return 0 if report["completed"] and all(
        item.get("expectedStatusObserved") is True for item in observations) else 1


def main(argv=None):
    try:
        return run(parser().parse_args(argv))
    except (ValueError, OSError, EOFError):
        print("Probe setup/report failed; check target approval, arguments, local key and output path. "
              "Credentials and raw errors are withheld.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
