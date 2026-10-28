targetScope = 'subscription'

@description('Azure region of the hub; one hub per region.')
param location string

@description('Dedicated hub resource group created and owned by this component.')
param resourceGroupName string

@description('Hub VNet name.')
param virtualNetworkName string

@description('Hub VNet address prefix (for example a /24).')
param virtualNetworkAddressPrefix string

@description('AzureFirewallSubnet prefix (/26).')
param firewallSubnetPrefix string

@description('Azure Firewall name.')
param firewallName string

@description('Azure Firewall Policy name.')
param firewallPolicyName string

@description('Azure Firewall public IP name.')
param publicIpName string

@description('Existing Log Analytics workspace for Firewall diagnostics.')
param logAnalyticsWorkspaceName string

@description('Resource group of the existing Log Analytics workspace.')
param logAnalyticsWorkspaceResourceGroupName string

param tags object = {}

resource hubGroup 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  scope: resourceGroup(logAnalyticsWorkspaceResourceGroupName)
  name: logAnalyticsWorkspaceName
}

module hubNetwork '../modules/hub-network/main.bicep' = {
  name: 'llmgw-hub-network'
  scope: hubGroup
  params: {
    location: location
    virtualNetworkName: virtualNetworkName
    virtualNetworkAddressPrefix: virtualNetworkAddressPrefix
    firewallSubnetPrefix: firewallSubnetPrefix
    firewallName: firewallName
    firewallPolicyName: firewallPolicyName
    publicIpName: publicIpName
    logAnalyticsWorkspaceId: logAnalyticsWorkspace.id
    tags: tags
  }
}

output hub object = {
  resourceGroupName: hubGroup.name
  location: location
  virtualNetworkId: hubNetwork.outputs.hub.virtualNetworkId
  virtualNetworkName: hubNetwork.outputs.hub.virtualNetworkName
  virtualNetworkAddressPrefix: hubNetwork.outputs.hub.virtualNetworkAddressPrefix
  firewallSubnetPrefix: hubNetwork.outputs.hub.firewallSubnetPrefix
  firewallName: hubNetwork.outputs.hub.firewallName
  firewallPolicyId: hubNetwork.outputs.hub.firewallPolicyId
  firewallPolicyName: hubNetwork.outputs.hub.firewallPolicyName
  firewallPrivateIpAddress: hubNetwork.outputs.hub.firewallPrivateIpAddress
  firewallPublicIpAddress: hubNetwork.outputs.hub.firewallPublicIpAddress
}
