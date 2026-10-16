import { DefaultAzureCredential } from '@azure/identity';
import { BlobServiceClient } from '@azure/storage-blob';
import { sha, type Index, type RecordEnvelope } from './record.js';
import type { Delivery } from './queue.js';

export interface Settings {
  storageUrl: string; dce: string; dcr: string;
  subscription: string; resourceGroup: string; storageAccount: string;
}
export function settings(env = process.env): Settings {
  const value = { storageUrl: env.L3_STORAGE_URL ?? '', dce: env.L3_DCE_URL ?? '',
    dcr: env.L3_DCR_ID ?? '', subscription: env.L3_SUBSCRIPTION ?? '',
    resourceGroup: env.L3_RESOURCE_GROUP ?? '', storageAccount: env.L3_STORAGE_ACCOUNT ?? '' };
  if (!/^https:\/\/[a-z0-9]{3,24}\.blob\.core\.windows\.net\/?$/.test(value.storageUrl)
    || !/^https:\/\/[a-z0-9.-]+\.ingest\.monitor\.azure\.com\/?$/.test(value.dce)
    || !/^dcr-[a-f0-9]{32}$/.test(value.dcr)) throw new Error('Invalid L3 Azure destinations');
  return value;
}
export class AzureDelivery implements Delivery {
  readonly blobs: BlobServiceClient;
  constructor(readonly config: Settings, readonly credential: Pick<DefaultAzureCredential, 'getToken'> = new DefaultAzureCredential()) {
    this.blobs = new BlobServiceClient(config.storageUrl, this.credential);
  }
  async content(record: RecordEnvelope): Promise<void> {
    const blob = this.blobs.getContainerClient('l3-content').getBlockBlobClient(record.index.blob);
    const text = JSON.stringify(record);
    try {
      await blob.upload(text, Buffer.byteLength(text), { conditions: { ifNoneMatch: '*' },
        metadata: { envelopehash: sha(text) }, blobHTTPHeaders: { blobContentType: 'application/json' },
        abortSignal: AbortSignal.timeout(30000) });
    } catch (error) {
      const status = (error as { statusCode?: number }).statusCode;
      if (status !== 409 && status !== 412) throw error;
      const existing = await blob.getProperties({ abortSignal: AbortSignal.timeout(15000) });
      if (existing.metadata?.envelopehash !== sha(text)) throw new Error('Audit blob conflict');
    }
  }
  async index(index: Index): Promise<void> {
    const token = await this.credential.getToken('https://monitor.azure.com/.default');
    const response = await fetch(`${this.config.dce.replace(/\/$/, '')}/dataCollectionRules/${this.config.dcr}/streams/Custom-L3Audit_CL?api-version=2023-01-01`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(15000),
      headers: { authorization: `Bearer ${token.token}`, 'content-type': 'application/json' },
      body: JSON.stringify([{ TimeGenerated: new Date().toISOString(), Record: index }]),
    });
    if (!response.ok) throw new Error(`LAW ingestion failed: ${response.status}`);
  }
}
