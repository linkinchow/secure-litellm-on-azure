targetScope = 'resourceGroup'

// Optional Stage 9 public entry through Application Gateway WAF_v2, used instead of Front Door when
// parameters.edge.type is applicationGateway. The gateway reaches the existing private ingress load
// balancers directly inside the target VNet, so no Private Link Service is involved.

@description('Create only new Stage 9 Application Gateway resources. Does not create DNS records or change the private ingress.')
param deployEdge bool = false

@description('API traffic gate. While false the API listener uses the block-all gate WAF policy.')
param enableApiTraffic bool = false

@description('Admin traffic gate. While false the Admin listener uses the block-all gate WAF policy.')
param enableAdminTraffic bool = false

@allowed(['dev', 'test', 'prod'])
param environmentName string = 'test'

@description('Environment domain supplied outside Git. API and Admin use separate listeners.')
param baseDomain string = 'example.com'

param location string = resourceGroup().location

@minLength(1)
@maxLength(32)
@description('Public client egress CIDRs allowed to reach the Admin listener. All other client addresses are blocked by the Admin WAF policy.')
param adminAllowedCidrs string[]

@description('Existing workspace for access, performance and firewall logs.')
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

@description('Existing target VNet that holds the private ingress subnet.')
param virtualNetworkName string = 'litellm-security-vnet'

@description('Dedicated Application Gateway subnet, created by this template.')
param subnetName string = 'snet-appgw'

param subnetPrefix string = '10.30.5.0/24'

@description('True when the subscription has EnableApplicationGatewayNetworkIsolation registered; the subnet must then be delegated.')
param networkIsolation bool = false

@description('Private API/Admin ingress load balancer addresses from the verified Stage 4 receipt.')
param backendAddresses object = { api: '', admin: '' }

@description('Certificate Vault that holds the PKCS#12 copies of the ingress certificates.')
param certificateVaultName string = 'kvcert-example'

param apiCertificateSecretName string = 'api-tls-pfx'

param adminCertificateSecretName string = 'admin-tls-pfx'

param availabilityZones string[] = ['1', '2', '3']

@minValue(0)
@maxValue(125)
param minCapacity int = 2

@minValue(2)
@maxValue(125)
param maxCapacity int = 10

@minValue(60)
@maxValue(86400)
@description('Seconds the gateway waits for the private ingress. Traefik and LiteLLM keep their own limits.')
param requestTimeoutSeconds int = 900

@description('Skip the managed rules for the JSON fields that carry prompt text. Code and SQL in prompts match SQL injection, XSS and command rules; paths, headers and other fields stay inspected.')
param wafExcludePromptFields bool = false

param tags object = {}

var apiHost = 'llm-api.${baseDomain}'
var adminHost = 'llm-admin.${baseDomain}'
var hosts = { api: apiHost, admin: adminHost }
var planes = ['api', 'admin']
var gatewayName = 'agw-llm-${environmentName}'
var gatewayId = resourceId('Microsoft.Network/applicationGateways', gatewayName)
var identityName = 'id-agw-llm-${environmentName}'
var apiWafName = 'wafagw-llm-api-${environmentName}'
var adminWafName = 'wafagw-llm-admin-${environmentName}'
var gateWafName = 'wafagw-llm-gate-${environmentName}'
var apiWafId = resourceId('Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies', apiWafName)
var adminWafId = resourceId('Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies', adminWafName)
var gateWafId = resourceId('Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies', gateWafName)
var publicIpId = resourceId('Microsoft.Network/publicIPAddresses', 'pip-agw-llm-${environmentName}')
// Not a secret: like the Front Door ID it only proves the request passed this gateway.
var edgeHeader = 'X-LLMGW-Edge-Id'
var edgeId = guid(resourceGroup().id, environmentName, 'llmgw-appgw-edge-identity')
var apiPaths = ['/chat/completions', '/v1/chat/completions', '/responses', '/v1/responses', '/embeddings', '/v1/embeddings']
var zones = empty(availabilityZones) ? null : availabilityZones
// The WAF flattens JSON bodies into argument names such as messages.content.
var promptFields = ['messages', 'system', 'input', 'instructions', 'prompt']
var secretsUserRole = '4633458b-17de-408a-b874-0445c86b69e6'
var scrubbingRules = [for variable in ['RequestHeaderNames', 'RequestCookieNames', 'RequestArgNames', 'RequestPostArgNames', 'RequestJSONArgNames']: {
  matchVariable: variable
  selectorMatchOperator: 'EqualsAny'
  state: 'Enabled'
}]
var policySettings = {
  state: 'Enabled'
  requestBodyCheck: true
  // Long prompts are normal: inspect the first 2 MB and pass larger bodies instead of rejecting them.
  requestBodyInspectLimitInKB: 2048
  requestBodyEnforcement: false
  fileUploadEnforcement: true
  fileUploadLimitInMb: 100
  logScrubbing: { state: 'Enabled', scrubbingRules: scrubbingRules }
}
var managedRuleSets = [
  { ruleSetType: 'Microsoft_DefaultRuleSet', ruleSetVersion: '2.1' }
  { ruleSetType: 'Microsoft_BotManagerRuleSet', ruleSetVersion: '1.0' }
]

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: virtualNetworkName
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: certificateVaultName
}

resource apiSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: apiCertificateSecretName
}

resource adminSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: vault
  name: adminCertificateSecretName
}

resource nsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = if (deployEdge) {
  name: 'nsg-agw-llm-${environmentName}'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowHttpsListeners'
        properties: { priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'Internet', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '443' }
      }
      {
        name: 'AllowGatewayManager'
        properties: { priority: 110, direction: 'Inbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'GatewayManager', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '65200-65535' }
      }
      {
        name: 'AllowAzureLoadBalancer'
        properties: { priority: 120, direction: 'Inbound', access: 'Allow', protocol: '*', sourceAddressPrefix: 'AzureLoadBalancer', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*' }
      }
    ]
  }
}

// The NSG is part of the subnet create so the subnet never exists without it. No route table: the
// gateway must keep direct Internet egress for its control plane.
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = if (deployEdge) {
  parent: vnet
  name: subnetName
  properties: {
    addressPrefix: subnetPrefix
    networkSecurityGroup: { id: nsg.id }
    delegations: networkIsolation ? [{ name: 'applicationGateways', properties: { serviceName: 'Microsoft.Network/applicationGateways' } }] : []
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = if (deployEdge) {
  name: 'pip-agw-llm-${environmentName}'
  location: location
  tags: tags
  sku: { name: 'Standard' }
  zones: zones
  properties: {
    publicIPAllocationMethod: 'Static'
    // Default 4 minutes silently drops clients still waiting for a non-streaming answer.
    idleTimeoutInMinutes: 30
    dnsSettings: { domainNameLabel: 'llm-${environmentName}-gw', domainNameLabelScope: 'ResourceGroupReuse' }
  }
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = if (deployEdge) {
  name: identityName
  location: location
  tags: tags
}

// Read access to the two PKCS#12 secrets only, not to the PEM or ACME state secrets.
resource apiSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployEdge) {
  name: guid(apiSecret.id, resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', identityName), secretsUserRole)
  scope: apiSecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRole)
    principalId: identity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource adminSecretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployEdge) {
  name: guid(adminSecret.id, resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', identityName), secretsUserRole)
  scope: adminSecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRole)
    principalId: identity!.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource apiWaf 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = if (deployEdge) {
  name: apiWafName
  location: location
  tags: tags
  properties: {
    policySettings: union(policySettings, { mode: wafMode })
    managedRules: {
      exclusions: wafExcludePromptFields ? map(promptFields, field => { matchVariable: 'RequestArgNames', selectorMatchOperator: 'StartsWith', selector: field }) : []
      managedRuleSets: managedRuleSets
    }
    customRules: [
      {
        name: 'BlockUnsupportedApiMethods'
        priority: 10
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [{ matchVariables: [{ variableName: 'RequestMethod' }], operator: 'Equal', negationConditon: true, matchValues: ['POST', 'GET'] }]
      }
      {
        name: 'BlockNonResponsesGet'
        priority: 11
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [
          { matchVariables: [{ variableName: 'RequestMethod' }], operator: 'Equal', negationConditon: false, matchValues: ['GET'] }
          { matchVariables: [{ variableName: 'RequestUri' }], operator: 'Regex', negationConditon: true, matchValues: ['^/(v1/)?responses(\\?.*)?$'] }
        ]
      }
      {
        name: 'BlockInvalidWebSocketUpgrade'
        priority: 12
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [
          { matchVariables: [{ variableName: 'RequestMethod' }], operator: 'Equal', negationConditon: false, matchValues: ['GET'] }
          { matchVariables: [{ variableName: 'RequestHeaders', selector: 'Upgrade' }], operator: 'Equal', negationConditon: true, matchValues: ['websocket'], transforms: ['Lowercase'] }
        ]
      }
      {
        name: 'BlockInvalidWebSocketConnection'
        priority: 13
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [
          { matchVariables: [{ variableName: 'RequestMethod' }], operator: 'Equal', negationConditon: false, matchValues: ['GET'] }
          { matchVariables: [{ variableName: 'RequestHeaders', selector: 'Connection' }], operator: 'Regex', negationConditon: true, matchValues: ['(^|[, \\t])upgrade([, \\t]|$)'], transforms: ['Lowercase'] }
        ]
      }
      {
        name: 'RateLimitApi'
        priority: 20
        state: 'Enabled'
        ruleType: 'RateLimitRule'
        rateLimitDuration: 'OneMin'
        rateLimitThreshold: rateLimitPerMinute
        groupByUserSession: [{ groupByVariables: [{ variableName: 'ClientAddr' }] }]
        action: 'Block'
        matchConditions: [{ matchVariables: [{ variableName: 'RequestUri' }], operator: 'BeginsWith', negationConditon: false, matchValues: ['/'] }]
      }
    ]
  }
}

resource adminWaf 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = if (deployEdge) {
  name: adminWafName
  location: location
  tags: tags
  properties: {
    // Always Prevention: the source-IP allowlist is the Admin admission control, not a tuning signal.
    policySettings: union(policySettings, { mode: 'Prevention' })
    managedRules: { managedRuleSets: managedRuleSets }
    customRules: [
      {
        name: 'BlockUnapprovedAdminSources'
        priority: 5
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [{ matchVariables: [{ variableName: 'RemoteAddr' }], operator: 'IPMatch', negationConditon: true, matchValues: adminAllowedCidrs }]
      }
      {
        name: 'BlockUnsafeMethods'
        priority: 10
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [{ matchVariables: [{ variableName: 'RequestMethod' }], operator: 'Equal', negationConditon: false, matchValues: ['TRACE', 'TRACK'] }]
      }
      {
        name: 'RateLimitAdmin'
        priority: 20
        state: 'Enabled'
        ruleType: 'RateLimitRule'
        rateLimitDuration: 'OneMin'
        rateLimitThreshold: adminRateLimitPerMinute
        groupByUserSession: [{ groupByVariables: [{ variableName: 'ClientAddr' }] }]
        action: 'Block'
        matchConditions: [{ matchVariables: [{ variableName: 'RequestUri' }], operator: 'BeginsWith', negationConditon: false, matchValues: ['/'] }]
      }
    ]
  }
}

// A separate always-Prevention policy keeps a closed plane closed even while the API policy runs in Detection.
resource gateWaf 'Microsoft.Network/ApplicationGatewayWebApplicationFirewallPolicies@2024-05-01' = if (deployEdge) {
  name: gateWafName
  location: location
  tags: tags
  properties: {
    policySettings: union(policySettings, { mode: 'Prevention' })
    managedRules: { managedRuleSets: managedRuleSets }
    customRules: [
      {
        name: 'TrafficDisabled'
        priority: 1
        state: 'Enabled'
        ruleType: 'MatchRule'
        action: 'Block'
        matchConditions: [{ matchVariables: [{ variableName: 'RemoteAddr' }], operator: 'IPMatch', negationConditon: false, matchValues: ['0.0.0.0/0'] }]
      }
    ]
  }
}

resource gateway 'Microsoft.Network/applicationGateways@2024-05-01' = if (deployEdge) {
  name: gatewayName
  location: location
  tags: tags
  zones: zones
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    sku: { name: 'WAF_v2', tier: 'WAF_v2' }
    autoscaleConfiguration: { minCapacity: minCapacity, maxCapacity: maxCapacity }
    // HTTP/2 client connections have a fixed 180-second idle timeout; HTTP/1.1 follows the public IP's 30 minutes.
    enableHttp2: false
    firewallPolicy: { id: gateWafId }
    // Response buffering off lets SSE events through as they arrive.
    globalConfiguration: { enableRequestBuffering: true, enableResponseBuffering: false }
    sslPolicy: { policyType: 'Predefined', policyName: 'AppGwSslPolicy20220101S' }
    gatewayIPConfigurations: [{ name: 'gateway', properties: { subnet: { id: subnet.id } } }]
    // Versionless, so renewed certificates are picked up without redeploying.
    sslCertificates: [for plane in planes: {
      name: plane
      properties: { keyVaultSecretId: 'https://${certificateVaultName}${environment().suffixes.keyvaultDns}/secrets/${plane == 'api' ? apiCertificateSecretName : adminCertificateSecretName}' }
    }]
    frontendIPConfigurations: [{ name: 'public', properties: { publicIPAddress: { id: publicIp.id } } }]
    frontendPorts: [{ name: 'https', properties: { port: 443 } }]
    backendAddressPools: [
      { name: 'api', properties: { backendAddresses: [{ ipAddress: backendAddresses.api }] } }
      { name: 'admin', properties: { backendAddresses: [{ ipAddress: backendAddresses.admin }] } }
      // Unlisted API paths land here and get 502, matching the Front Door route path list.
      { name: 'deny', properties: { backendAddresses: [] } }
    ]
    probes: [for plane in planes: {
      name: plane
      properties: {
        protocol: 'Https'
        host: hosts[plane]
        path: '/readyz'
        interval: 30
        timeout: 30
        unhealthyThreshold: 3
        pickHostNameFromBackendHttpSettings: false
        match: { statusCodes: ['200'] }
      }
    }]
    backendHttpSettingsCollection: [for plane in planes: {
      name: plane
      properties: {
        port: 443
        protocol: 'Https'
        cookieBasedAffinity: 'Disabled'
        requestTimeout: requestTimeoutSeconds
        hostName: hosts[plane]
        pickHostNameFromBackendAddress: false
        probe: { id: '${gatewayId}/probes/${plane}' }
      }
    }]
    httpListeners: [for plane in planes: {
      name: plane
      properties: {
        protocol: 'Https'
        hostName: hosts[plane]
        requireServerNameIndication: true
        frontendIPConfiguration: { id: '${gatewayId}/frontendIPConfigurations/public' }
        frontendPort: { id: '${gatewayId}/frontendPorts/https' }
        sslCertificate: { id: '${gatewayId}/sslCertificates/${plane}' }
        firewallPolicy: { id: plane == 'api' ? (enableApiTraffic ? apiWafId : gateWafId) : (enableAdminTraffic ? adminWafId : gateWafId) }
      }
    }]
    // Overwrites any client-supplied value; the private ingress only routes requests carrying it.
    rewriteRuleSets: [
      {
        name: 'edge-identity'
        properties: {
          rewriteRules: [
            {
              name: 'set-edge-identity'
              ruleSequence: 100
              conditions: []
              actionSet: { requestHeaderConfigurations: [{ headerName: edgeHeader, headerValue: edgeId }], responseHeaderConfigurations: [] }
            }
          ]
        }
      }
    ]
    urlPathMaps: [
      {
        name: 'api-paths'
        properties: {
          defaultBackendAddressPool: { id: '${gatewayId}/backendAddressPools/deny' }
          defaultBackendHttpSettings: { id: '${gatewayId}/backendHttpSettingsCollection/api' }
          pathRules: [
            {
              name: 'inference'
              properties: {
                paths: apiPaths
                backendAddressPool: { id: '${gatewayId}/backendAddressPools/api' }
                backendHttpSettings: { id: '${gatewayId}/backendHttpSettingsCollection/api' }
                rewriteRuleSet: { id: '${gatewayId}/rewriteRuleSets/edge-identity' }
              }
            }
          ]
        }
      }
    ]
    requestRoutingRules: [
      {
        name: 'api'
        properties: {
          ruleType: 'PathBasedRouting'
          priority: 100
          httpListener: { id: '${gatewayId}/httpListeners/api' }
          urlPathMap: { id: '${gatewayId}/urlPathMaps/api-paths' }
        }
      }
      {
        name: 'admin'
        properties: {
          ruleType: 'Basic'
          priority: 110
          httpListener: { id: '${gatewayId}/httpListeners/admin' }
          backendAddressPool: { id: '${gatewayId}/backendAddressPools/admin' }
          backendHttpSettings: { id: '${gatewayId}/backendHttpSettingsCollection/admin' }
          rewriteRuleSet: { id: '${gatewayId}/rewriteRuleSets/edge-identity' }
        }
      }
    ]
  }
  dependsOn: [apiSecretReader, adminSecretReader, apiWaf, adminWaf, gateWaf]
}

resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (deployEdge) {
  scope: gateway
  name: 'edge-security-and-health'
  properties: {
    workspaceId: workspace.id
    logAnalyticsDestinationType: 'Dedicated'
    logs: [{ categoryGroup: 'allLogs', enabled: true }]
    metrics: [{ category: 'AllMetrics', enabled: true }]
  }
}

output edge object = {
  edgeType: 'applicationGateway'
  provisioned: deployEdge
  trafficEnabled: deployEdge && enableApiTraffic
  apiTrafficEnabled: deployEdge && enableApiTraffic
  adminTrafficEnabled: deployEdge && enableAdminTraffic
  apiHost: apiHost
  adminHost: adminHost
  gatewayId: deployEdge ? gatewayId : ''
  publicIpId: deployEdge ? publicIpId : ''
  publicIpAddress: deployEdge ? publicIp!.properties.ipAddress : ''
  publicIpFqdn: deployEdge ? publicIp!.properties.dnsSettings.fqdn : ''
  edgeHeader: edgeHeader
  edgeId: edgeId
  apiWafId: deployEdge ? apiWafId : ''
  adminWafId: deployEdge ? adminWafId : ''
  gateWafId: deployEdge ? gateWafId : ''
  adminAccessMode: 'SourceIpAllowlistAndNativeLogin'
  adminAllowedCidrs: adminAllowedCidrs
  adminRateLimitPerMinute: adminRateLimitPerMinute
  rateLimitPerMinute: rateLimitPerMinute
  backendAddresses: backendAddresses
  subnetPrefix: subnetPrefix
}
