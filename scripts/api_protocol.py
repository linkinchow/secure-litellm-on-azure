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


def app_gateway_api_method_rules():
    """The same method gate in the Application Gateway WAF custom-rule schema."""
    variables = {"RequestMethod": "RequestMethod", "RequestUri": "RequestUri", "RequestHeader": "RequestHeaders"}
    operators = {"Equal": "Equal", "RegEx": "Regex"}
    rules = []
    for rule in api_method_rules():
        conditions = []
        for condition in rule["matchConditions"]:
            variable = {"variableName": variables[condition["matchVariable"]]}
            if "selector" in condition:
                variable["selector"] = condition["selector"]
            values = condition["matchValue"]
            if condition["matchVariable"] == "RequestUri":
                # Application Gateway matches RequestUri on the path and query only, never a scheme or host.
                values = [value.replace("(https://[^/?#]+)?", "") for value in values]
            converted = {"matchVariables": [variable], "operator": operators[condition["operator"]],
                         "negationConditon": condition.get("negateCondition", False), "matchValues": values}
            if "transforms" in condition:
                converted["transforms"] = condition["transforms"]
            conditions.append(converted)
        rules.append({"name": rule["name"], "priority": rule["priority"], "state": "Enabled",
                      "ruleType": "MatchRule", "action": "Block", "matchConditions": conditions})
    return rules
