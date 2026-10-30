targetScope = 'subscription'

@description('Leave empty to create an independent management VNet with NAT, without target peering or DNS links. Otherwise reuse an approved same-subscription, same-region subnet without changing its network.')
param subnetResourceId string = ''

@description('New-network mode only: approved non-overlapping private IPv4 /24. Runner and Bastion use the first two /26 subnets.')
param managementAddressPrefix string = '10.50.0.0/24'

@description('Create a charged Standard Bastion with native SSH support in new-network mode. Existing-subnet mode never creates a Bastion.')
param createBastion bool = empty(subnetResourceId)

@description('SSH public key only. Never supply a private key or GitHub registration token.')
@minLength(40)
param sshPublicKey string

@description('Required for SSH without the managed Bastion: approved management IPv4 CIDR. Empty denies all inbound. Managed Bastion uses its own subnet instead.')
@maxLength(18)
param sshSourceCidr string = ''

@allowed(['dev', 'test', 'prod'])
param environmentName string = 'test'

param location string = deployment().location

@description('Standard_D2s_v5 (2 vCPU/8 GiB) is a customer-approved cost floor: image builds and large restores run slower.')
@allowed(['Standard_D2s_v5', 'Standard_D4s_v3', 'Standard_D4s_v5', 'Standard_D8s_v5'])
param virtualMachineSize string = 'Standard_D4s_v3'

@minValue(128)
param osDiskSizeGiB int = 128

@minValue(256)
param dataDiskSizeGiB int = 256

@description('Approved Canonical Marketplace image version. latest selects the current Ubuntu 24.04 LTS image when creating the VM.')
param ubuntuImageVersion string = 'latest'

var adminUsername = 'runneradmin'
var suffix = take(uniqueString(subscription().subscriptionId, subnetResourceId, environmentName), 8)
var runnerName = 'vm-llmgw-runner-${environmentName}-${suffix}'
var ownedTags = {
  managedBy: 'llmgw-manual-runner'
  environment: environmentName
  purpose: 'private-migration-runner'
}

resource runnerGroup 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-llmgw-runner-${environmentName}-${suffix}'
  location: location
  tags: ownedTags
}

module network 'management-network.bicep' = if (empty(subnetResourceId)) {
  scope: runnerGroup
  params: {
    location: location
    namePrefix: runnerName
    addressPrefix: managementAddressPrefix
    createBastion: createBastion
    tags: ownedTags
  }
}

module runner 'standard-vm.bicep' = {
  scope: runnerGroup
  params: {
    location: location
    runnerName: runnerName
    subnetResourceId: empty(subnetResourceId) ? network!.outputs.runnerSubnetId : subnetResourceId
    sshPublicKey: sshPublicKey
    sshSourceCidr: empty(subnetResourceId) && createBastion ? network!.outputs.bastionSubnetPrefix : sshSourceCidr
    virtualMachineSize: virtualMachineSize
    adminUsername: adminUsername
    environmentName: environmentName
    osDiskSizeGiB: osDiskSizeGiB
    dataDiskSizeGiB: dataDiskSizeGiB
    ubuntuImageVersion: ubuntuImageVersion
    tags: ownedTags
  }
}

output resourceGroupName string = runnerGroup.name
output virtualMachineName string = runnerName
output virtualMachineId string = runner.outputs.virtualMachineId
output networkMode string = empty(subnetResourceId) ? 'new' : 'existing'
output managementVnetId string = empty(subnetResourceId) ? network!.outputs.vnetId : ''
output runnerSubnetId string = empty(subnetResourceId) ? network!.outputs.runnerSubnetId : subnetResourceId
output outboundPublicIp string = empty(subnetResourceId) ? network!.outputs.outboundPublicIp : ''
output bastionName string = empty(subnetResourceId) && createBastion ? network!.outputs.bastionName : ''
output privateIpAddress string = runner.outputs.privateIpAddress
output sshCommand string = 'ssh ${adminUsername}@${runner.outputs.privateIpAddress}'
output bastionSshCommand string = empty(subnetResourceId) && createBastion
  ? 'az network bastion ssh --subscription ${subscription().subscriptionId} --resource-group ${runnerGroup.name} --name ${network!.outputs.bastionName} --target-resource-id ${runner.outputs.virtualMachineId} --auth-type ssh-key --username ${adminUsername} --ssh-key ~/.ssh/id_ed25519'
  : ''
output targetPrivateConnectivityVerified bool = false
output runnerLabels array = ['self-hosted', 'Linux', 'X64', 'llmgw-${environmentName}-private']
output bootstrapCheck string = 'sudo cloud-init status --wait --long && sudo test -f /var/lib/llmgw-runner/bootstrap-complete'
output registrationCommand string = 'sudo /usr/local/sbin/llmgw-runner-register'
output githubRegistered bool = false
