@description('Azure region for the hub network and Azure Firewall.')
param location string

@description('Hub VNet name.')
param virtualNetworkName string

@description('Hub VNet address prefix. Reserve room for AzureFirewallManagementSubnet, AzureBastionSubnet and GatewaySubnet; they are not created here.')
param virtualNetworkAddressPrefix string

@description('AzureFirewallSubnet prefix (/26).')
param firewallSubnetPrefix string

@description('Azure Firewall name.')
param firewallName string

@description('Azure Firewall Policy name. Workloads add their own rule collection groups; the hub owns none.')
param firewallPolicyName string

@description('Public IP name used only by Azure Firewall egress.')
param publicIpName string

@description('Existing Log Analytics workspace resource ID for Firewall diagnostics.')
param logAnalyticsWorkspaceId string

@description('Resource tags.')
param tags object = {}

// Subnets stay inline so a re-run never resets them; subnets added outside this template appear as a What-if Modify.
resource virtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' = {
  name: virtualNetworkName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [virtualNetworkAddressPrefix]
    }
    dhcpOptions: {
      dnsServers: []
    }
    subnets: [
      {
        name: 'AzureFirewallSubnet'
        properties: {
          addressPrefix: firewallSubnetPrefix
          privateEndpointNetworkPolicies: 'Disabled'
          privateLinkServiceNetworkPolicies: 'Enabled'
        }
      }
    ]
  }
}

resource firewallSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-07-01' existing = {
  parent: virtualNetwork
  name: 'AzureFirewallSubnet'
}

// Zones stay unset: in zonal regions the platform spreads Azure Firewall and Standard public IPs across zones.
resource firewallPublicIp 'Microsoft.Network/publicIPAddresses@2024-07-01' = {
  name: publicIpName
  location: location
  tags: tags
  sku: {
    name: 'Standard'
    tier: 'Regional'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource firewallPolicy 'Microsoft.Network/firewallPolicies@2024-07-01' = {
  name: firewallPolicyName
  location: location
  tags: tags
  properties: {
    dnsSettings: {
      enableProxy: true
    }
    intrusionDetection: {
      mode: 'Alert'
    }
    sku: {
      tier: 'Premium'
    }
    threatIntelMode: 'Alert'
  }
}

resource firewall 'Microsoft.Network/azureFirewalls@2024-07-01' = {
  name: firewallName
  location: location
  tags: tags
  properties: {
    firewallPolicy: {
      id: firewallPolicy.id
    }
    ipConfigurations: [
      {
        name: 'firewall-ip-configuration'
        properties: {
          publicIPAddress: {
            id: firewallPublicIp.id
          }
          subnet: {
            id: firewallSubnet.id
          }
        }
      }
    ]
    sku: {
      name: 'AZFW_VNet'
      tier: 'Premium'
    }
    threatIntelMode: 'Alert'
  }
}

resource firewallDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: firewall
  name: 'send-firewall-logs-to-log-analytics'
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logAnalyticsDestinationType: 'Dedicated'
    logs: [
      {
        categoryGroup: 'allLogs'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

output hub object = {
  location: location
  virtualNetworkId: virtualNetwork.id
  virtualNetworkName: virtualNetwork.name
  virtualNetworkAddressPrefix: virtualNetworkAddressPrefix
  firewallSubnetPrefix: firewallSubnetPrefix
  firewallName: firewall.name
  firewallPolicyId: firewallPolicy.id
  firewallPolicyName: firewallPolicy.name
  firewallPrivateIpAddress: firewall.properties.ipConfigurations[0].properties.privateIPAddress
  firewallPublicIpAddress: firewallPublicIp.properties.ipAddress
}
