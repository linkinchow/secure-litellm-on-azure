"""Reviewed method gate for Responses WebSocket handshakes, never an Allow rule."""


def api_method_rules():
    get = {"matchVariable": "RequestMethod", "operator": "Equal", "matchValue": ["GET"]}
    specifications = [
        ("BlockUnsupportedApiMethods", 10, [
            {"matchVariable": "RequestMethod", "operator": "Equal", "negateCondition": True,
             "matchValue": ["POST", "GET"]},
        ]),
        ("BlockNonResponsesGet", 11, [
            dict(get),
            {"matchVariable": "RequestUri", "operator": "RegEx", "negateCondition": True,
             "matchValue": [r"^(https://[^/?#]+)?/(v1/)?responses(\?.*)?$"]},
        ]),
        ("BlockInvalidWebSocketUpgrade", 12, [
            dict(get),
            {"matchVariable": "RequestHeader", "selector": "Upgrade", "operator": "Equal",
             "negateCondition": True, "matchValue": ["websocket"], "transforms": ["Lowercase"]},
        ]),
        ("BlockInvalidWebSocketConnection", 13, [
            dict(get),
            {"matchVariable": "RequestHeader", "selector": "Connection", "operator": "RegEx",
             "negateCondition": True, "matchValue": [r"(^|[, \t])upgrade([, \t]|$)"], "transforms": ["Lowercase"]},
        ]),
    ]
    return [
        {"name": name, "priority": priority, "enabledState": "Enabled",
         "ruleType": "MatchRule", "action": "Block", "matchConditions": conditions}
        for name, priority, conditions in specifications
    ]
