// Deployed into the hub resource group by a spoke platform: only the hub-to-spoke peering and the spoke's own rule collection group.

@description('Existing hub VNet name.')
param hubVirtualNetworkName string

@description('Existing hub Azure Firewall Policy name.')
param firewallPolicyName string

@description('Spoke (target) VNet resource ID.')
param spokeVirtualNetworkId string

@description('Hub-to-spoke peering name.')
param peeringName string

@description('Rule collection group owned by this spoke inside the shared hub policy.')
param ruleCollectionGroupName string

@description('Rule collection group priority; unique within the hub policy.')
@minValue(100)
@maxValue(65000)
param ruleCollectionGroupPriority int

@description('Spoke AKS node and ingress subnet prefixes allowed to use approved egress.')
param sourceAddressPrefixes array

@description('Approved HTTPS destination FQDNs. Keep this list explicit and reviewed.')
param approvedHttpsFqdns array

resource hubVirtualNetwork 'Microsoft.Network/virtualNetworks@2024-07-01' existing = {
  name: hubVirtualNetworkName
}

resource firewallPolicy 'Microsoft.Network/firewallPolicies@2024-07-01' existing = {
  name: firewallPolicyName
}

resource hubToSpoke 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2024-07-01' = {
  parent: hubVirtualNetwork
  name: peeringName
  properties: {
    remoteVirtualNetwork: { id: spokeVirtualNetworkId }
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    allowGatewayTransit: false
    useRemoteGateways: false
  }
}

// Same collections as modules/firewall-egress 'litellm-egress-rules'; tests keep them identical.
resource ruleCollectionGroup 'Microsoft.Network/firewallPolicies/ruleCollectionGroups@2024-07-01' = {
  parent: firewallPolicy
  name: ruleCollectionGroupName
  properties: {
    priority: ruleCollectionGroupPriority
    ruleCollections: [
      {
        name: 'allow-approved-https'
        priority: 200
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        action: {
          type: 'Allow'
        }
        rules: [
          {
            name: 'approved-https-fqdns'
            ruleType: 'ApplicationRule'
            sourceAddresses: sourceAddressPrefixes
            protocols: [
              {
                protocolType: 'Https'
                port: 443
              }
            ]
            targetFqdns: approvedHttpsFqdns
            terminateTLS: false
          }
        ]
      }
      {
        name: 'allow-platform-network'
        priority: 210
        ruleCollectionType: 'FirewallPolicyFilterRuleCollection'
        action: {
          type: 'Allow'
        }
        rules: [
          {
            name: 'azure-dns'
            ruleType: 'NetworkRule'
            sourceAddresses: sourceAddressPrefixes
            destinationAddresses: [
              '168.63.129.16'
            ]
            destinationPorts: [
              '53'
            ]
            ipProtocols: [
              'TCP'
              'UDP'
            ]
          }
          {
            name: 'network-time'
            ruleType: 'NetworkRule'
            sourceAddresses: sourceAddressPrefixes
            destinationAddresses: [
              '*'
            ]
            destinationPorts: [
              '123'
            ]
            ipProtocols: [
              'UDP'
            ]
          }
        ]
      }
    ]
  }
}

output hubEgress object = {
  peeringId: hubToSpoke.id
  ruleCollectionGroupId: ruleCollectionGroup.id
}
