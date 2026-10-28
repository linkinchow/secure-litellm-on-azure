@description('Existing LiteLLM security VNet name.')
param virtualNetworkName string

@description('Resource tags.')
param tags object = {}

@description('Configure Stage 5 private DNS zones and VNet links.')
param configureStage5Zones bool = false

@description('Create the Key Vault private DNS zone. Keep false when a centrally managed zone already exists.')
param createKeyVaultZone bool = false

@description('Manage the Key Vault VNet link only when no earlier component or DNS Owner manages it.')
param configureKeyVaultLink bool = true

@description('Create the PostgreSQL private DNS zone. Keep false when a centrally managed zone already exists.')
param createPostgresqlZone bool = false

@description('Create the Azure Managed Redis private DNS zone when it does not already exist.')
param createManagedRedisZone bool = true

@description('Approved Runner VNet resource ID for Stage 5 private data-plane operations.')
param runnerVirtualNetworkId string = ''

@description('Create Runner links for the Stage 5 private DNS zones.')
param configureRunnerLinks bool = false

resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: virtualNetworkName
}

resource acrZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.azurecr.io'
  location: 'global'
  tags: tags
}

resource acrLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: acrZone
  name: '${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

@description('Resolve Azure OpenAI names missing from the private zone over public DNS; needed when some accounts are reached without a private endpoint.')
param azureOpenAIFallbackToInternet bool = false

resource azureOpenAIZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: 'privatelink.openai.azure.com'
  location: 'global'
  tags: tags
}

resource azureOpenAILink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: azureOpenAIZone
  name: '${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    resolutionPolicy: azureOpenAIFallbackToInternet ? 'NxDomainRedirect' : 'Default'
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
}

var keyVaultZoneName = 'privatelink.vaultcore.azure.net'
var postgresqlZoneName = 'privatelink.postgres.database.azure.com'
var managedRedisZoneName = 'privatelink.redis.azure.net'
var targetVirtualNetworkId = virtualNetwork.id
var createRunnerLinks = configureStage5Zones && configureRunnerLinks && !empty(runnerVirtualNetworkId) && toLower(runnerVirtualNetworkId) != toLower(targetVirtualNetworkId)
var runnerLinkName = 'stage5-runner-${uniqueString(toLower(runnerVirtualNetworkId))}'

resource keyVaultZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (configureStage5Zones && createKeyVaultZone) {
  name: keyVaultZoneName
  location: 'global'
  tags: tags
}

resource keyVaultLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (configureStage5Zones && configureKeyVaultLink) {
  #disable-next-line use-parent-property
  name: '${keyVaultZoneName}/${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  dependsOn: [
    keyVaultZone
  ]
}

resource keyVaultRunnerLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (createRunnerLinks && configureKeyVaultLink) {
  #disable-next-line use-parent-property
  name: '${keyVaultZoneName}/${runnerLinkName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: runnerVirtualNetworkId
    }
  }
  dependsOn: [
    keyVaultZone
  ]
}

resource postgresqlZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (configureStage5Zones && createPostgresqlZone) {
  name: postgresqlZoneName
  location: 'global'
  tags: tags
}

resource postgresqlLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (configureStage5Zones) {
  #disable-next-line use-parent-property
  name: '${postgresqlZoneName}/${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  dependsOn: [
    postgresqlZone
  ]
}

resource postgresqlRunnerLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (createRunnerLinks) {
  #disable-next-line use-parent-property
  name: '${postgresqlZoneName}/${runnerLinkName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: runnerVirtualNetworkId
    }
  }
  dependsOn: [
    postgresqlZone
  ]
}

resource managedRedisZone 'Microsoft.Network/privateDnsZones@2024-06-01' = if (configureStage5Zones && createManagedRedisZone) {
  name: managedRedisZoneName
  location: 'global'
  tags: tags
}

resource managedRedisLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (configureStage5Zones) {
  #disable-next-line use-parent-property
  name: '${managedRedisZoneName}/${virtualNetworkName}-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: virtualNetwork.id
    }
  }
  dependsOn: [
    managedRedisZone
  ]
}

resource managedRedisRunnerLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = if (createRunnerLinks) {
  #disable-next-line use-parent-property
  name: '${managedRedisZoneName}/${runnerLinkName}'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: {
      id: runnerVirtualNetworkId
    }
  }
  dependsOn: [
    managedRedisZone
  ]
}

output privateDns object = {
  acrZoneId: acrZone.id
  azureOpenAIZoneId: azureOpenAIZone.id
  keyVaultZoneId: resourceId('Microsoft.Network/privateDnsZones', keyVaultZoneName)
  postgresqlZoneId: resourceId('Microsoft.Network/privateDnsZones', postgresqlZoneName)
  managedRedisZoneId: resourceId('Microsoft.Network/privateDnsZones', managedRedisZoneName)
  runnerLinksManaged: createRunnerLinks
  runnerLinkName: createRunnerLinks ? runnerLinkName : ''
}
