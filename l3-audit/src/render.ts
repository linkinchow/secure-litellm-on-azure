import { readFile, writeFile } from 'node:fs/promises';
import { isIP } from 'node:net';

export interface DeploymentSettings {
  image: string; clientId: string; storageUrl: string; dce: string; dcr: string; privateEndpointIps: string[];
}
export function render(settings: DeploymentSettings): object[] {
  if (!/^[a-z0-9]+\.azurecr\.io\/[a-z0-9/._-]+@sha256:[a-f0-9]{64}$/.test(settings.image)
    || !/^[a-f0-9-]{36}$/i.test(settings.clientId)
    || !/^https:\/\/[a-z0-9]{3,24}\.blob\.core\.windows\.net\/?$/.test(settings.storageUrl)
    || !/^https:\/\/[a-z0-9.-]+\.ingest\.monitor\.azure\.com\/?$/.test(settings.dce)
    || !/^dcr-[a-f0-9]{32}$/.test(settings.dcr)
    || !settings.privateEndpointIps.length
    || !settings.privateEndpointIps.every(ip => isIP(ip) === 4 && /^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(ip))) {
    throw new Error('Invalid immutable L3 deployment settings');
  }
  const labels = { 'app.kubernetes.io/name': 'l3-audit' };
  const metadata = (name: string): object => ({ name, namespace: 'litellm', labels });
  const tcp = (port: number): object[] => [{ protocol: 'TCP', port }];
  const policy = (name: string, selector: object, from: object[], port: number): object => ({
    apiVersion: 'networking.k8s.io/v1', kind: 'NetworkPolicy', metadata: metadata(name),
    spec: { podSelector: { matchLabels: selector }, policyTypes: ['Ingress'],
      ingress: [{ from, ports: tcp(port) }] },
  });
  return [
    { apiVersion: 'v1', kind: 'ServiceAccount', metadata: {
      ...metadata('l3-audit'), annotations: { 'azure.workload.identity/client-id': settings.clientId } } },
    { apiVersion: 'v1', kind: 'Service', metadata: metadata('l3-audit'), spec: {
      selector: labels, ports: [{ name: 'http', port: 8080, targetPort: 8080 }], type: 'ClusterIP' } },
    { apiVersion: 'apps/v1', kind: 'StatefulSet', metadata: metadata('l3-audit'), spec: {
      serviceName: 'l3-audit', replicas: 2, selector: { matchLabels: labels },
      updateStrategy: { type: 'RollingUpdate' },
      template: { metadata: { labels: { ...labels, 'azure.workload.identity/use': 'true' } }, spec: {
        serviceAccountName: 'l3-audit', automountServiceAccountToken: false, terminationGracePeriodSeconds: 60,
        securityContext: { runAsNonRoot: true, runAsUser: 10001, runAsGroup: 10001, fsGroup: 10001,
          seccompProfile: { type: 'RuntimeDefault' } },
        topologySpreadConstraints: [{ maxSkew: 1, topologyKey: 'kubernetes.io/hostname',
          whenUnsatisfiable: 'ScheduleAnyway', labelSelector: { matchLabels: labels } }],
        containers: [{ name: 'l3-audit', image: settings.image,
          ports: [{ containerPort: 8080, name: 'http' }],
          env: [{ name: 'L3_STORAGE_URL', value: settings.storageUrl }, { name: 'L3_DCE_URL', value: settings.dce },
            { name: 'L3_DCR_ID', value: settings.dcr }, { name: 'L3_SPOOL', value: '/spool' }],
          resources: { requests: { cpu: '100m', memory: '256Mi' }, limits: { cpu: '1', memory: '768Mi' } },
          securityContext: { allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: { drop: ['ALL'] } },
          readinessProbe: { httpGet: { path: '/l3/health', port: 8080 }, periodSeconds: 5 },
          livenessProbe: { httpGet: { path: '/l3/health', port: 8080 }, periodSeconds: 15 },
          volumeMounts: [{ name: 'spool', mountPath: '/spool' }, { name: 'tmp', mountPath: '/tmp' }] }],
        volumes: [{ name: 'tmp', emptyDir: { sizeLimit: '64Mi' } }],
      } },
      volumeClaimTemplates: [{ metadata: { name: 'spool' }, spec: {
        accessModes: ['ReadWriteOnce'], storageClassName: 'managed-csi',
        resources: { requests: { storage: '2Gi' } } } }],
    } },
    { apiVersion: 'policy/v1', kind: 'PodDisruptionBudget', metadata: metadata('l3-audit'),
      spec: { minAvailable: 1, selector: { matchLabels: labels } } },
    policy('l3-proxy-ingress', labels, [{ namespaceSelector: {
      matchLabels: { 'kubernetes.io/metadata.name': 'llm-api-ingress' } } }], 8080),
    policy('l3-backend-ingress', { 'app.kubernetes.io/name': 'litellm', 'app.kubernetes.io/component': 'gateway' },
      [{ podSelector: { matchLabels: labels } }], 4000),
    { apiVersion: 'networking.k8s.io/v1', kind: 'NetworkPolicy', metadata: metadata('l3-egress'), spec: {
      podSelector: { matchLabels: labels }, policyTypes: ['Egress'], egress: [
        { to: [{ namespaceSelector: { matchLabels: { 'kubernetes.io/metadata.name': 'kube-system' } },
          podSelector: { matchLabels: { 'k8s-app': 'kube-dns' } } }],
          ports: [{ protocol: 'UDP', port: 53 }, { protocol: 'TCP', port: 53 }] },
        { to: [{ podSelector: { matchLabels: { 'app.kubernetes.io/name': 'litellm', 'app.kubernetes.io/component': 'gateway' } } }], ports: tcp(4000) },
        { to: settings.privateEndpointIps.map(ip => ({ ipBlock: { cidr: `${ip}/32` } })), ports: tcp(443) },
        // Entra token exchange uses approved public authority endpoints via the existing firewall.
        { to: [{ ipBlock: { cidr: '0.0.0.0/0', except: ['10.0.0.0/8', '169.254.169.254/32', '172.16.0.0/12', '192.168.0.0/16'] } }], ports: tcp(443) },
      ],
    } },
  ];
}
if (process.argv[1]?.endsWith('/render.js')) {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) throw new Error('Use render.js SETTINGS_JSON OUTPUT_JSON');
  const documents = render(JSON.parse(await readFile(input, 'utf8')) as DeploymentSettings);
  await writeFile(output, JSON.stringify({ apiVersion: 'v1', kind: 'List', items: documents }, null, 2),
    { mode: 0o600, flag: 'wx' });
}
