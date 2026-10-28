targetScope = 'resourceGroup'

type stage4NetworkConfiguration = {
  virtualNetworkName: string
  privateEndpointSubnetName: string
  firewallSubnetPrefix: string
  systemSubnetName: string
  systemSubnetPrefix: string
  userSubnetName: string
  userSubnetPrefix: string
  ingressSubnetName: string
  ingressSubnetPrefix: string
  podCidr: string
  serviceCidr: string
  dnsServiceIp: string
}

type stage4AksConfiguration = {
  name: string
  nodeResourceGroupName: string?
  dnsPrefix: string
  kubernetesVersion: string
  systemNodeVmSize: string
  systemNodeCount: int
  userNodeVmSize: string
  userNodeCount: int
  availabilityZones: string[]?
  upgradeChannel: ('none' | 'patch' | 'stable' | 'rapid' | 'node-image')?
  nodeOSUpgradeChannel: ('None' | 'Unmanaged' | 'NodeImage' | 'SecurityPatch')?
}

type azureOpenAIConnection = {
  alias: string
  accountResourceId: string
  subscriptionId: string
  resourceGroupName: string
  accountName: string
  endpoint: string?
  @description('False reaches the account over its public endpoint (Firewall FQDN allowlist plus managed identity) instead of a private endpoint.')
  privateEndpoint: bool?
}

type stage5DataConfiguration = {
  postgresqlDatabaseName: string
  postgresqlSkuName: string
  postgresqlHighAvailabilityMode: ('Disabled' | 'SameZone' | 'ZoneRedundant')
  postgresqlEntraAdministratorObjectId: string
  postgresqlEntraAdministratorPrincipalName: string
  postgresqlEntraAdministratorPrincipalType: ('Group' | 'ServicePrincipal' | 'User')
  redisSkuName: string
  redisHighAvailability: ('Enabled' | 'Disabled')?
}

@description('Deployment environment.')
@allowed([
  'dev'
  'test'
  'prod'
])
param environmentName string

@description('Azure region for platform resources.')
param location string = resourceGroup().location

@description('Create the target ACR. Keep false until its Private Endpoint and DNS design are approved in Stage 4.')
param deployContainerRegistry bool = false

@description('Globally unique target ACR name.')
@minLength(5)
@maxLength(50)
param containerRegistryName string

@description('Temporary ACR public access. Production must remain Disabled.')
@allowed([
  'Disabled'
  'Enabled'
])
param containerRegistryPublicNetworkAccess string = 'Disabled'

@description('Common resource tags.')
param tags object = {}

@description('Create Stage 4 network, Firewall, Private AKS, Workload Identity, and private ACR connectivity. Defaults false for safe compilation and what-if.')
param deployStage4 bool = false

@description('Create Stage 5 Key Vault, PostgreSQL Flexible Server, Azure Managed Redis, Private Endpoints, diagnostics, and data-plane identity assignments. Requires deployStage4=true.')
param deployStage5 bool = false

@description('Approved migration runner principal ID allowed to initialize the backend Vault.')
param bootstrapPrincipalId string = ''

@description('Create the Stage 5 Key Vault private DNS zone. Keep false when the central zone already exists.')
param createStage5KeyVaultPrivateDnsZone bool = false

@description('Use resource-id before the first Stage 4 deployment so What-if can resolve role names. Retain principal-id for existing assignments unless separately migrated.')
@allowed([
  'principal-id'
  'resource-id'
])
param stage4RoleAssignmentNaming string = 'principal-id'

@description('Keep false when the Stage 4 certificate-vault component or DNS Owner manages the Key Vault VNet link.')
param configureStage5KeyVaultDnsLink bool = true

@description('Create the Stage 5 PostgreSQL private DNS zone. Keep false when the central zone already exists.')
param createStage5PostgresqlPrivateDnsZone bool = false

@description('Create the Stage 5 Azure Managed Redis private DNS zone.')
param createStage5ManagedRedisPrivateDnsZone bool = true

@description('Approved Runner VNet resource ID requiring Stage 5 private data-plane DNS resolution.')
param stage5RunnerVirtualNetworkId string = ''

@description('Create Runner VNet links for Stage 5 private DNS zones. Keep false for customer-managed DNS forwarding.')
param manageStage5RunnerDnsLinks bool = false

@description('Stage 5 data platform configuration. Entra administrator metadata must be injected through protected deployment variables before deployment.')
param stage5Data stage5DataConfiguration = {
  postgresqlDatabaseName: 'litellm'
  postgresqlSkuName: 'Standard_D2s_v3'
  postgresqlHighAvailabilityMode: 'Disabled'
  postgresqlEntraAdministratorObjectId: ''
  postgresqlEntraAdministratorPrincipalName: ''
  postgresqlEntraAdministratorPrincipalType: 'Group'
  redisSkuName: 'Balanced_B0'
}

@description('Stage 4 network configuration. CIDRs must be approved against connected enterprise networks before enabling deployment.')
param stage4Network stage4NetworkConfiguration = {
  virtualNetworkName: 'litellm-security-vnet'
  privateEndpointSubnetName: 'snet-private-endpoints'
  firewallSubnetPrefix: '10.30.0.0/26'
  systemSubnetName: 'snet-aks-system'
  systemSubnetPrefix: '10.30.1.0/24'
  userSubnetName: 'snet-aks-user'
  userSubnetPrefix: '10.30.2.0/23'
  ingressSubnetName: 'snet-private-ingress'
  ingressSubnetPrefix: '10.30.4.0/24'
  podCidr: '10.244.0.0/16'
  serviceCidr: '10.31.0.0/16'
  dnsServiceIp: '10.31.0.10'
}

@description('Stage 4 Private AKS configuration.')
param stage4Aks stage4AksConfiguration = {
  name: 'litellm-private-${environmentName}'
  dnsPrefix: 'litellm-private-${environmentName}'
  kubernetesVersion: '1.35'
  systemNodeVmSize: 'Standard_D2s_v3'
  systemNodeCount: 2
  userNodeVmSize: 'Standard_D2s_v3'
  userNodeCount: 2
}

@description('Existing Log Analytics workspace name for Private AKS monitoring and Defender.')
param logAnalyticsWorkspaceName string

@description('Reviewed Azure Firewall HTTPS FQDN allowlist. Extend only through approved change review.')
param approvedHttpsFqdns array = [
  'mcr.microsoft.com'
  '*.data.mcr.microsoft.com'
  '*.hcp.${location}.azmk8s.io'
  '*.tun.${location}.azmk8s.io'
  'packages.microsoft.com'
  'packages.aks.azure.com'
  'acs-mirror.azureedge.net'
]

var publicModelHosts = map(filter(azureOpenAIConnections, connection => !(connection.?privateEndpoint ?? true)), connection => '${connection.accountName}.openai.azure.com')

var azureEnvironmentHttpsFqdns = [
  replace(replace(environment().resourceManager, 'https://', ''), '/', '')
  replace(replace(environment().authentication.loginEndpoint, 'https://', ''), '/', '')
]

var stage5Enabled = deployStage4 && deployStage5
var stage5UniqueSuffix = uniqueString(subscription().id, resourceGroup().id, environmentName)
var keyVaultName = 'kv-lt-${environmentName}-${stage5UniqueSuffix}'
var postgresqlServerName = 'psql-lt-${environmentName}-${stage5UniqueSuffix}'
var managedRedisName = 'redis-lt-${environmentName}-${stage5UniqueSuffix}'

@description('Azure OpenAI resources connected to the private gateway. Keep empty in committed parameter files; inject protected values at deployment time.')
param azureOpenAIConnections azureOpenAIConnection[] = []

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: logAnalyticsWorkspaceName
}

module containerRegistry '../modules/container-registry/main.bicep' = if (deployContainerRegistry) {
  params: {
    name: containerRegistryName
    location: location
    environmentName: environmentName
    publicNetworkAccess: containerRegistryPublicNetworkAccess
    tags: tags
  }
}

module networkFoundation '../modules/network-foundation/main.bicep' = if (deployStage4) {
  params: {
    virtualNetworkName: stage4Network.virtualNetworkName
    firewallSubnetPrefix: stage4Network.firewallSubnetPrefix
  }
}

module privateDns '../modules/private-dns/main.bicep' = if (deployStage4) {
  params: {
    virtualNetworkName: stage4Network.virtualNetworkName
    configureStage5Zones: deployStage5
    createKeyVaultZone: createStage5KeyVaultPrivateDnsZone
    configureKeyVaultLink: configureStage5KeyVaultDnsLink
    createPostgresqlZone: createStage5PostgresqlPrivateDnsZone
    createManagedRedisZone: createStage5ManagedRedisPrivateDnsZone
    runnerVirtualNetworkId: stage5RunnerVirtualNetworkId
    configureRunnerLinks: manageStage5RunnerDnsLinks
    azureOpenAIFallbackToInternet: !empty(publicModelHosts)
    tags: tags
  }
}

module firewall '../modules/firewall-egress/main.bicep' = if (deployStage4) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    firewallName: 'afw-litellm-${environmentName}'
    firewallPolicyName: 'afwp-litellm-${environmentName}'
    publicIpName: 'pip-afw-litellm-${environmentName}'
    sourceAddressPrefixes: [
      stage4Network.systemSubnetPrefix
      stage4Network.userSubnetPrefix
      stage4Network.ingressSubnetPrefix
    ]
    approvedHttpsFqdns: union(approvedHttpsFqdns, azureEnvironmentHttpsFqdns, publicModelHosts)
    logAnalyticsWorkspaceId: logAnalyticsWorkspace.id
    tags: tags
  }
  dependsOn: [
    networkFoundation
  ]
}

module aksNetwork '../modules/aks-network/main.bicep' = if (deployStage4) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    systemSubnetName: stage4Network.systemSubnetName
    systemSubnetPrefix: stage4Network.systemSubnetPrefix
    userSubnetName: stage4Network.userSubnetName
    userSubnetPrefix: stage4Network.userSubnetPrefix
    ingressSubnetName: stage4Network.ingressSubnetName
    ingressSubnetPrefix: stage4Network.ingressSubnetPrefix
    firewallPrivateIpAddress: firewall!.outputs.firewall.privateIpAddress
    tags: tags
  }
}

module privateAks '../modules/private-aks/main.bicep' = if (deployStage4) {
  params: {
    name: stage4Aks.name
    nodeResourceGroupName: stage4Aks.?nodeResourceGroupName ?? ''
    location: location
    dnsPrefix: stage4Aks.dnsPrefix
    kubernetesVersion: stage4Aks.kubernetesVersion
    systemSubnetId: aksNetwork!.outputs.aksNetwork.systemSubnetId
    userSubnetId: aksNetwork!.outputs.aksNetwork.userSubnetId
    logAnalyticsWorkspaceId: logAnalyticsWorkspace.id
    systemNodeVmSize: stage4Aks.systemNodeVmSize
    systemNodeCount: stage4Aks.systemNodeCount
    userNodeVmSize: stage4Aks.userNodeVmSize
    userNodeCount: stage4Aks.userNodeCount
    availabilityZones: stage4Aks.?availabilityZones ?? []
    upgradeChannel: stage4Aks.?upgradeChannel ?? 'patch'
    nodeOSUpgradeChannel: stage4Aks.?nodeOSUpgradeChannel ?? 'NodeImage'
    podCidr: stage4Network.podCidr
    serviceCidr: stage4Network.serviceCidr
    dnsServiceIp: stage4Network.dnsServiceIp
    tags: tags
  }
}

module aksNetworkRole '../aks-ingress-role/main.bicep' = if (deployStage4) {
  params: {
    aksClusterName: stage4Aks.name
    virtualNetworkName: stage4Network.virtualNetworkName
    controlPlanePrincipalId: privateAks!.outputs.aks.controlPlanePrincipalId
  }
}

module workloadIdentity '../modules/workload-identity/main.bicep' = if (deployStage4) {
  params: {
    identityName: 'id-litellm-workload-${environmentName}'
    location: location
    oidcIssuerUrl: privateAks!.outputs.aks.oidcIssuerUrl
    kubernetesNamespace: 'litellm'
    serviceAccountName: 'litellm'
    tags: tags
  }
}

module acrPrivateEndpoint '../modules/acr-private-endpoint/main.bicep' = if (deployStage4 && deployContainerRegistry) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    privateEndpointSubnetName: stage4Network.privateEndpointSubnetName
    registryResourceId: containerRegistry!.outputs.registry.id
    registryName: containerRegistry!.outputs.registry.name
    tags: tags
  }
  dependsOn: [
    privateDns
  ]
}

module acrPullRole '../modules/acr-pull-role/main.bicep' = if (deployStage4 && deployContainerRegistry) {
  params: {
    registryName: containerRegistry!.outputs.registry.name
    kubeletPrincipalId: privateAks!.outputs.aks.kubeletObjectId
    principalSourceResourceId: stage4RoleAssignmentNaming == 'resource-id' ? resourceId('Microsoft.ContainerService/managedClusters', stage4Aks.name) : ''
  }
}

module azureOpenAIPrivateEndpoints '../modules/aoai-private-endpoint/main.bicep' = [for connection in azureOpenAIConnections: if (deployStage4 && (connection.?privateEndpoint ?? true)) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    privateEndpointSubnetName: stage4Network.privateEndpointSubnetName
    accountResourceId: connection.accountResourceId
    accountAlias: connection.alias
    tags: tags
  }
  dependsOn: [
    privateDns
  ]
}]

module azureOpenAIDataPlaneRoles '../modules/model-access-role/main.bicep' = [for connection in azureOpenAIConnections: if (deployStage4) {
  scope: resourceGroup(connection.subscriptionId, connection.resourceGroupName)
  params: {
    accountName: connection.accountName
    principalId: workloadIdentity!.outputs.workloadIdentity.principalId
    principalSourceResourceId: stage4RoleAssignmentNaming == 'resource-id' ? resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', 'id-litellm-workload-${environmentName}') : ''
  }
}]

module keyVault '../modules/key-vault/main.bicep' = if (stage5Enabled) {
  params: {
    name: keyVaultName
    location: location
    workloadPrincipalId: workloadIdentity!.outputs.workloadIdentity.principalId
    principalSourceResourceId: stage4RoleAssignmentNaming == 'resource-id' ? resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', 'id-litellm-workload-${environmentName}') : ''
    bootstrapPrincipalId: bootstrapPrincipalId
    logAnalyticsWorkspaceId: logAnalyticsWorkspace.id
    tags: tags
  }
}

module postgresql '../modules/postgresql-flexible-server/main.bicep' = if (stage5Enabled) {
  params: {
    name: postgresqlServerName
    location: location
    databaseName: stage5Data.postgresqlDatabaseName
    entraAdministratorObjectId: stage5Data.postgresqlEntraAdministratorObjectId
    entraAdministratorPrincipalName: stage5Data.postgresqlEntraAdministratorPrincipalName
    entraAdministratorPrincipalType: stage5Data.postgresqlEntraAdministratorPrincipalType
    logAnalyticsWorkspaceId: logAnalyticsWorkspace.id
    skuName: stage5Data.postgresqlSkuName
    highAvailabilityMode: stage5Data.postgresqlHighAvailabilityMode
    tags: tags
  }
}

module managedRedis '../modules/managed-redis/main.bicep' = if (stage5Enabled) {
  params: {
    name: managedRedisName
    location: location
    workloadPrincipalId: workloadIdentity!.outputs.workloadIdentity.principalId
    principalSourceResourceId: stage4RoleAssignmentNaming == 'resource-id' ? resourceId('Microsoft.ManagedIdentity/userAssignedIdentities', 'id-litellm-workload-${environmentName}') : ''
    logAnalyticsWorkspaceId: logAnalyticsWorkspace.id
    skuName: stage5Data.redisSkuName
    highAvailability: stage5Data.?redisHighAvailability ?? 'Enabled'
    tags: tags
  }
}

module keyVaultPrivateEndpoint '../modules/key-vault-private-endpoint/main.bicep' = if (stage5Enabled) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    privateEndpointSubnetName: stage4Network.privateEndpointSubnetName
    keyVaultResourceId: keyVault!.outputs.keyVault.id
    keyVaultAlias: 'litellm-${environmentName}'
    tags: tags
  }
  dependsOn: [
    privateDns
  ]
}

module postgresqlPrivateEndpoint '../modules/postgresql-private-endpoint/main.bicep' = if (stage5Enabled) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    privateEndpointSubnetName: stage4Network.privateEndpointSubnetName
    serverResourceId: postgresql!.outputs.postgresql.id
    serverAlias: 'litellm-${environmentName}'
    tags: tags
  }
  dependsOn: [
    privateDns
  ]
}

module redisPrivateEndpoint '../modules/redis-private-endpoint/main.bicep' = if (stage5Enabled) {
  params: {
    location: location
    virtualNetworkName: stage4Network.virtualNetworkName
    privateEndpointSubnetName: stage4Network.privateEndpointSubnetName
    redisResourceId: managedRedis!.outputs.redis.id
    redisAlias: 'litellm-${environmentName}'
    tags: tags
  }
  dependsOn: [
    privateDns
  ]
}

output platform object = {
  environment: environmentName
  containerRegistryDeployed: deployContainerRegistry
  containerRegistryName: containerRegistry.?outputs.?registry.?name ?? containerRegistryName
  stage4Deployed: deployStage4
  privateAksName: privateAks.?outputs.?aks.?name ?? stage4Aks.name
  workloadIdentityName: workloadIdentity.?outputs.?workloadIdentity.?name ?? 'id-litellm-workload-${environmentName}'
  workloadIdentityClientId: workloadIdentity.?outputs.?workloadIdentity.?clientId ?? ''
  workloadIdentityPrincipalId: workloadIdentity.?outputs.?workloadIdentity.?principalId ?? ''
  stage5Deployed: stage5Enabled
  keyVaultName: keyVault.?outputs.?keyVault.?name ?? keyVaultName
  postgresqlServerName: postgresql.?outputs.?postgresql.?name ?? postgresqlServerName
  managedRedisName: managedRedis.?outputs.?redis.?name ?? managedRedisName
  managedRedisHostName: managedRedis.?outputs.?redis.?hostName ?? ''
}
