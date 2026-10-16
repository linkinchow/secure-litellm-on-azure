"""Configuration bridge only; optional L3 capture and delivery run in TypeScript."""

from scripts.customer_migration import require


def enabled(config):
    if "l3Audit" not in config:
        return False
    settings = config["l3Audit"]
    require(isinstance(settings, dict) and set(settings) == {"enabled"}, "l3Audit requires exactly enabled")
    require(settings["enabled"] is True, "Remove l3Audit to restore the approved native baseline")
    require(config.get("application", {}).get("authentication", {}).get("mode") == "native",
            "Optional TypeScript L3 currently requires the native gateway")
    require(config.get("contentAudit", {}).get("mode") == "native",
            "Optional L3 requires a retained native audit rollback policy")
    return True


def upstream(config, plane):
    require(plane in {"api", "admin"}, "Unknown ingress plane")
    return ("http://l3-audit.litellm.svc.cluster.local:8080"
            if plane == "api" and enabled(config)
            else "http://litellm.litellm.svc.cluster.local:4000")
