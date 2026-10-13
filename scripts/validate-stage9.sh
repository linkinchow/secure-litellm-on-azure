#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
python_bin="${PYTHON_BIN:-$repo_root/.venv/bin/python}"
[[ -x "$python_bin" ]] || python_bin="$(command -v python3)"

echo "[1/4] Stage 8 regression (not production acceptance)"
bash scripts/validate-stage8.sh
echo "[2/4] Edge and PLS compiled contracts"
output_dir="$(mktemp -d)"
trap 'rm -rf "$output_dir"' EXIT
az bicep build --file infra/edge/main.bicep --outfile "$output_dir/edge.json" >/dev/null
az bicep build --file infra/edge-origin/main.bicep --outfile "$output_dir/origin.json" >/dev/null
az bicep build-params --file infra/edge/main.bicepparam --outfile "$output_dir/edge-parameters.json" >/dev/null
az bicep build-params --file infra/edge-origin/main.bicepparam --outfile "$output_dir/origin-parameters.json" >/dev/null
kubectl kustomize deploy/validation/stage9 > "$output_dir/stage9.yaml"
"$python_bin" scripts/validate_stage9.py "$output_dir/edge.json" "$output_dir/origin.json" "$output_dir/stage9.yaml"
echo "[3/4] Release evidence, What-if and bypass negative tests"
"$python_bin" -m unittest tests.test_stage9_release tests.test_stage9_templates tests.test_private_ingress_probes tests.test_api_wss tests.test_responses_wss_probe
echo "[4/4] Shell and OSS boundary"
bash -n scripts/validate-stage9.sh
"$python_bin" scripts/validate_product_boundary.py
echo "Stage 9 offline checks passed; no DNS change, resource deployment or traffic switch."