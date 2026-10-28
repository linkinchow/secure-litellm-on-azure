using './main.bicep'

param deployEdge = false
param enableApiTraffic = false
param environmentName = 'test'
param baseDomain = 'example.com'
param privateOrigin = {
  privateLinkServiceId: 'REPLACE_API_PRIVATE_LINK_SERVICE_ID'
  privateLinkLocation: 'westus3'
}
param adminPrivateOrigin = {
  privateLinkServiceId: 'REPLACE_ADMIN_PRIVATE_LINK_SERVICE_ID'
  privateLinkLocation: 'westus3'
}
param adminAllowedCidrs = ['REPLACE_ADMIN_ALLOWED_PUBLIC_CIDR']
param logAnalyticsWorkspaceName = 'REPLACE_LOG_ANALYTICS_WORKSPACE'
param wafMode = 'Detection'
param rateLimitPerMinute = 600
param adminRateLimitPerMinute = 120
param modelListRateLimitPerMinute = 120
