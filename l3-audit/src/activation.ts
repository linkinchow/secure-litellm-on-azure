import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { readFile, writeFile, stat } from 'node:fs/promises';
import { parse as yamlParse, stringify as yamlStringify } from 'yaml';
import { object, sha } from './record.js';

const execute = promisify(execFile);
type Document = Record<string, unknown>;
// Request-compatibility callbacks reviewed for the backend image; mirrors BACKEND_CALLBACKS in scripts/backend_manifest.py.
// They rewrite requests and record no content, so they do not change what the PG flag controls.
export const REVIEWED_CALLBACKS: readonly string[] = ['LiteLLM.runtime.callbacks.codex_passthrough'];
function reviewedCallbacks(value: unknown): boolean {
  return value === undefined || value === null
    || (Array.isArray(value) && value.every(item => typeof item === 'string' && REVIEWED_CALLBACKS.includes(item)));
}
export function pgConfiguration(text: string, enabled: boolean): string {
  let runtime: Document;
  try { runtime = object(yamlParse(text)); } catch { throw new Error('Runtime YAML is invalid; values withheld'); }
  const settings = object(runtime.general_settings);
  const logging = object(runtime.litellm_settings);
  if (settings.disable_spend_logs !== false || typeof settings.store_prompts_in_spend_logs !== 'boolean'
    || !reviewedCallbacks(logging.callbacks) || logging.success_callback || logging.failure_callback) {
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
export const PLATFORM_MANAGER = 'llmgw-migration';
// Earlier activation releases used kubectl's JSON patch, whose default field manager took the volume reference.
const LEGACY_VOLUME_MANAGERS = new Set(['kubectl-patch']);
export function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) => item && typeof item === 'object' && !Array.isArray(item)
    ? Object.fromEntries(Object.entries(item).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)) : item);
}
// Rebuild the configuration one field manager applied from the live object and its FieldsV1 set
// (like client-go's Extract), so re-applying it under that manager keeps every field it owns.
export function extractOwned(value: unknown, fields: Document): unknown {
  if (Array.isArray(value)) {
    const result: unknown[] = [], used = new Set<string>();
    value.forEach((item, position) => {
      const key = Object.keys(fields).find(name => !used.has(name) && (name === `i:${position}`
        || (name.startsWith('k:') && Object.entries(object(JSON.parse(name.slice(2))))
          .every(([field, wanted]) => canonical(object(item)[field]) === canonical(wanted)))
        || (name.startsWith('v:') && canonical(JSON.parse(name.slice(2))) === canonical(item))));
      if (!key) return;
      used.add(key);
      result.push(key.startsWith('v:') ? structuredClone(item) : key.startsWith('k:')
        ? { ...object(JSON.parse(key.slice(2))), ...object(ownedValue(item, object(fields[key]))) }
        : ownedValue(item, object(fields[key])));
    });
    return result;
  }
  const source = object(value), result: Document = {};
  for (const [key, child] of Object.entries(fields)) {
    if (key.startsWith('f:') && key.slice(2) in source) result[key.slice(2)] = ownedValue(source[key.slice(2)], object(child));
  }
  return result;
}
// An empty set owns the whole (leaf or atomic) value; "." alone owns only the value's presence.
function ownedValue(item: unknown, fields: Document): unknown {
  const names = Object.keys(fields);
  if (!names.length) return structuredClone(item);
  if (names.some(name => name !== '.')) return extractOwned(item, fields);
  return Array.isArray(item) ? [] : typeof item === 'object' && item !== null ? {} : structuredClone(item);
}
function volumeFields(fields: Document, volume: string): Document {
  const volumes = object(object(object(object(fields['f:spec'])['f:template'])['f:spec'])['f:volumes']);
  const key = Object.keys(volumes).find(name => name.startsWith('k:')
    && canonical(JSON.parse(name.slice(2))) === canonical({ name: volume }));
  return key ? object(volumes[key]) : {};
}
export interface Activation { document: Document; forceConflicts: boolean }
// The platform applies this Deployment with server-side apply as llmgw-migration (Stage 6/8). Re-apply that
// manager's own configuration with only the volume reference changed, so ownership stays with the platform.
export function activationFor(current: Snapshot, configName: string): Activation {
  const metadata = object(current.deployment.metadata);
  const managers = Array.isArray(metadata.managedFields) ? metadata.managedFields.map(object) : [];
  if (!managers.length) throw new Error('Deployment ownership metadata is missing; read it with --show-managed-fields');
  const platform = managers.filter(entry => entry.manager === PLATFORM_MANAGER && entry.operation === 'Apply');
  if (platform.length !== 1 || platform[0]!.fieldsType !== 'FieldsV1') {
    throw new Error('Expected one llmgw-migration server-side apply owner of the LiteLLM Deployment; rerun the platform application stage');
  }
  const pod = object(object(object(current.deployment.spec).template).spec);
  const volume = object(array(pod.volumes)[current.volumeIndex]).name;
  if (typeof volume !== 'string' || !volume) throw new Error('Expected named LiteLLM runtime configuration volume');
  const owned = object(extractOwned(current.deployment, object(platform[0]!.fieldsV1)));
  const volumes = object(object(object(owned.spec).template).spec).volumes;
  const entry = Array.isArray(volumes) ? volumes.map(object).find(item => item.name === volume) : undefined;
  if (!entry) throw new Error('Runtime configuration volume is not owned by llmgw-migration; rerun the platform application stage');
  entry.configMap = { ...object(entry.configMap), name: configName };
  let forceConflicts = false;
  for (const item of managers) {
    if (item === platform[0] || !('f:name' in object(volumeFields(object(item.fieldsV1), volume)['f:configMap']))) continue;
    if (!LEGACY_VOLUME_MANAGERS.has(String(item.manager)) || item.operation !== 'Update') {
      throw new Error('Another field manager owns the runtime configuration reference; review it before activation');
    }
    forceConflicts = true;
  }
  const { apiVersion: _version, kind: _kind, metadata: ownedMetadata, ...rest } = owned;
  return { forceConflicts, document: { apiVersion: 'apps/v1', kind: 'Deployment',
    metadata: { ...object(ownedMetadata), name: 'litellm', namespace: 'litellm', uid: metadata.uid }, ...rest } };
}
export function expectedSpec(current: Snapshot, configName: string): Document {
  const spec = structuredClone(object(current.deployment.spec));
  const volume = object(array(object(object(spec.template).spec).volumes)[current.volumeIndex]);
  volume.configMap = { ...object(volume.configMap), name: configName };
  return spec;
}
interface Plan {
  version: 1; enabled: boolean; sourceSha: string; configSha: string;
  kubeconfig: string; expectedServer: string; before: Snapshot; desired: Document; activation: Activation;
}
export function privateApiOrigin(value: unknown, cluster: unknown): string {
  if (typeof value !== 'string' || value.trim() !== value || !/^https:\/\/[a-zA-Z0-9.-]+(?::443)?\/?$/.test(value)
    || typeof cluster !== 'string' || !/^[a-zA-Z0-9-]+$/.test(cluster)) {
    throw new Error('AKS API must be an HTTPS private FQDN on port 443 without credentials, query or non-root path');
  }
  const url = new URL(value);
  if (!url.hostname.startsWith(`${cluster.toLowerCase()}-`)
    || !/\.privatelink\.[a-z0-9-]+\.azmk8s\.io$/.test(url.hostname)) {
    throw new Error('Expected approved private AKS FQDN from az aks show');
  }
  return url.origin;
}
async function main(): Promise<void> {
  const [operation, configPath, kubeconfig, expectedServer, planPath, mode, approval] = process.argv.slice(2);
  if (!configPath || !kubeconfig || !expectedServer || !planPath
    || !['enable', 'disable'].includes(mode ?? '') || !['plan', 'execute'].includes(operation ?? '')) {
    throw new Error('Use activation.js plan|execute CFG KUBECONFIG EXPECTED_SERVER PLAN enable|disable [SHA256]');
  }
  const configText = await readFile(configPath, 'utf8'), config = object(JSON.parse(configText));
  const cluster = object(object(object(config.parameters).platform).stage4Aks).name;
  const approvedServer = privateApiOrigin(expectedServer, cluster);
  const native = object(object(config.application).authentication).mode === 'native';
  const selected = object(config.l3Audit).enabled === true;
  if (!native) throw new Error('PG activation requires application.authentication.mode=native; no changes performed');
  if (mode === 'enable' && !selected) {
    throw new Error('Enable requires top-level l3Audit.enabled=true (boolean) in CFG; merge the opt-in setting and rerun ingress plan/execute before PG activation');
  }
  if (mode === 'disable' && selected) {
    throw new Error('Disable requires removing the top-level l3Audit setting from CFG; no changes performed');
  }
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
  const server = privateApiOrigin(object(object(array(context.clusters)[0]).cluster).server, cluster);
  if (server !== approvedServer) throw new Error('Kubeconfig does not target the approved AKS API');
  // kubectl hides managedFields by default; activation needs them to keep the platform's field ownership.
  const deployment = object(JSON.parse(await command(['get', 'deployment', 'litellm', '--show-managed-fields', '-o', 'json'])));
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
  const activation = activationFor(before, configName);
  const plan: Plan = { version: 1, enabled: mode === 'enable', sourceSha, configSha: sha(configText),
    kubeconfig, expectedServer: approvedServer, before, desired, activation };
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
    || JSON.stringify(reviewed.desired) !== JSON.stringify(desired)
    || canonical(reviewed.activation) !== canonical(activation)) throw new Error('Plan is stale; replan and approve');
  // UID and resourceVersion make the apply conditional on the Deployment read above (compare-and-swap).
  const workload = planPath + '.deployment.json';
  await writeFile(workload, JSON.stringify({ ...activation.document, metadata: { ...object(activation.document.metadata),
    resourceVersion: object(deployment.metadata).resourceVersion } }), { mode: 0o600, flag: 'wx' });
  const apply = ['apply', '--server-side', `--field-manager=${PLATFORM_MANAGER}`,
    ...(activation.forceConflicts ? ['--force-conflicts'] : []), '-f', workload];
  // Before any change, prove the server would alter only the runtime configuration reference.
  const preview = object(JSON.parse(await command([...apply, '--dry-run=server', '-o', 'json'])));
  if (canonical(preview.spec) !== canonical(expectedSpec(before, configName))
    || object(preview.metadata).uid !== object(deployment.metadata).uid
    || canonical(object(preview.metadata).labels) !== canonical(object(deployment.metadata).labels)
    || canonical(object(preview.metadata).annotations) !== canonical(object(deployment.metadata).annotations)) {
    throw new Error('Server dry-run changes more than the runtime configuration reference; no changes performed');
  }
  // Write the exact immutable ConfigMap via a private file; kubectl never receives secret values as arguments.
  const material = planPath + '.configmap.json';
  await writeFile(material, JSON.stringify(desired), { mode: 0o600, flag: 'wx' });
  await command(['apply', '--server-side', '--field-manager=llmgw-l3', '-f', material]);
  await command(apply);
  await command(['rollout', 'status', 'deployment/litellm', '--timeout=15m']);
  console.log(JSON.stringify({ pgContentEnabled: !plan.enabled, rolloutVerified: true,
    configMap: configName, infrastructureDeployed: false }));
}
if (process.argv[1]?.endsWith('/activation.js')) main().catch(error => {
  console.error(error instanceof SyntaxError ? 'L3 input JSON is invalid; values withheld'
    : error instanceof Error ? error.message : 'L3 activation failed');
  process.exitCode = 1;
});
