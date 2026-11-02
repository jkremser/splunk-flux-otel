# Mobility Global: Flux, Splunk OTel and record-only Predictor

Traffic → HAProxy → nginx. Splunk's bundled single cluster collector scrapes each HAProxy pod at port 1024. The shared receiver feeds `metrics/haproxy` → Splunk and `metrics/scaling` → OTel Scaler over OTLP/gRPC. Existing Splunk node/cluster monitoring pipelines remain intact.

KEDA queries the scaler and drives nginx's HPA. Predictor independently subscribes to KEDA's TLS raw-metrics stream and stores RPS measurements in PostgreSQL. **The ScaledObject never queries Predictor.** Training readiness is not required for recording.

## Versions and ownership

| Component | Pin | Namespace |
|---|---|---|
| AKS | 1.35.8, two Standard_D2s_v5 nodes | mobility-global / West Europe |
| Flux | 2.10.0 | flux-system |
| HAProxy chart / controller | 1.47.3 / 3.2.3 | ingress-system |
| Splunk chart / Collector | 0.120.0 | observability |
| Kedify KEDA | v2.20.0-1 | keda |
| Kedify Agent | v0.8.3 | keda |
| OTel Scaler | 0.1.5 | keda |
| Predictor | 0.1.9 | keda |
| PostgreSQL | 17.11, digest pinned | keda |

Kubernetes 1.34 from the plan is unavailable in West Europe. 1.35 is inside KEDA 2.20's tested Kubernetes range. Azure replaces the customer's EKS environment.

All releases and workload configuration are owned by Flux. Agent auto-updates are disabled and optional Agent subcharts are disabled. Bootstrap installs only Flux and initial trust material outside GitOps. Generated HPA resources belong to KEDA; nginx's replica field is omitted from Git. PostgreSQL uses an 8Gi `managed-csi` disk. PVC history is retained across pod replacement.

Predictor stores uploaded model files on a dedicated 1Gi `managed-csi` PVC mounted at `/models`. PostgreSQL remains the database; `noSharedVolumes: true` prevents the chart from creating its SQLite PVC. Training jobs use temporary model storage and upload to the API. The API pod uses `fsGroup: 65532` for write access and a `Recreate` deployment strategy for its single-node disk, so upgrades briefly interrupt the API. The PVC and HelmRelease belong to the same Flux Kustomization, allowing the API pod to trigger `WaitForFirstConsumer` disk binding.

Splunk 0.120.0 has a malformed legacy schema keyword (`deprecated` is a string), rejected by Helm 4. Schema checking is disabled only for that HelmRelease; its rendered configuration is validated with the actual Collector binary.

## Prepare secrets

Prerequisites: Azure CLI, GitHub CLI, Helm, kubectl, Ruby, Python 3, SOPS and age. Install Flux **2.10.0** at `.local/bin/flux` (or on PATH). The old preinstalled 0.41 CLI does not support these Flux APIs.

```sh
cp .env.example .env
# Edit .env locally; never commit it or paste tokens into chat.
# Current backend: set SPLUNK_HEC_ENDPOINT and the dedicated SPLUNK_HEC_TOKEN.
python3 scripts/prepare-secrets.py --platform
git add .sops.yaml infrastructure/secrets clusters/mobility-global/reconciliation.yaml
git commit -m 'Configure encrypted deployment credentials'
git push origin main
```

The secrets Kustomization starts suspended so independent infrastructure can deploy before credentials are available. This script enables it once all credentials have been encrypted. Commit its reconciliation change along with the secrets.

The script generates a private age key and PostgreSQL password under gitignored `.local/`. Back up the age key privately: it is needed to recover/edit encrypted credentials. Repeated runs reuse the same key/password. The realm is shown under Splunk Observability Cloud **Settings → your username → Organizations**, or in the API/ingest endpoint hostname. Splunk Cloud Platform is a different product; its HEC tokens cannot replace an Observability ingest token.

## Provision and bootstrap

```sh
az login --tenant 3ed9431b-8569-4806-bc02-18c7d201a7be
scripts/bootstrap.sh
scripts/status.sh
```

Bootstrap uses subscription `78db6462-39be-4bd7-bf09-787daf09a3ce`, creates/reuses resource group and AKS `mobility-global`, and uses only `.local/kubeconfig`. It does not switch the user's normal Kubernetes context. GitHub bootstrap needs repository administration/deploy-key permissions. Existing resources are inspected before reuse; access errors are not treated as absence.

Flux dependency order is namespaces/sources → encrypted secrets → infrastructure → nginx/Collector → ScaledObject → MetricPredictor. CRDs and controller readiness are gated before custom resources. Do not manually Helm-install duplicate releases.

## Validate before deployment

```sh
scripts/download-charts.sh
ruby scripts/validate.rb
# Linux example; macOS release binaries are also available:
curl -fsSL https://github.com/signalfx/splunk-otel-collector/releases/download/v0.120.0/otelcol_linux_amd64 -o .cache/otelcol
chmod +x .cache/otelcol
SPLUNK_PLATFORM_HEC_TOKEN=validation K8S_NODE_NAME=validation \
 K8S_POD_NAME=validation K8S_NAMESPACE=observability \
 .cache/otelcol validate --config=.cache/rendered/splunk-collector.yaml
```

Rendering checks every component and Kustomization, keeps the configured Splunk metrics exporter, verifies the scaling exporter, and checks that Predictor cannot drive nginx scaling. Chart archive checksums are committed in `charts.lock.json`.

## Observe metrics and scaling

```sh
export KUBECONFIG="$PWD/.local/kubeconfig"
kubectl --context mobility-global -n ingress-system port-forward svc/haproxy-kubernetes-ingress 8080:80
# In another terminal:
curl -H 'Host: nginx.demo.local' http://127.0.0.1:8080/
```

Use a separate local-only port-forward for the scaler:

```sh
kubectl --context mobility-global -n keda port-forward svc/otel-scaler 19090:9090
curl --fail http://127.0.0.1:19090/memstore/names
curl --fail -H 'Content-Type: application/json' --data \
 '{"query":"sum(haproxy_backend_http_requests_total{workload_namespace=demo,workload=nginx})","operationOverTime":"rate"}' \
 http://127.0.0.1:19090/memstore/query
```

Confirm fresh samples, two HAProxy producer identities, and actual backend label `proxy=demo_svc_nginx_http`. `producer_uid` preserves identity across pod replacement. The scaling filter drops all other metrics/backends before adding workload labels. Verify the stored metric name; do not treat HTTP 200 or a zero value alone as proof of healthy ingestion.

Policy: total RPS / 20 RPS per nginx replica; bounds 1–5, 15s polling, 60s rate retention and 60s HPA downscale stabilization. Missing-metric errors are enabled. Stale data and exporter errors must be checked independently. Ports: 4317 OTLP ingestion, 4318 external scaler protocol, 9090 diagnostics.

## Bounded load test through Flux

The load-test Flux Kustomization is opt-in and starts suspended. Set its `spec.suspend: false` in `clusters/mobility-global/reconciliation.yaml`, then commit/push. The Job sends 60 RPS for three minutes, then 120 RPS for three minutes, and exits. Expect about three replicas, a cap of five, then return to one after idle samples and stabilization. Confirm achieved rate and no dropped k6 iterations.

Watch with `kubectl --context mobility-global -n demo get deploy,scaledobject,hpa -w` and read `job/nginx-loadtest-01` logs. Remove the overlay through Git afterward. For another run, change the Job name in Git; do not mutate an existing Job's immutable template. The Job has a 480s deadline and no retries.

## Verify recording and independence

```sh
kubectl --context mobility-global -n demo get mp nginx-recording -o yaml
kubectl --context mobility-global -n keda exec postgres-0 -- \
 psql -U predictor -d predictor -c \
 "SELECT count(*), min(timestamp), max(timestamp) FROM metrics WHERE name='nginx-rps';"
```

Counts and timestamps must advance and recorded RPS must follow load. Verify history survives PostgreSQL/Predictor pod replacement. To prove independence, commit Predictor `replicaCount: 0`, repeat traffic, verify OTel/HPA scaling continues, then revert the commit. The nginx ScaledObject must retain only its named `measured` external OTel trigger.

Confirm fresh HAProxy and cluster metrics in Splunk. The MP contains no synthetic CSV seed; early training may report insufficient data without invalidating successful recording.

## Rollback and cleanup

Revert configuration commits and let Flux reconcile. Do not reset the scaler store or delete the PostgreSQL PVC to diagnose ingestion. Deleting resource group `mobility-global` is a separate, deliberate teardown that destroys the cluster and its persisted data; export any recording history first. No automatic cloud teardown is configured.

HTTP Scaler, ScaleOps coexistence testing, Humanitec and Kgateway are follow-up work. Keep ScaleOps replica optimization off this demo until shared ownership of the generated HPA has been tested.

## Optional staged bootstrap

`python3 scripts/prepare-secrets.py --kedify-only` encrypts only the Kedify and PostgreSQL credentials and enables their Flux dependency. Splunk stays suspended until backend credentials are supplied. For the configured Cloud Platform trial, run again with `--platform`; for the optional Observability profile, use a realm/ingest token and no backend flag. Commit all generated Secret/settings files, `.sops.yaml`, and reconciliation changes. A Cloud Platform HEC token is not an Observability ingest token.

The live Agent 0.8.3 controller also needs cluster-wide read-only ConfigMap discovery with optional autowiring RBAC disabled; this is granted explicitly through `agent.extraRbacRules`. Self-update and component-install permissions remain disabled.

## Configured Splunk backend

This demo uses the supplied **Splunk Cloud Platform trial**, whose console is https://prd-p-hzecp.splunkcloud.com. It is a different product from the customer’s **Observability Cloud** backend. No realm is needed for this backend.

- HEC endpoint: `https://prd-p-hzecp.splunkcloud.com:8088/services/collector/event` (verified directly against this trial).
- Metrics index: `mobility_metrics`, capped at 1024 MB and seven days of retention.
- Token: `mobility-otel`, enabled and restricted to that index; its value is SOPS-encrypted in Git.
- Both the normal Collector metrics pipelines and `metrics/haproxy` export through `splunk_hec/platform_metrics`; the separate `metrics/scaling` branch still exports OTLP/gRPC to the OTel Scaler.
- Logs and traces are disabled for this metrics-only trial token. The Collector’s bundled Kubernetes/node metrics collection remains enabled.
- This trial presents `CN=SplunkServerDefaultCert`, signed by `SplunkCommonCA`, with no hostname/SAN extension. `splunkPlatform.insecureSkipVerify: true` is deliberately limited to this disposable trial destination. Production must use a correctly issued certificate and enable verification.

Use `SPLUNK_HEC_ENDPOINT` and `SPLUNK_HEC_TOKEN` in `.env`, then run `scripts/prepare-secrets.py --platform`. To reproduce the customer’s Observability destination instead, replace the release’s `spec.values` with `infrastructure/splunk/observability-values.yaml`, supply a real `SPLUNK_REALM` and ingest-capable `SPLUNK_ACCESS_TOKEN`, and run `scripts/prepare-secrets.py` without `--platform`. Preserve the existing chart/release identity. The Observability profile keeps the same selected metric and scaling branch.

Inspect the token under **Settings → Data Inputs → HTTP Event Collector**, and the index under **Settings → Indexes**. A verification metric was indexed successfully:

```spl
| mstats latest(_value) AS value WHERE index=mobility_metrics AND metric_name=mobility_setup_check
```

For incoming HAProxy counter values:

```spl
| mstats latest(_value) AS requests WHERE index=mobility_metrics AND metric_name=haproxy_backend_http_requests_total BY proxy,pod
```

The configured Splunk report is **[Mobility Global - nginx request rate](https://prd-p-hzecp.splunkcloud.com/en-GB/app/search/report?s=Mobility%20Global%20-%20nginx%20request%20rate)**. Its SPL is tracked in `splunk/reports/nginx-request-rate.spl`; it sums per-producer counter rates in 30-second buckets. Sign in with your existing console account to open it.

For this small two-node reproduction, HAProxy reserves 100m CPU per pod, OTel Scaler 100m, and Splunk node agents 100m each. The stock reservations left insufficient scheduling space despite low observed usage. These are demo settings, not production sizing recommendations.

## Verified deployment — 2026-10-29

All six HelmReleases are Ready. HAProxy metrics are fresh in both Splunk and OTel Scaler, and the exact scaling query is verified against real stored data. The named `measured` external trigger remains the only trigger on nginx.

The GitOps k6 Job completed **32,401 requests, zero failures, zero dropped iterations**. nginx reached **3 replicas at about 60 RPS**, then **5 replicas at about 120 RPS**, and returned to **1 replica** after traffic stopped. During the 120-RPS phase Predictor was at zero replicas and nginx still reached five, proving that the forecast model does not drive scaling. Predictor was restored to one replica and recording resumed.

PostgreSQL history survived replacement of its pod on the existing Azure Disk PVC. The final check found 21 recorded samples, including values near 120 RPS and idle zeroes. MetricPredictor is Ready with its KEDA source subscribed. See `verification/results.json` for sampled observations. The load-test Kustomization is suspended again; the completed Job/logs remain available for inspection.
