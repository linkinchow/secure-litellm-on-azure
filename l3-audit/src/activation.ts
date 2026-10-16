import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile, stat } from 'node:fs/promises';
import { parse as yamlParse, stringify as yamlStringify } from 'yaml';
import { object, sha } from './record.js';

const execute = promisify(execFile);
type Document = Record<string, unknown>;
export function pgConfiguration(text: string, enabled: boolean): string {
  let runtime: Document;
  try { runtime = object(yamlParse(text)); } catch { throw new Error('Runtime YAML is invalid; values withheld'); }
  const settings = object(runtime.general_settings);
  const logging = object(runtime.litellm_settings);
  if (settings.disable_spend_logs !== false || typeof settings.store_prompts_in_spend_logs !== 'boolean'
    || logging.callbacks || logging.success_callback || logging.failure_callback) {
    throw new Error('Expected native Spend Logs baseline without unreviewed callbacks');
  }
  runtime.general_settings = { ...settings, store_prompts_in_spend_logs: !enabled };
  return yamlStringify(runtime);
}
function array(value: unknown): unknown[] {
  if (!Array.isArray(value)) throw new Error('Expected Kubernetes array');
  return value;
}
interface Snapshot { deployment: Document; configMap: Document; volumeIndex: number }
export function snapshot(deployment: Document, configMap: Document): Snapshot {
  const metadata = object(deployment.metadata), spec = object(deployment.spec);
  if (metadata.name !== 'litellm' || metadata.namespace !== 'litellm' || typeof metadata.uid !== 'string') {
    throw new Error('Expected live native LiteLLM Deployment');
  }
  const template = object(spec.template), pod = object(template.spec);
  const volumes = array(pod.volumes);
  const indices = volumes.flatMap((volume, i) => object(volume).configMap ? [i] : []);
  const selected = indices.filter(i => object(object(volumes[i]).configMap).name === object(configMap.metadata).name);
  if (selected.length !== 1 || typeof object(configMap.data)['config.yaml'] !== 'string') {
    throw new Error('Expected exactly one mounted LiteLLM runtime configuration');
  }
  return { deployment, configMap, volumeIndex: selected[0]! };
}
export function patchFor(current: Snapshot, configName: string): object[] {
  const template = object(object(current.deployment.spec).template);
  return [
    { op: 'test', path: '/metadata/uid', value: object(current.deployment.metadata).uid },
    { op: 'test', path: '/spec/template', value: template },
    { op: 'replace', path: `/spec/template/spec/volumes/${current.volumeIndex}/configMap/name`, value: configName },
  ];
}
interface Plan {
  version: 1; enabled: boolean; sourceSha: string; configSha: string;
  kubeconfig: string; expectedServer: string; before: Snapshot; desired: Document;
}
async function main(): Promise<void> {
  const [operation, configPath, kubeconfig, expectedServer, planPath, mode, approval] = process.argv.slice(2);
  if (!configPath || !kubeconfig || !expectedServer || !planPath
    || !['enable', 'disable'].includes(mode ?? '') || !['plan', 'execute'].includes(operation ?? '')) {
    throw new Error('Use activation.js plan|execute CFG KUBECONFIG EXPECTED_SERVER PLAN enable|disable [SHA256]');
  }
  const configText = await readFile(configPath, 'utf8'), config = object(JSON.parse(configText));
  const cluster = object(object(object(config.parameters).platform).stage4Aks).name;
  if (typeof cluster !== 'string' || !expectedServer.startsWith(`https://${cluster}-`)
    || !/\.privatelink\.[a-z0-9-]+\.azmk8s\.io(?::443)?$/.test(expectedServer)) {
    throw new Error('Expected approved private AKS FQDN from az aks show');
  }
  const native = object(object(config.application).authentication).mode === 'native';
  const selected = object(config.l3Audit).enabled === true;
  if (!native || selected !== (mode === 'enable')) throw new Error('CFG native/L3 mode differs from operation');
  const sourceSha = sha(Buffer.concat(await Promise.all([
    new URL(import.meta.url), new URL('./record.js', import.meta.url),
    new URL('../../package.json', import.meta.url),
  ].map(path => readFile(path)))));
  const kube = ['--kubeconfig', kubeconfig, '--namespace', 'litellm'];
  async function command(args: string[]): Promise<string> {
    try {
      return (await execute('kubectl', [...kube, ...args], { timeout: 15 * 60 * 1000,
        maxBuffer: 8 * 1024 * 1024 })).stdout;
    } catch { throw new Error('Scoped kubectl operation failed; inspect private connectivity, RBAC, admission or rollout'); }
  }
  const context = object(JSON.parse(await command(['config', 'view', '--minify', '-o', 'json'])));
  const server = object(object(array(context.clusters)[0]).cluster).server;
  if (server !== expectedServer) throw new Error('Kubeconfig does not target the approved AKS API');
  const deployment = object(JSON.parse(await command(['get', 'deployment', 'litellm', '-o', 'json'])));
  const pod = object(object(object(deployment.spec).template).spec);
  const containers = array(pod.containers).map(object).filter(container => container.name === 'litellm');
  if (containers.length !== 1 || containers[0]!.image !== object(config.application).backendImage) {
    throw new Error('Live backend image differs from approved CFG');
  }
  const configVolumes = array(pod.volumes).map(volume => object(object(volume).configMap).name)
    .filter((name): name is string => typeof name === 'string' && name.startsWith('litellm-config-'));
  if (configVolumes.length !== 1) throw new Error('Expected managed LiteLLM configuration volume');
  const configMap = object(JSON.parse(await command(['get', 'configmap', configVolumes[0]!, '-o', 'json'])));
  const before = snapshot(deployment, configMap);
  const wanted = pgConfiguration(String(object(configMap.data)['config.yaml']), mode === 'enable');
  const configName = 'litellm-config-l3-' + sha(wanted).slice(0, 12);
  const desired = { apiVersion: 'v1', kind: 'ConfigMap', immutable: true,
    metadata: { name: configName, namespace: 'litellm',
      labels: { 'app.kubernetes.io/managed-by': 'llmgw-workflow' } }, data: { 'config.yaml': wanted } };
  const plan: Plan = { version: 1, enabled: mode === 'enable', sourceSha, configSha: sha(configText),
    kubeconfig, expectedServer, before, desired };
  if (operation === 'plan') {
    await writeFile(planPath, JSON.stringify(plan), { mode: 0o600, flag: 'wx' });
    console.log(JSON.stringify({ pgContentEnabled: !plan.enabled, planSha256: sha(JSON.stringify(plan)),
      note: 'Only runtime config volume changes; no infrastructure, image, model or DNS changes.' }));
    return;
  }
  const file = await readFile(planPath, 'utf8');
  if ((await stat(planPath)).mode & 0o077) throw new Error('Plan must have private permissions');
  if (!approval || sha(file) !== approval) throw new Error('Approval does not match saved plan');
  const reviewed: Plan = JSON.parse(file);
  // Compare only the target template and configuration, not status/managedFields timestamps.
  if (reviewed.sourceSha !== sourceSha || reviewed.configSha !== plan.configSha
    || reviewed.enabled !== plan.enabled || reviewed.expectedServer !== server
    || object(reviewed.before.deployment.metadata).uid !== object(deployment.metadata).uid
    || JSON.stringify(object(object(reviewed.before.deployment.spec).template)) !== JSON.stringify(object(object(deployment.spec).template))
    || JSON.stringify(reviewed.before.configMap.data) !== JSON.stringify(configMap.data)
    || JSON.stringify(reviewed.desired) !== JSON.stringify(desired)) throw new Error('Plan is stale; replan and approve');
  // Write the exact immutable ConfigMap via a private file; kubectl never receives secret values as arguments.
  const material = planPath + '.configmap.json';
  await writeFile(material, JSON.stringify(desired), { mode: 0o600, flag: 'wx' });
  await command(['apply', '--server-side', '--field-manager=llmgw-l3', '-f', material]);
  const patchPath = planPath + '.patch.json';
  await writeFile(patchPath, JSON.stringify(patchFor(before, configName)), { mode: 0o600, flag: 'wx' });
  await command(['patch', 'deployment', 'litellm', '--type=json', '--patch-file', patchPath]);
  await command(['rollout', 'status', 'deployment/litellm', '--timeout=15m']);
  console.log(JSON.stringify({ pgContentEnabled: !plan.enabled, rolloutVerified: true,
    configMap: configName, infrastructureDeployed: false }));
}
if (process.argv[1]?.endsWith('/activation.js')) main().catch(error => {
  console.error(error instanceof SyntaxError ? 'L3 input JSON is invalid; values withheld'
    : error instanceof Error ? error.message : 'L3 activation failed');
  process.exitCode = 1;
});
