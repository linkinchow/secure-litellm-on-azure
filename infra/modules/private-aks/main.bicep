@description('Private AKS cluster name.')
param name string

@description('Optional node resource group name. AKS creates it; immutable after cluster creation.')
param nodeResourceGroupName string = ''

@description('Azure region for the cluster.')
param location string

@description('DNS prefix for the private AKS cluster.')
param dnsPrefix string

@description('Supported Kubernetes version frozen for this environment.')
param kubernetesVersion string

@description('AKS system node subnet resource ID.')
param systemSubnetId string

@description('AKS user node subnet resource ID.')
param userSubnetId string

@description('Existing Log Analytics workspace resource ID for Container Insights and Defender.')
param logAnalyticsWorkspaceId string

@description('System pool VM SKU.')
param systemNodeVmSize string = 'Standard_D2s_v3'

@description('User pool VM SKU.')
param userNodeVmSize string = 'Standard_D2s_v3'

@description('Initial system pool node count.')
param systemNodeCount int = 2

@description('Initial user pool node count.')
param userNodeCount int = 2

@description('Availability zones for both node pools. Empty keeps regional placement; zones cannot be changed after a pool is created.')
param availabilityZones string[] = []

@description('AKS pod CIDR for Azure CNI Overlay.')
param podCidr string = '10.244.0.0/16'

@description('Kubernetes service CIDR. It must not overlap the VNet, pod CIDR, or connected networks.')
param serviceCidr string = '10.31.0.0/16'

@description('Kubernetes DNS service IP inside serviceCidr.')
param dnsServiceIp string = '10.31.0.10'

@description('Kubernetes auto-upgrade channel; none leaves version upgrades to an approved change.')
@allowed([
  'none'
  'patch'
  'stable'
  'rapid'
  'node-image'
])
param upgradeChannel string = 'patch'

@description('Node OS image auto-upgrade channel; None leaves node image upgrades to an approved change.')
@allowed([
  'None'
  'Unmanaged'
  'NodeImage'
  'SecurityPatch'
])
param nodeOSUpgradeChannel string = 'NodeImage'

@description('Resource tags.')
param tags object = {}

var poolZones = empty(availabilityZones) ? null : availabilityZones
var minimumNodeCount = max(2, length(availabilityZones))

resource cluster 'Microsoft.ContainerService/managedClusters@2025-01-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'Base'
    tier: 'Standard'
  }
  properties: {
    aadProfile: {
      enableAzureRBAC: true
      managed: true
    }
    addonProfiles: {
      azurepolicy: {
        enabled: true
      }
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
          rotationPollInterval: '5m'
        }
      }
    }
    agentPoolProfiles: [
      {
        name: 'systempool'
        type: 'VirtualMachineScaleSets'
        mode: 'System'
        count: systemNodeCount
        vmSize: systemNodeVmSize
        osType: 'Linux'
        osSKU: 'AzureLinux'
        enableAutoScaling: true
        minCount: minimumNodeCount
        maxCount: 4
        availabilityZones: poolZones
        enableNodePublicIP: false
        maxPods: 50
        vnetSubnetID: systemSubnetId
        upgradeSettings: {
          maxSurge: '33%'
        }
      }
    ]
    apiServerAccessProfile: {
      disableRunCommand: true
      enablePrivateCluster: true
      enablePrivateClusterPublicFQDN: false
      privateDNSZone: 'system'
    }
    autoUpgradeProfile: {
      nodeOSUpgradeChannel: nodeOSUpgradeChannel
      upgradeChannel: upgradeChannel
    }
    disableLocalAccounts: true
    dnsPrefix: dnsPrefix
    enableRBAC: true
    identityProfile: {}
    kubernetesVersion: kubernetesVersion
    metricsProfile: {
      costAnalysis: {
        enabled: true
      }
    }
    networkProfile: {
      dnsServiceIP: dnsServiceIp
      ipFamilies: [
        'IPv4'
      ]
      loadBalancerSku: 'standard'
      networkDataplane: 'cilium'
      networkPlugin: 'azure'
      networkPluginMode: 'overlay'
      networkPolicy: 'cilium'
      outboundType: 'userDefinedRouting'
      podCidr: podCidr
      serviceCidr: serviceCidr
    }
    nodeResourceGroup: empty(nodeResourceGroupName) ? '${resourceGroup().name}-${name}-nodes' : nodeResourceGroupName
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      defender: {
        logAnalyticsWorkspaceResourceId: logAnalyticsWorkspaceId
        securityMonitoring: {
          enabled: true
        }
      }
      imageCleaner: {
        enabled: true
        intervalHours: 48
      }
      workloadIdentity: {
        enabled: true
      }
    }
    storageProfile: {
      blobCSIDriver: {
        enabled: false
      }
      diskCSIDriver: {
        enabled: true
      }
      fileCSIDriver: {
        enabled: true
      }
      snapshotController: {
        enabled: true
      }
    }
    supportPlan: 'KubernetesOfficial'
  }
}

resource userPool 'Microsoft.ContainerService/managedClusters/agentPools@2025-01-01' = {
  parent: cluster
  name: 'userpool'
  properties: {
    type: 'VirtualMachineScaleSets'
    mode: 'User'
    count: userNodeCount
    vmSize: userNodeVmSize
    osType: 'Linux'
    osSKU: 'AzureLinux'
    enableAutoScaling: true
    minCount: minimumNodeCount
    maxCount: 6
    availabilityZones: poolZones
    enableNodePublicIP: false
    maxPods: 50
    vnetSubnetID: userSubnetId
    upgradeSettings: {
      maxSurge: '33%'
    }
    nodeLabels: {
      workload: 'litellm'
    }
  }
}

resource clusterDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  scope: cluster
  name: 'send-aks-control-plane-to-log-analytics'
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

output aks object = {
  id: cluster.id
  name: cluster.name
  controlPlanePrincipalId: cluster.identity.principalId
  oidcIssuerUrl: cluster.properties.oidcIssuerProfile.issuerURL
  kubeletObjectId: cluster.properties.identityProfile.kubeletidentity.objectId
  systemSubnetId: systemSubnetId
  userSubnetId: userSubnetId
}
