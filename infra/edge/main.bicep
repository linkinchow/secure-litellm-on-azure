targetScope = 'resourceGroup'

@description('Create only new Stage 9 edge resources. Does not create DNS records or deploy an ingress controller.')
param deployEdge bool = false

@description('Separate traffic gate. Keep false until private origin, WAF association and release evidence are reviewed.')
param enableApiTraffic bool = false

@description('Independent Admin traffic gate. Keep false until the source-IP allowlist, private origin and release evidence are reviewed.')
param enableAdminTraffic bool = false

@allowed(['dev', 'test', 'prod'])
param environmentName string = 'test'

@description('Environment domain supplied outside Git. API and Admin use separate Front Door endpoints.')
param baseDomain string = 'example.com'

type privateOriginConfiguration = {
  privateLinkServiceId: string
  privateLinkLocation: string
}

@description('Existing, reviewed API-only Private Link Service. Never use the Admin ingress or the legacy public gateway.')
param privateOrigin privateOriginConfiguration

@description('Existing, reviewed Admin-only Private Link Service. Never reuse the API frontend.')
param adminPrivateOrigin privateOriginConfiguration

@minLength(1)
@maxLength(32)
@description('Public client egress CIDRs allowed to reach the Admin domain. All other source socket addresses are blocked by the Admin WAF policy.')
param adminAllowedCidrs string[]

@description('Existing workspace for edge access, health and WAF diagnostics.')
param logAnalyticsWorkspaceName string

@allowed(['Detection', 'Prevention'])
param wafMode string = 'Detection'

@minValue(1)
@maxValue(100000)
@description('Per-client-IP rate threshold per minute; validate corporate NAT traffic before Prevention.')
param rateLimitPerMinute int = 600

@minValue(1)
@maxValue(10000)
@description('Admin per-client-IP rate threshold per minute after source-IP admission.')
param adminRateLimitPerMinute int = 120

@minValue(1)
@maxValue(10000)
@description('Per-client-IP threshold per minute for GET /models and /v1/models (Codex model discovery); 120 matches the legacy ingress 2 requests/second.')
param modelListRateLimitPerMinute int = 120

param tags object = {}

var suffix = uniqueString(resourceGroup().id, environmentName)
var apiHost = 'llm-api.${baseDomain}'
var adminHost = 'llm-admin.${baseDomain}'
var apiTrafficState = enableApiTraffic ? 'Enabled' : 'Disabled'
var adminTrafficState = enableAdminTraffic ? 'Enabled' : 'Disabled'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource profile 'Microsoft.Cdn/profiles@2025-04-15' = if (deployEdge) {
  name: 'afd-llm-${environmentName}-${suffix}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
  properties: { originResponseTimeoutSeconds: 240 }
}

resource endpoint 'Microsoft.Cdn/profiles/afdEndpoints@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'llm-api-${environmentName}-${suffix}'
  location: 'global'
  properties: { enabledState: apiTrafficState }
}

resource adminEndpoint 'Microsoft.Cdn/profiles/afdEndpoints@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'llm-admin-${environmentName}-${suffix}'
  location: 'global'
  properties: { enabledState: adminTrafficState }
}

resource domain 'Microsoft.Cdn/profiles/customDomains@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'llm-api'
  properties: {
    hostName: apiHost
    tlsSettings: {
      certificateType: 'ManagedCertificate'
      minimumTlsVersion: 'TLS12'
    }
  }
}

resource adminDomain 'Microsoft.Cdn/profiles/customDomains@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'llm-admin'
  properties: {
    hostName: adminHost
    tlsSettings: {
      certificateType: 'ManagedCertificate'
      minimumTlsVersion: 'TLS12'
    }
  }
}

resource originGroup 'Microsoft.Cdn/profiles/originGroups@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'private-api'
  properties: {
    sessionAffinityState: 'Disabled'
    healthProbeSettings: {
      probePath: '/readyz'
      probeProtocol: 'Https'
      probeRequestType: 'GET'
      probeIntervalInSeconds: 30
    }
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 0
    }
  }
}

resource adminOriginGroup 'Microsoft.Cdn/profiles/originGroups@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'private-admin'
  properties: {
    sessionAffinityState: 'Disabled'
    healthProbeSettings: {
      probePath: '/readyz'
      probeProtocol: 'Https'
      probeRequestType: 'GET'
      probeIntervalInSeconds: 30
    }
    loadBalancingSettings: {
      sampleSize: 4
      successfulSamplesRequired: 3
      additionalLatencyInMilliseconds: 0
    }
  }
}

resource origin 'Microsoft.Cdn/profiles/originGroups/origins@2025-04-15' = if (deployEdge) {
  parent: originGroup
  name: 'private-api'
  properties: {
    hostName: apiHost
    originHostHeader: apiHost
    httpsPort: 443
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
    priority: 1
    weight: 1000
    sharedPrivateLinkResource: {
      privateLink: { id: privateOrigin.privateLinkServiceId }
      privateLinkLocation: privateOrigin.privateLinkLocation
      requestMessage: 'Stage 9 API origin only; verify target profile and manually approve after review.'
    }
  }
}

resource adminOrigin 'Microsoft.Cdn/profiles/originGroups/origins@2025-04-15' = if (deployEdge) {
  parent: adminOriginGroup
  name: 'private-admin'
  properties: {
    hostName: adminHost
    originHostHeader: adminHost
    httpsPort: 443
    enabledState: 'Enabled'
    enforceCertificateNameCheck: true
    priority: 1
    weight: 1000
    sharedPrivateLinkResource: {
      privateLink: { id: adminPrivateOrigin.privateLinkServiceId }
      privateLinkLocation: adminPrivateOrigin.privateLinkLocation
      requestMessage: 'Stage 9 Admin origin only; verify target profile and manually approve after review.'
    }
  }
}

resource waf 'Microsoft.Network/frontDoorWebApplicationFirewallPolicies@2024-02-01' = if (deployEdge) {
  name: 'wafllm${environmentName}${suffix}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
  properties: {
    policySettings: {
      enabledState: 'Enabled'
      mode: wafMode
      requestBodyCheck: 'Enabled'
      logScrubbing: {
        state: 'Enabled'
        scrubbingRules: [for variable in ['RequestHeaderNames', 'RequestCookieNames', 'QueryStringArgNames', 'RequestBodyPostArgNames', 'RequestBodyJsonArgNames']: {
          matchVariable: variable
          selectorMatchOperator: 'EqualsAny'
          state: 'Enabled'
        }]
      }
    }
    managedRules: {
      managedRuleSets: [
        { ruleSetType: 'Microsoft_DefaultRuleSet', ruleSetVersion: '2.1', ruleSetAction: 'Block' }
        { ruleSetType: 'Microsoft_BotManagerRuleSet', ruleSetVersion: '1.1', ruleSetAction: 'Block' }
      ]
    }
    customRules: {
      rules: [
        {
          name: 'BlockUnsupportedApiMethods'
          priority: 10
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestMethod', operator: 'Equal', negateCondition: true, matchValue: ['POST', 'GET'] }]
        }
        {
          name: 'BlockNonResponsesGet'
          priority: 11
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [
            { matchVariable: 'RequestMethod', operator: 'Equal', matchValue: ['GET'] }
            { matchVariable: 'RequestUri', operator: 'RegEx', negateCondition: true, matchValue: ['^(https://[^/?#]+)?/(v1/)?(responses|models)(\\?.*)?$'] }
          ]
        }
        {
          name: 'BlockInvalidWebSocketUpgrade'
          priority: 12
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [
            { matchVariable: 'RequestMethod', operator: 'Equal', matchValue: ['GET'] }
            { matchVariable: 'RequestUri', operator: 'RegEx', matchValue: ['^(https://[^/?#]+)?/(v1/)?responses(\\?.*)?$'] }
            { matchVariable: 'RequestHeader', selector: 'Upgrade', operator: 'Equal', negateCondition: true, matchValue: ['websocket'], transforms: ['Lowercase'] }
          ]
        }
        {
          name: 'BlockInvalidWebSocketConnection'
          priority: 13
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [
            { matchVariable: 'RequestMethod', operator: 'Equal', matchValue: ['GET'] }
            { matchVariable: 'RequestUri', operator: 'RegEx', matchValue: ['^(https://[^/?#]+)?/(v1/)?responses(\\?.*)?$'] }
            { matchVariable: 'RequestHeader', selector: 'Connection', operator: 'RegEx', negateCondition: true, matchValue: ['(^|[, \\t])upgrade([, \\t]|$)'], transforms: ['Lowercase'] }
          ]
        }
        {
          name: 'BlockModelListNonGet'
          priority: 14
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [
            { matchVariable: 'RequestMethod', operator: 'Equal', negateCondition: true, matchValue: ['GET'] }
            { matchVariable: 'RequestUri', operator: 'RegEx', matchValue: ['^(https://[^/?#]+)?/(v1/)?models(\\?.*)?$'] }
          ]
        }
        {
          name: 'RateLimitModelList'
          priority: 15
          enabledState: 'Enabled'
          ruleType: 'RateLimitRule'
          rateLimitDurationInMinutes: 1
          rateLimitThreshold: modelListRateLimitPerMinute
          groupBy: [{ variableName: 'SocketAddr' }]
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestUri', operator: 'RegEx', matchValue: ['^(https://[^/?#]+)?/(v1/)?models(\\?.*)?$'] }]
        }
        {
          name: 'RateLimitApi'
          priority: 20
          enabledState: 'Enabled'
          ruleType: 'RateLimitRule'
          rateLimitDurationInMinutes: 1
          rateLimitThreshold: rateLimitPerMinute
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestUri', operator: 'BeginsWith', matchValue: ['/'] }]
        }
      ]
    }
  }
}

resource adminWaf 'Microsoft.Network/frontDoorWebApplicationFirewallPolicies@2024-02-01' = if (deployEdge) {
  name: 'wafllmadmin${environmentName}${suffix}'
  location: 'global'
  tags: tags
  sku: { name: 'Premium_AzureFrontDoor' }
  properties: {
    policySettings: {
      enabledState: 'Enabled'
      mode: 'Prevention'
      requestBodyCheck: 'Enabled'
      logScrubbing: {
        state: 'Enabled'
        scrubbingRules: [for variable in ['RequestHeaderNames', 'RequestCookieNames', 'QueryStringArgNames', 'RequestBodyPostArgNames', 'RequestBodyJsonArgNames']: {
          matchVariable: variable
          selectorMatchOperator: 'EqualsAny'
          state: 'Enabled'
        }]
      }
    }
    managedRules: {
      managedRuleSets: [
        { ruleSetType: 'Microsoft_DefaultRuleSet', ruleSetVersion: '2.1', ruleSetAction: 'Block' }
        { ruleSetType: 'Microsoft_BotManagerRuleSet', ruleSetVersion: '1.1', ruleSetAction: 'Block' }
      ]
    }
    customRules: {
      rules: [
        {
          name: 'BlockUnapprovedAdminSources'
          priority: 5
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [{
            matchVariable: 'SocketAddr'
            operator: 'IPMatch'
            negateCondition: true
            matchValue: adminAllowedCidrs
          }]
        }
        {
          name: 'BlockUnsafeMethods'
          priority: 10
          enabledState: 'Enabled'
          ruleType: 'MatchRule'
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestMethod', operator: 'Equal', matchValue: ['TRACE', 'TRACK'] }]
        }
        {
          name: 'RateLimitAdmin'
          priority: 20
          enabledState: 'Enabled'
          ruleType: 'RateLimitRule'
          rateLimitDurationInMinutes: 1
          rateLimitThreshold: adminRateLimitPerMinute
          action: 'Block'
          matchConditions: [{ matchVariable: 'RequestUri', operator: 'BeginsWith', matchValue: ['/'] }]
        }
      ]
    }
  }
}

resource securityPolicy 'Microsoft.Cdn/profiles/securityPolicies@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'api-waf'
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: { id: waf!.id }
      associations: [{ domains: [{ id: domain!.id }], patternsToMatch: ['/*'] }]
    }
  }
}

resource adminSecurityPolicy 'Microsoft.Cdn/profiles/securityPolicies@2025-04-15' = if (deployEdge) {
  parent: profile
  name: 'admin-waf'
  properties: {
    parameters: {
      type: 'WebApplicationFirewall'
      wafPolicy: { id: adminWaf!.id }
      associations: [{ domains: [{ id: adminDomain!.id }], patternsToMatch: ['/*'] }]
    }
  }
}

resource route 'Microsoft.Cdn/profiles/afdEndpoints/routes@2025-04-15' = if (deployEdge) {
  parent: endpoint
  name: 'api-only'
  properties: {
    enabledState: apiTrafficState
    customDomains: [{ id: domain!.id }]
    originGroup: { id: originGroup!.id }
    supportedProtocols: ['Https']
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Disabled'
    patternsToMatch: [
      '/chat/completions'
      '/v1/chat/completions'
      '/responses'
      '/v1/responses'
      '/embeddings'
      '/v1/embeddings'
      // Model discovery: the API WAF admits GET only and rate-limits it per client IP.
      '/models'
      '/v1/models'
    ]
    ruleSets: []
  }
  dependsOn: [origin, securityPolicy]
}

resource adminRoute 'Microsoft.Cdn/profiles/afdEndpoints/routes@2025-04-15' = if (deployEdge) {
  parent: adminEndpoint
  name: 'admin-ip-allowlist-only'
  properties: {
    enabledState: adminTrafficState
    customDomains: [{ id: adminDomain!.id }]
    originGroup: { id: adminOriginGroup!.id }
    supportedProtocols: ['Https']
    forwardingProtocol: 'HttpsOnly'
    httpsRedirect: 'Enabled'
    linkToDefaultDomain: 'Disabled'
    patternsToMatch: ['/*']
    ruleSets: []
  }
  dependsOn: [adminOrigin, adminSecurityPolicy]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployEdge) {
  scope: profile
  name: 'edge-security-and-health'
  properties: {
    workspaceId: workspace.id
    logs: [for category in ['FrontDoorAccessLog', 'FrontDoorHealthProbeLog', 'FrontDoorWebApplicationFirewallLog']: { category: category, enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output edge object = {
  provisioned: deployEdge
  trafficEnabled: deployEdge && enableApiTraffic
  apiTrafficEnabled: deployEdge && enableApiTraffic
  adminTrafficEnabled: deployEdge && enableAdminTraffic
  apiHost: apiHost
  adminHost: adminHost
  endpointHost: deployEdge ? endpoint!.properties.hostName : ''
  adminEndpointHost: deployEdge ? adminEndpoint!.properties.hostName : ''
  routeId: deployEdge ? route!.id : ''
  adminRouteId: deployEdge ? adminRoute!.id : ''
  adminWafId: deployEdge ? adminWaf!.id : ''
  adminAccessMode: 'SourceIpAllowlistAndNativeLogin'
  privateOrigin: privateOrigin
  adminPrivateOrigin: adminPrivateOrigin
  adminAllowedCidrs: adminAllowedCidrs
  adminRateLimitPerMinute: adminRateLimitPerMinute
  modelListRateLimitPerMinute: modelListRateLimitPerMinute
  profileId: deployEdge ? profile!.properties.frontDoorId : ''
}
