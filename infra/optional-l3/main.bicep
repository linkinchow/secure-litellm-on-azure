targetScope = 'resourceGroup'

param location string = resourceGroup().location
param storageAccountName string
param workspaceName string
param virtualNetworkName string
param privateEndpointSubnetName string
param aksOidcIssuer string
param administratorPrincipalId string
@allowed(['User', 'Group'])
param administratorPrincipalType string = 'Group'
@minLength(3)
@description('Customer notification email resolved from CFG ownerEmail, or an approved signed-in user mail.')
param alertEmail string
@description('Use the existing scope already linked to this VNet; empty creates a new scope.')
param existingMonitorScopeName string = ''
@description('Only the reviewed missing zone names; never adopt another RG zone without approval.')
param privateDnsZonesToCreate array = []
@description('Only zones without an equivalent link to this VNet.')
param privateDnsZonesToLink array = []
param workspaceAlreadyLinkedToMonitorScope bool = false
@description('Zone-redundant content storage and spool disks; set true when the AKS node pools span availability zones. An existing LRS account needs a separate redundancy conversion.')
param zoneRedundant bool = false

var prefix = 'l3-${storageAccountName}'
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${prefix}-alerts'
  location: 'global'
  properties: {
    groupShortName: 'L3Audit'
    enabled: true
    emailReceivers: [{
      name: 'l3-owner'
      emailAddress: alertEmail
      useCommonAlertSchema: true
    }]
  }
}
var monitorScopeName = empty(existingMonitorScopeName) ? '${prefix}-monitor' : existingMonitorScopeName
var zoneNames = [
  'privatelink.blob.${environment().suffixes.storage}'
  'privatelink.monitor.azure.com'
  'privatelink.oms.opinsights.azure.com'
  'privatelink.ods.opinsights.azure.com'
  'privatelink.agentsvc.azure-automation.net'
]

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}
resource network 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}
resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: network
  name: privateEndpointSubnetName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: '${prefix}-writer'
  location: location
}
resource federation 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: identity
  name: 'aks-l3-audit'
  properties: {
    issuer: aksOidcIssuer
    subject: 'system:serviceaccount:litellm:l3-audit'
    audiences: ['api://AzureADTokenExchange']
  }
}
resource account 'Microsoft.Storage/storageAccounts@2025-06-01' = {
  name: storageAccountName
  location: location
  kind: 'StorageV2'
  sku: { name: zoneRedundant ? 'Standard_ZRS' : 'Standard_LRS' }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    publicNetworkAccess: 'Disabled'
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
    isHnsEnabled: false
    networkAcls: { bypass: 'None', defaultAction: 'Deny' }
    encryption: {
      keySource: 'Microsoft.Storage'
      requireInfrastructureEncryption: true
      services: { blob: { enabled: true, keyType: 'Account' } }
    }
  }
}
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-06-01' = {
  parent: account
  name: 'default'
  properties: {
    isVersioningEnabled: false
    deleteRetentionPolicy: { enabled: false }
    containerDeleteRetentionPolicy: { enabled: false }
  }
}
resource content 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-06-01' = {
  parent: blobService
  name: 'l3-content'
  properties: { publicAccess: 'None' }
}
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2025-06-01' = {
  parent: account
  name: 'default'
  properties: {
    policy: {
      rules: [{
        name: 'normal-content-30-days'
        enabled: true
        type: 'Lifecycle'
        definition: {
          filters: { blobTypes: ['blockBlob'], prefixMatch: ['l3-content/content/'] }
          actions: { baseBlob: { delete: { daysAfterModificationGreaterThan: 30 } } }
        }
      }]
    }
  }
}
resource writerBlob 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: content
  name: guid(content.id, identity.id, 'writer')
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
resource adminData 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, administratorPrincipalId, 'data')
  properties: {
    principalId: administratorPrincipalId
    principalType: administratorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
  }
}
resource adminHolds 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, administratorPrincipalId, 'holds')
  properties: {
    principalId: administratorPrincipalId
    principalType: administratorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '17d1049b-9a84-46fb-8f53-869881c3d3ab')
  }
}
resource adminLogs 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: workspace
  name: guid(workspace.id, administratorPrincipalId, 'l3-query')
  properties: {
    principalId: administratorPrincipalId
    principalType: administratorPrincipalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '73c42c96-874c-492b-b04d-ab87d138a893')
  }
}
resource table 'Microsoft.OperationalInsights/workspaces/tables@2023-09-01' = {
  parent: workspace
  name: 'L3Audit_CL'
  properties: {
    plan: 'Analytics'
    retentionInDays: 30
    totalRetentionInDays: 30
    schema: {
      name: 'L3Audit_CL'
      columns: [
        { name: 'TimeGenerated', type: 'datetime' }
        { name: 'Record', type: 'dynamic' }
      ]
    }
  }
}
resource dce 'Microsoft.Insights/dataCollectionEndpoints@2023-03-11' = {
  name: '${prefix}-dce'
  location: location
  properties: { networkAcls: { publicNetworkAccess: 'Disabled' } }
}
resource dcr 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: '${prefix}-dcr'
  location: location
  properties: {
    dataCollectionEndpointId: dce.id
    streamDeclarations: {
      'Custom-L3Audit_CL': {
        columns: [
          { name: 'TimeGenerated', type: 'datetime' }
          { name: 'Record', type: 'dynamic' }
        ]
      }
    }
    destinations: { logAnalytics: [{ name: 'law', workspaceResourceId: workspace.id }] }
    dataFlows: [{
      streams: ['Custom-L3Audit_CL']
      destinations: ['law']
      transformKql: 'source'
      outputStream: 'Custom-L3Audit_CL'
    }]
  }
  dependsOn: [table]
}
resource writerLogs 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: dcr
  name: guid(dcr.id, identity.id)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '3913510d-42f4-4e42-8a64-420c390055eb')
  }
}
resource newScope 'Microsoft.Insights/privateLinkScopes@2021-07-01-preview' = if (empty(existingMonitorScopeName)) {
  name: monitorScopeName
  location: 'global'
  properties: { accessModeSettings: { ingestionAccessMode: 'PrivateOnly', queryAccessMode: 'PrivateOnly' } }
}
resource scope 'Microsoft.Insights/privateLinkScopes@2021-07-01-preview' existing = {
  name: monitorScopeName
}
resource scopeWorkspace 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = if (!workspaceAlreadyLinkedToMonitorScope) {
  parent: scope
  name: '${prefix}-workspace'
  properties: { linkedResourceId: workspace.id }
  dependsOn: [newScope]
}
resource scopeDce 'Microsoft.Insights/privateLinkScopes/scopedResources@2021-07-01-preview' = {
  parent: scope
  name: '${prefix}-dce'
  properties: { linkedResourceId: dce.id }
  dependsOn: [newScope]
}
resource createdZones 'Microsoft.Network/privateDnsZones@2024-06-01' = [for name in zoneNames: if (contains(privateDnsZonesToCreate, name)) {
  name: name
  location: 'global'
}]
resource zones 'Microsoft.Network/privateDnsZones@2024-06-01' existing = [for name in zoneNames: {
  name: name
}]
resource links 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = [for (name, i) in zoneNames: if (contains(privateDnsZonesToLink, name)) {
  parent: zones[i]
  name: '${prefix}-target'
  location: 'global'
  properties: { registrationEnabled: false, virtualNetwork: { id: network.id } }
  dependsOn: [createdZones]
}]
resource blobPe 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: '${prefix}-blob-pe'
  location: location
  properties: {
    subnet: { id: subnet.id }
    privateLinkServiceConnections: [{
      name: 'blob'
      properties: { privateLinkServiceId: account.id, groupIds: ['blob'] }
    }]
  }
}
resource blobDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: blobPe
  name: 'default'
  properties: { privateDnsZoneConfigs: [{ name: 'blob', properties: { privateDnsZoneId: zones[0].id } }] }
  dependsOn: [createdZones]
}
resource monitorPe 'Microsoft.Network/privateEndpoints@2024-07-01' = {
  name: '${prefix}-monitor-pe'
  location: location
  properties: {
    subnet: { id: subnet.id }
    privateLinkServiceConnections: [{
      name: 'azuremonitor'
      properties: { privateLinkServiceId: scope.id, groupIds: ['azuremonitor'] }
    }]
  }
  dependsOn: [newScope]
}
resource monitorDns 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-07-01' = {
  parent: monitorPe
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [for i in range(1, 4): {
      name: 'monitor-${i}', properties: { privateDnsZoneId: zones[i].id }
    }]
  }
  dependsOn: [createdZones]
}
resource alerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for item in [
  {
    name: 'delivery-risk'
    query: 'L3Audit_CL | where TimeGenerated > ago(10m) | where isnotempty(tostring(Record.risk))'
  }
  {
    name: 'heartbeat-missing'
    query: 'L3Audit_CL | where TimeGenerated > ago(10m) | where tostring(Record.path) == "/l3/health" | summarize n=count() | where n == 0'
  }
]: {
  name: '${prefix}-${item.name}'
  location: location
  properties: {
    displayName: '${prefix}-${item.name}'
    enabled: true
    severity: 2
    evaluationFrequency: 'PT5M'
    windowSize: 'PT10M'
    scopes: [workspace.id]
    criteria: {
      allOf: [{
        query: item.query
        timeAggregation: 'Count'
        operator: 'GreaterThan'
        threshold: 0
        failingPeriods: { numberOfEvaluationPeriods: 1, minFailingPeriodsToAlert: 1 }
      }]
    }
    actions: { actionGroups: [actionGroup.id] }
  }
  dependsOn: [table]
}]

output deploymentSettings object = {
  clientId: identity.properties.clientId
  storageUrl: account.properties.primaryEndpoints.blob
  dce: dce.properties.logsIngestion.endpoint
  dcr: dcr.properties.immutableId
  zoneRedundant: zoneRedundant
}
output workspaceId string = workspace.properties.customerId
output privateEndpoints array = [blobPe.id, monitorPe.id]
output alertActionGroupId string = actionGroup.id
