#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3)"

output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT

export LITELLM_ACR_SUFFIX="${LITELLM_ACR_SUFFIX:-stage4check}"

echo "[1/5] Stage 3 regression"
bash scripts/validate-stage3.sh

echo "[2/5] Stage 4 static safety"
"$python_bin" scripts/validate_stage4.py

echo "[3/5] Stage 4 Bicep modules"
for file in \
  infra/certificate-vault/main.bicep \
  infra/aks-ingress-role/main.bicep \
  infra/runner-target-connectivity/main.bicep \
  infra/hub/main.bicep \
  infra/modules/hub-network/main.bicep \
  infra/modules/hub-egress/main.bicep \
  infra/modules/network-foundation/main.bicep \
  infra/modules/firewall-egress/main.bicep \
  infra/modules/aks-network/main.bicep \
  infra/modules/private-dns/main.bicep \
  infra/modules/private-aks/main.bicep \
  infra/modules/workload-identity/main.bicep \
  infra/modules/acr-private-endpoint/main.bicep \
  infra/modules/acr-pull-role/main.bicep \
  infra/modules/aoai-private-endpoint/main.bicep \
  infra/modules/model-access-role/main.bicep; do
  az bicep build --file "$file" --outfile "$output_dir/$(echo "$file" | tr '/.' '__').json" >/dev/null
done

echo "[4/5] Environment templates"
az bicep build --file infra/environments/main.bicep --outfile "$output_dir/environment.json" >/dev/null
for file in infra/environments/{dev,test,prod}/main.bicepparam; do
  az bicep build-params --file "$file" --outfile "$output_dir/$(echo "$file" | tr '/.' '__').json" >/dev/null
done

echo "[5/5] Stage 4 repository policy"
if grep -RInE 'Microsoft\.ApiManagement|azure-mgmt-apimanagement|az[[:space:]]+apim' infra/modules infra/environments deploy/components/stage4-network 2>/dev/null; then
  echo "Stage 4 assets must not create or manage APIM." >&2
  exit 1
fi
if grep -RInE "(subscriptionId|subscription_id)[[:space:]]*[=:][[:space:]]*['\"][0-9a-f-]{36}['\"]|https://[^<[:space:]]+\\.openai\\.azure\\.com" infra/environments deploy/components/stage4-network 2>/dev/null; then
  echo "Stage 4 committed assets contain a concrete subscription or model endpoint." >&2
  exit 1
fi

echo "Stage 4 validation passed."
