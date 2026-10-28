@description('Globally unique Azure Managed Redis name.')
@minLength(1)
@maxLength(60)
param name string

@description('Azure region for Redis.')
param location string

@description('LiteLLM Workload Identity object ID used as the Redis Entra username.')
param workloadPrincipalId string

@description('Optional stable identity resource ID for first-deployment What-if. Empty preserves existing principal-based assignment names.')
param principalSourceResourceId string = ''

@description('Existing Log Analytics workspace resource ID.')
param logAnalyticsWorkspaceId string

@description('Azure Managed Redis SKU.')
param skuName string = 'Balanced_B0'

@description('Managed Redis high availability; Disabled has no SLA and is a customer-approved cost trade-off.')
@allowed([
  'Enabled'
  'Disabled'
])
param highAvailability string = 'Enabled'

@description('Resource tags.')
param tags object = {}

resource cluster 'Microsoft.Cache/redisEnterprise@2025-07-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: skuName
  }
  properties: {
    encryption: {}
    highAvailability: highAvailability
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Disabled'
  }
}

resource database 'Microsoft.Cache/redisEnterprise/databases@2025-07-01' = {
  parent: cluster
  name: 'default'
  properties: {
    accessKeysAuthentication: 'Disabled'
    clientProtocol: 'Encrypted'
    clusteringPolicy: 'NoCluster'
    evictionPolicy: 'AllKeysLRU'
    modules: []
    persistence: {
      aofEnabled: true
      aofFrequency: '1s'
      rdbEnabled: false
    }
    port: 10000
  }
}

resource accessPolicyAssignment 'Microsoft.Cache/redisEnterprise/databases/accessPolicyAssignments@2025-07-01' = {
  parent: database
  name: empty(principalSourceResourceId) ? workloadPrincipalId : uniqueString(database.id, principalSourceResourceId)
  properties: {
    accessPolicyName: 'default'
    user: {
      objectId: workloadPrincipalId
    }
  }
}

resource clusterDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: cluster
  name: 'send-managed-redis-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

resource databaseDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: database
  name: 'send-managed-redis-database-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        category: 'ConnectionEvents'
        enabled: true
      }
    ]
  }
}

output redis object = {
  id: cluster.id
  name: cluster.name
  databaseId: database.id
  hostName: cluster.properties.hostName
  port: database.properties.port
  entraUsername: workloadPrincipalId
}
