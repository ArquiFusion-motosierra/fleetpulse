# AWS Core Pipeline (T05, T09, T14) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build Benjamin's AWS side of FleetPulse so that a telemetry batch from the simulator becomes a scored anomaly, an `incident-flow` execution, a registry record, domain events on SNS and four role-based emails, with everything deployable by `serverless deploy` once Learner Lab credentials exist.

**Architecture:** One Lambda layer (`fp-shared`) carries the shared library `fplib` plus `jsonschema` and `google-auth`, and bundles the contracts and `ids.yaml`. Four Serverless services sit on top: `fp-core` (tables, bucket, topics, HTTP API, `ingest`, `archiver`), `fp-outbox-publisher` (outbox stream → SNS), `fp-scoring` (state stream → score → StartExecution) and `fp-incidents` (Step Functions Standard `incident-flow` as a raw CloudFormation resource with an ASL file, plus approvals and close APIs). `serverless-compose.yml` orders them. Everything runs with `LabRole`.

**Tech Stack:** Python 3.12 arm64, Serverless Framework 3 (`npx serverless@3`), boto3, jsonschema 4, google-auth, pytest + moto 5 for tests. No plugins.

**Spec:** `docs/architecture.md` (flow, data ownership, Well-Architected decisions), `contracts/*.schema.json` + `contracts/secrets.md` + `contracts/README.md` (frozen names), backlog issues T05 (#5), T09 (#8), T14 (#12). The original design document lives in the private `backlog` repo.

## Global Constraints

- Every `serverless.yml`: `provider.iam.role: arn:aws:iam::${aws:accountId}:role/LabRole`, no `iamRoleStatements`, `runtime: python3.12`, `architecture: arm64`, `region: us-east-1`, `logRetentionInDays: 14`, `stage: ${opt:stage, 'dev'}`. Crons are `schedule` events, never EventBridge Scheduler. Service names `fp-<name>`.
- Fixed resource names (contracts/secrets.md): tables `fp-state`, `fp-outbox`, `fp-inbox`, `fp-registry`; bucket `fp-raw-${aws:accountId}`; topics `fp-domain-events`, `fp-notify` (attribute `role` ∈ technician, supervisor, management, operator), `fp-alerts`; queues `gcp-events-q`, `registry-q` (owned by their consumers); state machines `incident-flow`, `daily-planning`; SSM `/fleetpulse/api/url`, `/fleetpulse/sim/scenarios`, `/fleetpulse/layers/shared`; secrets `fleetpulse/api/ingest-key`, `fleetpulse/api/console-key`, `fleetpulse/gcp/sa-invoker`.
- Event envelope and payloads validate against `contracts/envelope.schema.json`, `incident.schema.json`, `part.schema.json`. `incident_id = INC-<machine_id>-<YYYYMMDDTHHMM of the 15-min window start, UTC>` and equals the Step Functions execution name. `severity`: observe ≥ 1.2, schedule ≥ 2.0, immediate ≥ 3.5. Timestamps ISO 8601 UTC with `Z`.
- Secrets are read through `fplib.secrets.get_json` (5-min cache), never from env vars; never logged. Every log line is one JSON object and carries `incident_id` or `batch_id` when known.
- Every cross-cloud or external call has an explicit timeout; Cloud Run `/explain` 8 s and a template fallback; the AI never blocks assignment.
- `fp-state` and `fp-registry`: on-demand, PITR on, `DeletionProtectionEnabled: true`, stream `NEW_IMAGE`, TTL attribute `ttl`. Reading items TTL 3 days, score items 3 days, cell anomalies 2 h, context 24 h, outbox 7 days, inbox 7 days.
- Tests: pytest, moto 5 (`mock_aws`), no network. Run from `aws/<service>/` with `../../.venv/bin/pytest -q tests`. Each service has `conftest.py` that puts the service root and `../shared/python` on `sys.path` (same pattern as `simulator/conftest.py`).
- Commits: Conventional Commits with scope, e.g. `feat(aws): ...`, pushed directly to `main` after `git pull --rebase`. Teammates push to `main` continuously: never force-push, never touch `simulator/`, `gcp/`, `oci/`, `aws/integrations/oci-writer/`, `aws/registry/`.
- Python style: `from __future__ import annotations`, module docstring stating what the Lambda does and its idempotency, boto3 clients created lazily (importable without AWS), pure functions separated from handlers so tests inject fakes (see `aws/integrations/oci-writer/ociw/handler.py`).

## Review Focus

1. A batch replayed by the simulator's Lambda retry (same `batch_id`) must return 200 `{"status":"duplicate"}` without writing readings twice → Task 2 `test_ingest_duplicate_batch_is_ignored`.
2. A reading with `ts` older than the machine's `last_ts` (out-of-order delivery) must still be stored but must not move `META.last_ts`, `lat`, `lon` backwards → Task 2 `test_meta_never_moves_backwards`.
3. Two score batches for the same machine inside one 15-min window must produce exactly one execution (second `StartExecution` gets `ExecutionAlreadyExists` and is swallowed) → Task 4 `test_same_window_starts_one_execution`.
4. A machine with fewer than 50 readings and no stored baseline must not be scored (no false alarm at cold start) → Task 4 `test_no_baseline_no_score`.
5. `POST /approvals/{token}` called twice (double click on the email link) must answer 200 the second time without raising; `SendTaskSuccess` failing with `TaskTimedOut` is treated as already decided → Task 6 `test_approval_is_idempotent`.

---

## File Structure

```
aws/
├── serverless-compose.yml                 # order: shared → core → outbox-publisher, scoring, incidents, simulator (../simulator)
├── shared/                                # Task 1 · layer fp-shared
│   ├── serverless.yml                     # service fp-shared: LayerVersion + SSM /fleetpulse/layers/shared
│   ├── build.sh                           # pip (aarch64 wheels) + copy contracts + ids.yaml→ids.json into python/fplib/data
│   ├── requirements.txt                   # jsonschema==4.23.0, google-auth==2.36.0, requests==2.32.3
│   ├── python/fplib/{__init__,clock,secrets,schema,outbox,inbox,geohash,ids,envelope,log}.py
│   ├── conftest.py
│   └── tests/test_fplib.py
├── ingest/                                # Task 2 · service fp-core
│   ├── serverless.yml                     # tables, bucket, topics, HttpApi, SSM api url, ingest-key secret, archiver ESM
│   ├── api/ingest.py                      # POST /telemetry, POST /context
│   ├── consumidores/archiver.py           # fp-state stream (R#) → s3://fp-raw/telemetry/dt=/hr=/*.ndjson.gz
│   ├── lib/{auth,state,storage}.py
│   ├── conftest.py
│   └── tests/{test_ingest,test_archiver}.py
├── integrations/outbox-publisher/         # Task 3 · service fp-outbox-publisher
│   ├── serverless.yml, pub/handler.py, conftest.py, tests/test_publisher.py
├── scoring/                               # Task 4 · service fp-scoring
│   ├── serverless.yml
│   ├── scoring/{handler,score,baselines,failure_mode}.py
│   ├── conftest.py
│   └── tests/{test_score,test_handler}.py
└── incidents/                             # Tasks 5–6 · service fp-incidents
    ├── serverless.yml                     # AWS::StepFunctions::StateMachine incident-flow + Lambdas + HttpApi routes
    ├── statemachine/incident-flow.asl.json
    ├── flow/{classify,check_existing,correlate_zone,create_incident,explain_cause,rank_technicians,
    │        wait_for_approval,assign_technician,render_messages,reserve_parts}.py
    ├── api/{approvals,close}.py
    ├── lib/{registry,technicians,templates,gcp_auth}.py
    ├── conftest.py
    └── tests/{test_flow_v0,test_flow_v1,test_api,test_asl}.py
scripts/subscribe-notify.sh                # Task 7
scripts/smoke.sh                           # Task 7: stage_ingest implemented
```

### Data model locked here (other tasks and Alex's `aws/registry/` depend on it)

`fp-state` (PK, SK; GSI1 on GSI1PK, GSI1SK):

| Item | PK | SK | Attributes | GSI1PK / GSI1SK |
|---|---|---|---|---|
| Machine | `MACHINE#<id>` | `META` | `machine_id, model, serial, zone_id, lat, lon, geohash6, status (ok/observe/schedule/immediate), hours_total, hours_idle, last_ts, baseline {var:{mu,sigma}}, baseline_n, updated_at` | `ZONE#<zone>` / `MACHINE#<id>` |
| Reading | `MACHINE#<id>` | `R#<ts>` | all reading fields + `batch_id`, `ttl` | — |
| Score | `MACHINE#<id>` | `S#<ts>` | `score, severity, z {var:{self,peer}}, means {var:value}, batch_id, ttl` | — |
| Peer baseline | `MODEL#<model>` | `BASELINE` | `baseline {var:{mu,sigma}}, updated_at` | — |
| Cell anomaly | `CELL#<geohash6>` | `A#<ts>#<machine_id>` | `machine_id, score, severity, deviated_vars, ttl` | — |
| Context | `CTX#<zone_id>` | `<ts>#<context_id>` | fields of `fuel.refill.schema.json`, `ttl` | — |
| Technician | `TECH#<id>` | `META` | `tech_id, name, specialty, zone_id, lat, lon, available (bool), current_incident` | `ZONE#<zone>` / `TECH#<id>` |
| Export watermark | `EXPORT#gcp` | `WATERMARK` | `last_hour` | — |

`fp-registry` (PK, SK; GSI1 on GSI1PK, GSI1SK), owned by Alex's `registry` Lambda for writes except where noted:

| Item | PK | SK | Written by | GSI1PK / GSI1SK |
|---|---|---|---|---|
| Incident | `INCIDENT#<incident_id>` | `META` | `create_incident` (open), `assign_technician`, `close` API | `MACHINE#<id>` / `INC#<opened_at>` and status lookups via `STATUS#<open\|closed>` on GSI2 (GSI2PK/GSI2SK = `STATUS#open` / `<opened_at>`) |
| Incident event | `INCIDENT#<incident_id>` | `EV#<occurred_at>#<event_id>` | any writer, same transaction as the outbox put | — |
| Stock | `STOCK#<part_id>#<zone_id>` | `META` | seed + registry Lambda (`on_hand, reserved, reorder_point`) | — |
| Reservation | `RSV#<incident_id>` | `<part_id>#<zone_id>` | registry Lambda | — |
| Order / reorder / KPI | `ORDER#…`, `REORDER#…`, `KPI#<day>` | … | daily-planning via registry Lambda | — |

`fp-outbox` item (Alex already writes this shape in `oci-writer`): `{event_id (PK), type, aggregate_id, incident_id, payload (full envelope as JSON string), created_at, ttl}`. `fp-inbox` item: `{message_id (PK), consumer, processed_at, ttl}`; `message_id` for ingest is `batch:<batch_id>`.

---

### Task 1: Shared layer `fp-shared` and `fplib`

**Files:**
- Create: `aws/shared/serverless.yml`, `aws/shared/build.sh`, `aws/shared/requirements.txt`, `aws/shared/conftest.py`
- Create: `aws/shared/python/fplib/__init__.py`, `clock.py`, `log.py`, `secrets.py`, `schema.py`, `outbox.py`, `inbox.py`, `geohash.py`, `ids.py`, `envelope.py`
- Test: `aws/shared/tests/test_fplib.py`
- Modify: `.gitignore` (add `aws/shared/python/jsonschema*`, `aws/shared/python/google*`, `aws/shared/python/*.dist-info`, `aws/shared/python/fplib/data/`, `aws/shared/python/fplib/contracts/` — build outputs)

**Interfaces (Produces):**
- `fplib.clock.now() -> datetime` (UTC, patchable in tests); `fplib.clock.iso(dt) -> str` (`%Y-%m-%dT%H:%M:%SZ`); `fplib.clock.parse(s) -> datetime`; `fplib.clock.window15(dt) -> str` → `YYYYMMDDTHHMM` of the window start.
- `fplib.log.event(msg: str, **fields) -> None` logs one JSON line at INFO; `fplib.log.error(msg, **fields)`.
- `fplib.secrets.get_json(name: str, ttl_s: int = 300, client=None) -> dict`; cache keyed by name; `fplib.secrets.clear_cache()`.
- `fplib.schema.validate(contract: str, obj: dict) -> None` raises `fplib.schema.ContractError(message, path)`; contracts loaded from `fplib/contracts/<contract>.schema.json` (bundled by `build.sh`) or, when absent, from `<repo>/contracts/` (dev/test path discovered by walking up to a dir containing `contracts/`). Uses `jsonschema.Draft202012Validator` with `FormatChecker()`.
- `fplib.outbox.put_event(table, envelope: dict, ttl_days: int = 7) -> None` → conditional put `attribute_not_exists(event_id)`; `fplib.outbox.transact_items(envelope) -> dict` returns the `Put` element for `TransactWriteItems`.
- `fplib.inbox.claim(table, message_id: str, consumer: str, ttl_days: int = 7) -> bool` → True when first time (conditional put), False on `ConditionalCheckFailedException`.
- `fplib.geohash.encode(lat: float, lon: float, precision: int = 6) -> str` (standard base32 geohash; no external lib).
- `fplib.ids.incident_id(machine_id: str, ts: datetime) -> str`; `fplib.ids.group_id(geohash6: str, ts: datetime) -> str` (`GRP-<geohash6>-<YYYYMMDDTHH>`); `fplib.ids.load() -> dict` (ids.yaml content from `fplib/data/ids.json` or repo `contracts/ids.yaml`); `fplib.ids.machines() -> dict[str, dict]`, `technicians() -> list[dict]`, `zones() -> dict[str, dict]`, `bom_for(model: str, failure_mode: str) -> list[dict]` (`[{part_id, qty}]`), `specialty_for(failure_mode: str) -> str`.
- `fplib.envelope.build(event_type: str, source: str, aggregate_type: str, aggregate_id: str, payload: dict, *, incident_id: str | None = None, machine_id: str | None = None, occurred_at: datetime | None = None, trace_id: str | None = None) -> dict` → sets `event_id = uuid4`, `schema_version = 1`, validates with `fplib.schema.validate("envelope", ...)` and, for `incident.*` / `part.*`, with the matching contract.

- [ ] **Step 1: Write failing tests** in `aws/shared/tests/test_fplib.py`:

```python
def test_window15_and_incident_id():
    ts = clock.parse("2026-11-15T14:38:05Z")
    assert clock.window15(ts) == "20261115T1430"
    assert ids.incident_id("EX-017", ts) == "INC-EX-017-20261115T1430"

def test_geohash_known_point():
    assert geohash.encode(57.64911, 10.40744, 6) == "u4pruy"   # reference vector
    assert len(geohash.encode(-13.9003, -71.5611, 6)) == 6

def test_envelope_validates_incident_opened():
    payload = json.load(open(REPO/"contracts/examples/envelope/incident-opened.json"))["payload"]
    env = envelope.build("incident.opened", "aws.incident-flow", "incident", "INC-EX-017-20261115T1430",
                         payload, incident_id="INC-EX-017-20261115T1430", machine_id="EX-017")
    assert env["schema_version"] == 1 and uuid.UUID(env["event_id"]).version == 4

def test_envelope_rejects_bad_payload():
    with pytest.raises(schema.ContractError):
        envelope.build("incident.opened", "aws.incident-flow", "incident", "INC-EX-017-20261115T1430", {}, incident_id="INC-EX-017-20261115T1430", machine_id="EX-017")

@mock_aws
def test_inbox_claim_once_and_outbox_conditional():
    ... create tables fp-inbox (message_id) and fp-outbox (event_id) with boto3 ...
    assert inbox.claim(inbox_tbl, "batch:B-1", "ingest") is True
    assert inbox.claim(inbox_tbl, "batch:B-1", "ingest") is False
    outbox.put_event(outbox_tbl, env); with pytest.raises(ClientError): outbox.put_event(outbox_tbl, env)

def test_bom_and_specialty():
    assert ids.bom_for("EX", "hydraulic_pump") and ids.specialty_for("hydraulic_pump") == "hydraulics"

def test_secrets_cached(monkeypatch):
    fake = FakeSecrets({"fleetpulse/api/ingest-key": '{"api_key":"k"}'})
    assert secrets.get_json("fleetpulse/api/ingest-key", client=fake)["api_key"] == "k"
    assert secrets.get_json("fleetpulse/api/ingest-key", client=fake)["api_key"] == "k" and fake.calls == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd aws/shared && ../../.venv/bin/pytest -q tests` → Expected: ImportError / failures.

- [ ] **Step 3: Implement `fplib` modules** with the signatures above. `schema.py` resolves the contracts dir once; `ids.load()` prefers `fplib/data/ids.json`, falls back to `contracts/ids.yaml` via PyYAML only in dev (import inside the function). `secrets.py` uses `time.monotonic()`.

- [ ] **Step 4: Write `aws/shared/build.sh`** (bash, `set -euo pipefail`): `pip install -r requirements.txt --platform manylinux2014_aarch64 --python-version 3.12 --only-binary=:all: --target python/ --upgrade`; copy `../../contracts/*.schema.json` to `python/fplib/contracts/`; convert `../../contracts/ids.yaml` to `python/fplib/data/ids.json` with the venv or system python (`python3 -c 'import yaml,json,sys; json.dump(yaml.safe_load(open(sys.argv[1])), open(sys.argv[2],"w"))'`). Print the layer size.

- [ ] **Step 5: Write `aws/shared/serverless.yml`**: service `fp-shared`; `layers.shared: {path: ., name: fp-shared, compatibleRuntimes: [python3.12], compatibleArchitectures: [arm64], retain: false}` with `package.patterns: ["!**", "python/**"]`; `resources.Resources.SharedLayerParam: AWS::SSM::Parameter {Name: /fleetpulse/layers/shared, Type: String, Value: {Ref: SharedLambdaLayer}}`. Add a top comment explaining other services consume `${ssm:/fleetpulse/layers/shared}`.

- [ ] **Step 6: Run tests, lint, package**

Run: `cd aws/shared && ../../.venv/bin/pytest -q tests && ../../.venv/bin/ruff check python tests && bash build.sh && npx -y serverless@3 package --stage ci`
Expected: tests PASS, ruff clean, `.serverless/` contains the layer zip (no credentials needed for `package`).

- [ ] **Step 7: Commit**

```bash
git add aws/shared .gitignore && git commit -m "feat(aws): fp-shared layer with fplib (contracts, outbox/inbox, ids, geohash, secrets cache)" && git pull --rebase origin main && git push origin main
```

---

### Task 2: Core stack `fp-core`: tables, bucket, topics, HTTP API, `ingest` and `archiver`

**Files:**
- Create: `aws/ingest/serverless.yml`, `aws/ingest/api/ingest.py`, `aws/ingest/consumidores/archiver.py`, `aws/ingest/lib/auth.py`, `aws/ingest/lib/state.py`, `aws/ingest/lib/storage.py`, `aws/ingest/conftest.py`
- Test: `aws/ingest/tests/test_ingest.py`, `aws/ingest/tests/test_archiver.py`

**Interfaces:**
- Consumes: `fplib.schema.validate`, `fplib.inbox.claim`, `fplib.secrets.get_json`, `fplib.geohash.encode`, `fplib.clock`.
- Produces (CloudFormation, consumed by name by every other service): tables `fp-state` (GSI1), `fp-outbox`, `fp-inbox`, `fp-registry` (GSI1, GSI2), bucket `fp-raw-${aws:accountId}` (versioned, lifecycle: `telemetry/` and `events/` to GLACIER_IR at 90 days), topics `fp-domain-events`, `fp-notify`, `fp-alerts`, HttpApi `fp-api` exported as `fp-core-HttpApiId` (CloudFormation Output `HttpApiId` + `HttpApiUrl`) so `fp-incidents` can attach routes to the same API, SSM `/fleetpulse/api/url` = `!GetAtt HttpApi.ApiEndpoint`, secret `fleetpulse/api/ingest-key` with `GenerateSecretString {SecretStringTemplate: "{}", GenerateStringKey: "api_key", ExcludePunctuation: true, PasswordLength: 40}`, and `fleetpulse/api/console-key` likewise.
- `lib/auth.py`: `check_api_key(headers: dict, secret_name: str) -> bool` (case-insensitive `x-api-key`, `hmac.compare_digest`).
- `lib/state.py`: `put_readings(table, batch: dict, now: datetime) -> int` (batch_writer, `ttl = ts + 3d`), `update_meta(table, latest_by_machine: dict[str, dict], now) -> None` (UpdateItem with `ConditionExpression attribute_not_exists(last_ts) OR last_ts < :ts`, swallow `ConditionalCheckFailedException`), `put_context(table, items: list[dict]) -> int`.
- `lib/storage.py`: `ndjson_gz(records: list[dict]) -> bytes`, `object_key(hour_ts: str, first_ts: str) -> str` → `telemetry/dt=YYYY-MM-DD/hr=HH/<first_ts_compact>-<8hex>.ndjson.gz`.
- `api/ingest.py`: `handler(event, context=None, state=None, inbox=None, secrets=None)` routing on `event["rawPath"]`; responses `{"statusCode", "headers": {"content-type":"application/json"}, "body"}`: 401 bad key, 400 `{"error": "<ContractError message>", "path": [...]}`, 200 `{"status":"duplicate","batch_id"}`, 202 `{"status":"accepted","batch_id","readings": n}`; `/context` 202 `{"status":"accepted","context": n}`.
- `consumidores/archiver.py`: `handler(event, context=None, s3=None)`; groups records by `hour(ts)`; one object per hour group; returns `{"objects": n, "records": m}`; `batchItemFailures` not used (S3 put failure raises so the whole batch retries; bisect on error enabled in ESM).

- [ ] **Step 1: Write failing tests** in `tests/test_ingest.py` (moto `mock_aws`, create the four tables with the key schema above, fake secrets client with `api_key = "k"`), using `contracts/examples/telemetry-batch/live.json` and `with-fault.json`:

```python
def test_rejects_missing_api_key(): assert handler(api_event("/telemetry", body, key=None), **deps)["statusCode"] == 401
def test_rejects_invalid_batch(): r = handler(api_event("/telemetry", {"batch_id": "x"}, key="k"), **deps); assert r["statusCode"] == 400 and "error" in json.loads(r["body"])
def test_accepts_batch_writes_readings_and_meta():
    r = handler(api_event("/telemetry", live, key="k"), **deps); assert r["statusCode"] == 202
    items = query(state, "MACHINE#EX-017"); assert any(i["SK"].startswith("R#") for i in items)
    meta = get(state, "MACHINE#EX-017", "META"); assert meta["geohash6"] and meta["last_ts"] == max(ts of EX-017 readings) and meta["GSI1PK"] == "ZONE#NORTE"
def test_ingest_duplicate_batch_is_ignored():
    handler(api_event("/telemetry", live, key="k"), **deps); r = handler(api_event("/telemetry", live, key="k"), **deps)
    assert r["statusCode"] == 200 and json.loads(r["body"])["status"] == "duplicate" and count_readings(state, "EX-017") == 10
def test_meta_never_moves_backwards():
    handler(api_event("/telemetry", live, key="k"), **deps); handler(api_event("/telemetry", older_batch(live, minutes=-30), key="k"), **deps)
    assert get(state, "MACHINE#EX-017", "META")["last_ts"] == original_last_ts
def test_context_stored_under_zone():
    r = handler(api_event("/context", [refill_example, weather_example], key="k"), **deps); assert r["statusCode"] == 202
    assert query(state, "CTX#NORTE")
```

`tests/test_archiver.py`:

```python
def test_archiver_writes_one_gz_object_per_hour():
    event = stream_event(readings_at(["2026-11-15T14:35:00Z", "2026-11-15T14:36:00Z", "2026-11-15T15:01:00Z"]))
    out = archiver.handler(event, s3=s3)  # moto bucket fp-raw-123456789012 via env BUCKET
    assert out == {"objects": 2, "records": 3}
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]]
    assert any(k.startswith("telemetry/dt=2026-11-15/hr=14/") for k in keys) and all(k.endswith(".ndjson.gz") for k in keys)
    assert json.loads(gzip.decompress(s3.get_object(...)["Body"].read()).splitlines()[0])["machine_id"]
def test_archiver_ignores_non_reading_items(): assert archiver.handler(stream_event([meta_item]), s3=s3) == {"objects": 0, "records": 0}
```

- [ ] **Step 2: Run tests to verify they fail** → `cd aws/ingest && ../../.venv/bin/pytest -q tests`

- [ ] **Step 3: Implement `lib/auth.py`, `lib/state.py`, `lib/storage.py`, `api/ingest.py`, `consumidores/archiver.py`.** DynamoDB stream images are in the typed format: deserialize with `boto3.dynamodb.types.TypeDeserializer`; convert `Decimal` to float/int when serializing NDJSON. Readings are stored with numbers as `Decimal` (use `json.loads(body, parse_float=Decimal)`).

- [ ] **Step 4: Write `aws/ingest/serverless.yml`**: service `fp-core`; `provider.httpApi: {name: fp-api, cors: false}`; functions `ingest` (handler `api/ingest.handler`, memory 512, timeout 29, layers `[${ssm:/fleetpulse/layers/shared}]`, events `httpApi: POST /telemetry` and `POST /context`, env `STATE_TABLE=fp-state INBOX_TABLE=fp-inbox API_KEY_SECRET=fleetpulse/api/ingest-key`) and `archiver` (handler `consumidores/archiver.handler`, memory 1024, timeout 300, env `BUCKET=fp-raw-${aws:accountId}`, event `stream: {type: dynamodb, arn: !GetAtt StateTable.StreamArn, batchSize: 1000, maximumBatchingWindow: 300, startingPosition: LATEST, bisectBatchOnFunctionError: true, maximumRetryAttempts: 3, filterPatterns: [{dynamodb: {NewImage: {SK: {S: [{prefix: "R#"}]}}}}]}`). `package.patterns: ["!**", "api/**", "consumidores/**", "lib/**", "!**/__pycache__/**"]`. Resources: the four tables (`BillingMode: PAY_PER_REQUEST`, `PointInTimeRecoverySpecification`, `DeletionProtectionEnabled: true`, `StreamSpecification NEW_IMAGE` on fp-state, fp-outbox and fp-registry, `TimeToLiveSpecification ttl`), bucket with `VersioningConfiguration Enabled`, `PublicAccessBlockConfiguration` all true, lifecycle rule GLACIER_IR at 90 days, three topics, `AWS::SSM::Parameter` for `/fleetpulse/api/url`, two `AWS::SecretsManager::Secret`. Outputs: `HttpApiId`, `HttpApiUrl`, `StateTableStreamArn`, `OutboxTableStreamArn`, `RegistryTableStreamArn`, `DomainEventsTopicArn`, `NotifyTopicArn`, `AlertsTopicArn`.

- [ ] **Step 5: Run tests, lint, package** → `cd aws/ingest && ../../.venv/bin/pytest -q tests && ../../.venv/bin/ruff check . && npx -y serverless@3 package --stage ci` (set `SLS_SSM_PLACEHOLDER` is not a thing: for `package` without credentials, `${ssm:...}` resolution fails; wrap as `${ssm:/fleetpulse/layers/shared, 'arn:aws:lambda:us-east-1:000000000000:layer:fp-shared:1'}` with that default so packaging works offline).
Expected: PASS, clean, `.serverless/cloudformation-template-update-stack.json` present.

- [ ] **Step 6: Commit** → `feat(aws): fp-core stack with ingest API, state tables, raw bucket and archiver (T05)`; pull --rebase; push.

---

### Task 3: `outbox-publisher`: fp-outbox stream → SNS `fp-domain-events`

**Files:**
- Create: `aws/integrations/outbox-publisher/serverless.yml`, `pub/handler.py`, `conftest.py`
- Test: `aws/integrations/outbox-publisher/tests/test_publisher.py`

**Interfaces:**
- Consumes: outbox item shape (`payload` = envelope JSON string, `type`, `aggregate_id`).
- Produces: SNS message = the envelope JSON string; `MessageAttributes`: `event_type` (String), `aggregate_type` (String), `incident_id` (String, only when present). Consumers (`gcp-events-q`, `registry-q`) subscribe with raw message delivery off and filter policies on `event_type` if they want.
- `pub.handler.handler(event, context=None, sns=None) -> {"batchItemFailures": [...]}` with `ReportBatchItemFailures` (`itemIdentifier = record["eventID"]`); only `INSERT` records; the handler never raises for one bad record.

- [ ] **Step 1: Write failing tests**: `test_publishes_insert_with_attributes` (fake sns records `publish` kwargs; assert `TopicArn`, `Message == payload`, attributes), `test_ignores_modify_and_remove`, `test_partial_failure_reports_event_id` (fake sns raising on the 2nd record → `batchItemFailures == [{"itemIdentifier": "evt-2"}]`).
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** handler; topic ARN from env `DOMAIN_TOPIC_ARN` default `arn:aws:sns:us-east-1:${accountId}` built at runtime from `context.invoked_function_arn` when env missing (keep simple: require env).
- [ ] **Step 4: `serverless.yml`**: service `fp-outbox-publisher`; function `publisher` with `stream` event on `arn:aws:dynamodb:${aws:region}:${aws:accountId}:table/fp-outbox/stream/*` is not allowed as wildcard → use `${cf:fp-core-${sls:stage}.OutboxTableStreamArn}` (with an offline default like Task 2 Step 5), `batchSize: 10`, `startingPosition: TRIM_HORIZON`, `functionResponseType: ReportBatchItemFailures`, `maximumRetryAttempts: 10`, `destinations.onFailure: arn:aws:sns:${aws:region}:${aws:accountId}:fp-alerts`. Env `DOMAIN_TOPIC_ARN: arn:aws:sns:${aws:region}:${aws:accountId}:fp-domain-events`. Layer from SSM.
- [ ] **Step 5: Run tests, ruff, `npx -y serverless@3 package --stage ci`.**
- [ ] **Step 6: Commit** → `feat(aws): outbox-publisher streams fp-outbox to SNS fp-domain-events (T05)`.

---

### Task 4: Scoring: `score` Lambda on the `fp-state` stream → `incident-flow` StartExecution (T09)

**Files:**
- Create: `aws/scoring/serverless.yml`, `scoring/score.py`, `scoring/baselines.py`, `scoring/failure_mode.py`, `scoring/handler.py`, `conftest.py`
- Test: `aws/scoring/tests/test_score.py`, `aws/scoring/tests/test_handler.py`

**Interfaces:**
- `scoring.score.VARS = ("coolant_temp_c","hydraulic_oil_temp_c","hydraulic_pressure_bar","vibration_mm_s","fuel_rate_lph","engine_rpm")`, `WEIGHTS = {hydraulic_pressure_bar:1.0, vibration_mm_s:1.0, coolant_temp_c:0.9, hydraulic_oil_temp_c:0.9, fuel_rate_lph:0.8, engine_rpm:0.5}`, `THRESHOLDS = {"observe":1.2,"schedule":2.0,"immediate":3.5}`.
- `scoring.score.batch_means(readings: list[dict]) -> dict[str, float]` (only readings with `engine_status != "off"`; empty → `{}`).
- `scoring.score.zscores(means, self_baseline, peer_baseline) -> dict[str, dict]` → `{var: {"self": z, "peer": z}}` (sigma floor 1e-6; missing var skipped).
- `scoring.score.trend_bonus(means, previous_means: list[dict]) -> int` → number of vars rising strictly across the last 3 batches (needs 2 previous).
- `scoring.score.compute(means, self_baseline, peer_baseline, previous_means) -> dict` → `{"score": float, "severity": "ok"|"observe"|"schedule"|"immediate", "z": {...}, "deviated_vars": [{"var","value","z"}] (|z|max ≥ 1.0, sorted by |z| desc)}`.
- `scoring.baselines.from_readings(readings: list[dict]) -> dict[str, dict] | None` → `{var: {"mu","sigma"}}` when ≥ 50 operating readings else None.
- `scoring.baselines.for_machine(table, machine_id: str, model: str) -> tuple[dict|None, dict|None]` → (self baseline from `META.baseline`, bootstrapping from the last 200 `R#` items and persisting `baseline`, `baseline_n` when missing; peer baseline from `MODEL#<model>/BASELINE` or `None`).
- `scoring.failure_mode.infer(deviated_vars: list[dict]) -> str` rule table: pressure↓(z<0)+(oil temp↑ or vibration↑) → `hydraulic_pump`; fuel↑ + (rpm deviated or coolant↑) → `fuel_contamination`; coolant↑ only → `engine_overheat`; hydraulic oil temp↑ only → `hydraulic_overheat`; vibration↑ only → `final_drive`; else `sensor_fault`.
- `scoring.handler.handler(event, context=None, table=None, sfn=None) -> {"scored": n, "started": [incident_ids]}`: groups stream `INSERT` records with `SK` prefix `R#` by `(machine_id, batch_id)`; for each group: baselines → `compute` → put `S#<last_ts>` item (ttl 3 d), update `META.status` to severity (or `ok`), when severity ≥ observe put `CELL#<geohash6>` / `A#<ts>#<machine>` (ttl 2 h), when `score ≥ 2.0` `start_execution(sfn, input)` with `name = incident_id`, swallowing `ExecutionAlreadyExists`.
- Execution input (consumed by Task 5): `{"incident_id","machine_id","model","serial","zone_id","lat","lon","geohash6","ts","batch_id","score","severity","z","deviated_vars","failure_mode","hours_total"}`.

- [ ] **Step 1: Write failing tests** in `test_score.py`: `test_zscores_and_weights` (hand-computed: pressure mu 300 σ 10 value 274 → z −2.6; score contribution 2.6), `test_trend_bonus_counts_three_rising`, `test_severity_thresholds` (1.19→ok, 1.2→observe, 2.0→schedule, 3.5→immediate), `test_failure_mode_hydraulic_pump`, `test_failure_mode_fuel_contamination`, `test_baseline_needs_50_readings`. In `test_handler.py` (moto DynamoDB with fp-state + fake sfn recording `start_execution` kwargs and raising `ExecutionAlreadyExists` on repeat): `test_no_baseline_no_score` (machine with 10 readings → no `S#` item, no start), `test_scores_and_starts_execution_when_drift` (seed META baseline + MODEL baseline, stream 10 readings with pressure at −3σ and oil temp +2.5σ → `S#` item, `CELL#` item, one start with `name == "INC-EX-017-<window>"` and `input.failure_mode == "hydraulic_pump"`), `test_same_window_starts_one_execution` (two batches in the window → sfn called twice, second raises, result `started` has one id, no exception).

- [ ] **Step 2: Run to verify fail.** `cd aws/scoring && ../../.venv/bin/pytest -q tests`
- [ ] **Step 3: Implement** the four modules.
- [ ] **Step 4: `serverless.yml`**: service `fp-scoring`; function `score` (memory 512, timeout 60, env `STATE_TABLE=fp-state STATE_MACHINE_ARN=arn:aws:states:${aws:region}:${aws:accountId}:stateMachine:incident-flow`), stream event on `${cf:fp-core-${sls:stage}.StateTableStreamArn}` (offline default), `batchSize: 1000`, `maximumBatchingWindow: 10`, `startingPosition: LATEST`, `parallelizationFactor: 2`, filter `SK prefix R#`, `bisectBatchOnFunctionError: true`, `maximumRetryAttempts: 3`, onFailure `fp-alerts`. Layer from SSM.
- [ ] **Step 5: Run tests, ruff, package.**
- [ ] **Step 6: Commit** → `feat(aws): scoring Lambda with self/peer z-scores, trend bonus and incident-flow start (T09)`.

---

### Task 5: `incident-flow` v0 (T09): Classify → CheckExisting → CreateIncident → RankTechnicians → Assign → Notify

**Files:**
- Create: `aws/incidents/serverless.yml`, `statemachine/incident-flow.asl.json`, `flow/classify.py`, `flow/check_existing.py`, `flow/create_incident.py`, `flow/rank_technicians.py`, `flow/assign_technician.py`, `flow/render_messages.py`, `lib/registry.py`, `lib/technicians.py`, `lib/templates.py`, `conftest.py`
- Test: `tests/test_flow_v0.py`, `tests/test_asl.py`

**Interfaces:**
- State machine input: Task 4's execution input. Every Lambda returns its input merged with its own result under a named key (`ResultPath`), so the ASL passes `$` along: `classify` → `$.classified {severity, needs_approval: bool}`; `check_existing` → `$.existing {found: bool, incident_id, escalated: bool}`; `create_incident` → `$.incident {incident_id, opened_at, recurrence_flag, group_id, zone_id}`; `rank_technicians` → `$.ranking {top: [{tech_id, name, distance_km, specialty_match, score}]}`; `assign_technician` → `$.assignment {tech_id, assigned_at, approval}`; `render_messages` → `$.messages {technician, supervisor, management, operator, subject}`.
- `lib/registry.py`: `open_incident_for(table, machine_id) -> dict | None` (GSI1 query `MACHINE#<id>` / begins_with `INC#`, filter `status = open`, newest first); `put_incident_tx(registry_tbl_name, outbox_tbl_name, incident_item: dict, envelope: dict, event_item: dict, client) -> None` (one `TransactWriteItems` with three `Put`s: META with `attribute_not_exists(PK)`, `EV#...`, outbox with `attribute_not_exists(event_id)`); `recent_closed_same_mode(table, machine_id, failure_mode, since: datetime) -> bool`; `update_incident(table, incident_id, **attrs)`.
- `lib/technicians.py`: `candidates(state_table, zone_hint: str) -> list[dict]` (all `TECH#` METAs via scan with `begins_with(PK,"TECH#")`; when the table has none, fall back to `fplib.ids.technicians()` with each tech at its zone centroid and `available=True`); `rank(cands, lat, lon, severity, failure_mode) -> list[dict]` score = `sev_w × (1/max(km,0.5)) × (2 if specialty == fplib.ids.specialty_for(failure_mode) else 1) × (1 if available else 0)` with `sev_w = {observe:1, schedule:2, immediate:3}`; haversine in km.
- `lib/templates.py`: `explanation(deviated_vars, failure_mode, machine_id) -> dict` (`{"source":"template","summary","probable_component","suggested_parts": [part_ids from BOM], "common_cause": None}`), `messages(incident: dict, explanation: dict, ranking_top: dict, approve_url: str | None) -> dict` with four distinct texts (technician: variables + z + location + parts; supervisor: machine, severity, technician proposed, approve link if any; management: impact in one line, only used when immediate or grouped; operator: a single instruction) and a `subject` `"[FleetPulse] <severity> <machine_id> <incident_id>"`.
- Lambdas: `classify.handler(event)`: severity from `event["score"]` (same thresholds), `needs_approval = severity == "schedule"`; `check_existing.handler(event, registry=None, outbox=None)`: if open incident exists and new severity > stored severity → write `incident.escalated` envelope to outbox + registry EV item, `update_incident(severity)`; returns `existing`; `create_incident.handler(event, client=None)`: builds `incident.opened` envelope (`source aws.incident-flow`, payload from input; `group_id` from `$.correlation.group_id` when present else `None`; `recurrence_flag` via `recent_closed_same_mode(... 7 days)`), writes with `put_incident_tx` (incident META: `status open, severity, score, failure_mode, machine_id, zone_id, lat, lon, opened_at, group_id, recurrence_flag, GSI1PK/GSI1SK, GSI2PK STATUS#open / GSI2SK opened_at`); `rank_technicians.handler(event, state=None)`; `assign_technician.handler(event, state=None, registry=None, outbox=None)`: tech = `$.approval.tech_id` if present else `$.ranking.top[0].tech_id`; approval = `$.approval.approval` if present else `"auto"`; `incident.assigned` envelope with `explanation = $.explanation` (Task 6) or `lib.templates.explanation(...)`; registry `update_incident(status open, tech_id, assigned_at, approval)`; fp-state `TECH#` `available=false, current_incident`; `render_messages.handler(event)`.
- ASL v0 (`statemachine/incident-flow.asl.json`, `DefinitionSubstitutions` for every Lambda ARN and the notify topic ARN): `ClassifySeverity` → `CheckExistingIncident` → Choice `AlreadyOpen?` (`$.existing.found == true` → `Succeed EscalatedOrIgnored`) → `CreateIncident` → `RankTechnicians` → `RenderMessages` → `AssignTechnician` → `NotifyByRole` (Parallel, 4 branches, each `arn:aws:states:::sns:publish` with `TopicArn`, `Subject.$ = $.messages.subject`, `Message.$ = $.messages.<role>`, `MessageAttributes: {role: {DataType: String, StringValue: <role>}}`; the management branch starts with a Choice: publish only when `$.classified.severity == immediate` or `$.incident.group_id` is present, else `Pass`) → `Succeed`. Every Lambda task: `Retry [{ErrorEquals: ["States.ALL"], IntervalSeconds: 2, MaxAttempts: 3, BackoffRate: 2}]`; a top-level `Catch` on each state → `PublishAlert` (`sns:publish` to `fp-alerts` with `$.Error` and `$$.Execution.Name`) → `Fail`. Tasks 6 inserts `CorrelateZone`, `ExplainCause`, `NeedsApproval`/`WaitForApproval`, `ReserveParts` into this file; design the v0 state names so they stay.

- [ ] **Step 1: Write failing tests** in `tests/test_flow_v0.py` (moto DynamoDB: fp-state, fp-registry, fp-outbox; sample input from a fixture matching Task 4's execution input for EX-017 with `score 2.31`): `test_classify_schedule_needs_approval`, `test_create_incident_writes_registry_and_outbox_atomically` (META + EV + outbox item exist; outbox payload validates against `incident.schema.json`; `recurrence_flag False`), `test_create_incident_sets_recurrence_when_same_mode_closed_recently`, `test_check_existing_escalates_open_incident` (open incident `schedule`, new `immediate` → `incident.escalated` in outbox, META severity updated, `existing.found True`), `test_rank_prefers_specialty_and_distance` (seed two TECH# items: hydraulics tech 2 km away vs engine tech 1 km → hydraulics first for `hydraulic_pump`), `test_rank_falls_back_to_ids_yaml_when_no_tech_items`, `test_assign_marks_tech_busy_and_emits_assigned` (TECH available false, outbox `incident.assigned` validates; approval `auto`), `test_messages_are_distinct_per_role` (four strings pairwise different, subject contains incident_id). In `tests/test_asl.py`: load the ASL JSON, assert `StartAt == "ClassifySeverity"`, every `Task` state of type Lambda has a `Retry` and a `Catch`, the Parallel has 4 branches, all `${...}` substitution keys appear in `serverless.yml` `DefinitionSubstitutions`.

- [ ] **Step 2: Run to verify fail.** `cd aws/incidents && ../../.venv/bin/pytest -q tests`
- [ ] **Step 3: Implement** libs, Lambdas and the ASL file.
- [ ] **Step 4: `serverless.yml`**: service `fp-incidents`; functions `classify`, `checkExisting`, `createIncident`, `rankTechnicians`, `renderMessages`, `assignTechnician` (memory 256, timeout 30, env `STATE_TABLE=fp-state REGISTRY_TABLE=fp-registry OUTBOX_TABLE=fp-outbox`, layer from SSM); resource `IncidentFlow: AWS::StepFunctions::StateMachine {StateMachineName: incident-flow, StateMachineType: STANDARD, RoleArn: arn:aws:iam::${aws:accountId}:role/LabRole, Definition: ${file(statemachine/incident-flow.asl.json)}, DefinitionSubstitutions: {ClassifyArn: !GetAtt ClassifyLambdaFunction.Arn, ..., NotifyTopicArn: arn:aws:sns:${aws:region}:${aws:accountId}:fp-notify, AlertsTopicArn: ...}, LoggingConfiguration: {Level: ALL, IncludeExecutionData: true, Destinations: [{CloudWatchLogsLogGroup: {LogGroupArn: !GetAtt IncidentFlowLogs.Arn}}]}}` plus `IncidentFlowLogs: AWS::Logs::LogGroup {LogGroupName: /aws/states/incident-flow, RetentionInDays: 14}`. Output `IncidentFlowArn`.
- [ ] **Step 5: Run tests, ruff, `npx -y serverless@3 package --stage ci`** (verify the generated template contains `AWS::StepFunctions::StateMachine` with the substituted definition).
- [ ] **Step 6: Commit** → `feat(aws): incident-flow v0 — classify, create, rank, assign, notify by role (T09)`.

---

### Task 6: `incident-flow` complete (T14): CorrelateZone, ExplainCause, approval by email, ReserveParts, close API

**Files:**
- Create: `flow/correlate_zone.py`, `flow/explain_cause.py`, `flow/wait_for_approval.py`, `flow/reserve_parts.py`, `api/approvals.py`, `api/close.py`, `lib/gcp_auth.py`
- Modify: `statemachine/incident-flow.asl.json`, `aws/incidents/serverless.yml`, `lib/templates.py` (supervisor message includes the approve link), `flow/assign_technician.py` (reads `$.approval`)
- Test: `tests/test_flow_v1.py`, `tests/test_api.py`

**Interfaces:**
- `correlate_zone.handler(event, state=None)` → `$.correlation {group_id: str|None, peers: [machine_id], context: {fuel_lots: [..], weather: [..]}}`: query `CELL#<geohash6>` items with `SK > A#<now-1h>`; distinct machines other than this one ≥ 1 (so ≥ 2 including self) → `group_id = fplib.ids.group_id(geohash6, now)`; context from `CTX#<zone_id>` last hour.
- `lib/gcp_auth.py`: `id_token(sa_key: dict, audience: str, now=None) -> str` using `google.oauth2.service_account.IDTokenCredentials` (`google-auth` from the layer); cached until 50 min.
- `explain_cause.handler(event, secrets=None, http=None)` → `$.explanation` = Cloud Run `POST /explain` JSON body `{hypotheses, probable_component, suggested_parts, confidence, common_cause, role_messages{technician,supervisor,management,operator}}` mapped to the contract's `explanation` object (`source: "gemini"`), with request body `{incident_id, machine_id, model, severity, deviated_vars, failure_mode, group_id, context, recent_incidents: []}`; secret `fleetpulse/gcp/sa-invoker` `{key, audience}`; `timeout 8 s`; on any exception, non-200, invalid JSON or missing fields → `lib.templates.explanation(...)` (`source: "template"`) and a log line `explain_fallback` with the reason. Env `INSIGHTS_URL` (the audience is the base URL; path `/explain`).
- `wait_for_approval.handler(event)` (invoked with `.waitForTaskToken`; input includes `token` and `approve_base_url` from `DefinitionSubstitutions`/env `API_URL` read from SSM `/fleetpulse/api/url`): publishes to `fp-notify` role `supervisor` a message with `GET <api>/approvals/<urlquoted token>?tech=<top tech_id>`; returns nothing (the task waits).
- `api/approvals.handler(event, sfn=None)`: `GET` or `POST /approvals/{token}`; `SendTaskSuccess(taskToken=unquoted, output=json {"approval":"approved","tech_id": query tech or null})`; on `TaskTimedOut`/`TaskDoesNotExist`/`InvalidToken` → 200 with body "Esta aprobación ya fue procesada"; success → 200 HTML "Aprobado: <tech_id> asignado a <incident_id>" (incident_id parsed from query `inc`).
- `reserve_parts.handler(event, outbox=None, registry=None)` → `$.reservation {requested: [{part_id, qty}]}`: lines = `fplib.ids.bom_for(model, failure_mode)` (fallback: `explanation.suggested_parts` with qty 1; if still empty → `requested: []` and no event); writes `part.reserve.requested` envelope (`aggregate_type part_reservation`, `aggregate_id RSV-<incident_id>`, `source aws.incident-flow`) to the outbox + registry EV item.
- `api/close.handler(event, state=None, registry=None, outbox=None, secrets=None)`: `POST /incidents/{incident_id}/close`, header `x-api-key` checked against `fleetpulse/api/console-key` (reuse `aws/ingest/lib/auth.py` logic by copying `check_api_key` into `aws/incidents/lib/auth.py`; do not import across services); body `{"closed_by","parts_used":[{part_id,zone_id,qty}],"cause_summary"}`; 404 unknown incident, 409 already closed, 200 `{"incident_id","downtime_h"}`; effects: registry META `status closed, closed_at, downtime_h = (closed_at - opened_at) hours rounded 2, closed_by, cause_summary, GSI2PK STATUS#closed`; TECH `available=true, current_incident=None`; outbox `incident.closed` (validates).
- ASL changes: after `CheckExistingIncident`'s Choice → `CorrelateZone` → `CreateIncident` → `ExplainCause` → `RankTechnicians` → `RenderMessages` → Choice `NeedsApproval` (`$.classified.needs_approval == true` → `WaitForApproval` else `AssignTechnician`); `WaitForApproval`: `Resource arn:aws:states:::lambda:invoke.waitForTaskToken`, `Parameters.Payload {input.$: "$", token.$: "$$.Task.Token"}`, `TimeoutSeconds: 1800`, `ResultPath: $.approval`, `Catch [{ErrorEquals: ["States.Timeout"], ResultPath: "$.approval_timeout", Next: "ApprovalTimedOut"}]`; `ApprovalTimedOut` (Pass) sets `$.approval = {"approval":"timeout","tech_id": null}` → `AssignTechnician` → `NotifyByRole` → `ReserveParts` → `Succeed`.
- `serverless.yml` additions: functions `correlateZone`, `explainCause` (timeout 15, env `INVOKER_SECRET=fleetpulse/gcp/sa-invoker`), `waitForApproval`, `reserveParts`, `approvals` (httpApi `GET /approvals/{token}` and `POST /approvals/{token}` on the shared API: `provider.httpApi.id: ${cf:fp-core-${sls:stage}.HttpApiId}` with offline default), `close` (httpApi `POST /incidents/{incident_id}/close`).

- [ ] **Step 1: Write failing tests** in `tests/test_flow_v1.py`: `test_correlate_groups_two_machines_same_cell_last_hour` (two `CELL#` items within the hour, one older than 1 h → `group_id` matches `^GRP-[0-9a-z]{6}-\d{8}T\d{2}$`, `peers` has one machine), `test_correlate_alone_returns_null_group`, `test_correlate_collects_fuel_lot_context` (`CTX#NORTE` refill with `fuel_lot L-2311` → in `context.fuel_lots`), `test_explain_uses_cloud_run_json` (fake http returning a valid body → `source gemini`, `suggested_parts` filtered to BOM part ids), `test_explain_falls_back_on_timeout` (fake raising `TimeoutError` → `source template`), `test_explain_falls_back_on_bad_json`, `test_reserve_parts_emits_requested_from_bom` (outbox event validates against `part.schema.json`, `aggregate_id == "RSV-"+incident_id`), `test_assign_uses_approved_tech_and_approval_flag` (`$.approval = {approval: approved, tech_id: TEC-02}` → assigned TEC-02 with `approval approved`), `test_asl_v1_has_wait_for_task_token_with_timeout_and_catch` (extend `test_asl.py`). In `tests/test_api.py`: `test_approval_sends_task_success` (fake sfn records `taskToken` unquoted and output), `test_approval_is_idempotent` (fake sfn raising `TaskTimedOut` → 200, no raise), `test_close_requires_console_key` (401), `test_close_updates_registry_frees_tech_and_emits_closed` (downtime computed from `opened_at`; `incident.closed` validates), `test_close_twice_is_409`.
- [ ] **Step 2: Run to verify fail.**
- [ ] **Step 3: Implement** modules, update ASL and `serverless.yml`.
- [ ] **Step 4: Run all incidents tests, ruff, package.**
- [ ] **Step 5: Commit** → `feat(aws): incident-flow with zone correlation, Gemini explanation, email approval, part reservation and close API (T14)`.

---

### Task 7: Compose, smoke `ingest` stage, notify subscriptions and deploy docs

**Files:**
- Create: `aws/serverless-compose.yml`, `scripts/subscribe-notify.sh`, `aws/README.md`
- Modify: `scripts/smoke.sh` (`stage_ingest`, `stage_e2e` text: 2 clouds), `scripts/push-secrets.sh` (grep pattern `fp/` → `fleetpulse/`), `README.md` (Desplegar section), `.github/workflows/deploy-aws.yml` (run `bash aws/shared/build.sh` before deploy; `npx serverless@3 deploy` from `aws/` uses the compose file), `.github/workflows/ci.yml` (`serverless-package` job: build layer then `npx serverless@3 package` per service dir, since compose `package` is not supported; python job already discovers `tests/` dirs).

**Interfaces:**
- `aws/serverless-compose.yml`: services `shared: {path: shared}`, `core: {path: ingest, dependsOn: [shared]}`, `outbox-publisher: {path: integrations/outbox-publisher, dependsOn: [core]}`, `scoring: {path: scoring, dependsOn: [core, incidents]}`, `incidents: {path: incidents, dependsOn: [core]}`, `simulator: {path: ../simulator, dependsOn: [core], params: {enabled: false}}`. `oci-writer` is **not** listed (plan B); add a one-line comment saying why.
- `scripts/subscribe-notify.sh <role> <email>`: `aws sns subscribe --topic-arn arn:aws:sns:us-east-1:<acct>:fp-notify --protocol email --notification-endpoint <email> --attributes '{"FilterPolicy":"{\"role\":[\"<role>\"]}"}'`; role ∈ technician, supervisor, management, operator; prints that the email must be confirmed.
- `scripts/smoke.sh stage_ingest`: reads `/fleetpulse/api/url` from SSM and the key from Secrets Manager via `aws` CLI, POSTs `contracts/examples/telemetry-batch/with-fault.json`, expects HTTP 202 or 200 duplicate, then `aws dynamodb get-item fp-state MACHINE#EX-017 META` has `last_ts`, and (after `sleep 20`, best effort) `aws s3 ls s3://fp-raw-<acct>/telemetry/` lists at least one object or prints `SKIP archiver window`.

- [ ] **Step 1: Write the compose file, scripts and README changes.** `aws/README.md` ≤ 40 lines: deploy order, `scripts/lab-creds.sh`, `bash aws/shared/build.sh && cd aws && npx serverless@3 deploy`, `scripts/subscribe-notify.sh`, how to run tests per service, how to enable the simulator (`--param="enabled=true"`).
- [ ] **Step 2: Verify offline**: `cd aws && npx -y serverless@3 print --stage ci` is not available for compose; instead run `for d in shared ingest integrations/outbox-publisher scoring incidents; do (cd $d && npx -y serverless@3 package --stage ci); done` → all succeed; `bash -n scripts/*.sh`; `.venv/bin/pytest -q aws/shared/tests aws/ingest/tests aws/integrations/outbox-publisher/tests aws/scoring/tests aws/incidents/tests` → all PASS.
- [ ] **Step 3: Commit** → `chore(aws): serverless-compose, smoke ingest stage, notify subscriptions, deploy docs`; pull --rebase; push. Confirm `ci` workflow is green on GitHub (`gh run list --repo ArquiFusion-motosierra/fleetpulse --limit 3`).

---

## Verification (end to end, once Learner Lab credentials exist)

1. `scripts/lab-creds.sh --gh` (Benjamin) → `deploy-aws` workflow or local `bash aws/shared/build.sh && cd aws && npx serverless@3 deploy`.
2. `scripts/subscribe-notify.sh technician <email>` ×4 roles, confirm emails.
3. `scripts/smoke.sh ingest` → VERDE.
4. `cd aws && npx serverless@3 deploy --param="enabled=true"` for the simulator, wait 3 min, `scripts/drift.py on S1`, watch `aws stepfunctions list-executions --state-machine-arn ...incident-flow` for `INC-EX-017-...` within ~15 simulated minutes; four emails arrive; `aws dynamodb get-item --table-name fp-registry --key '{"PK":{"S":"INCIDENT#INC-EX-017-..."},"SK":{"S":"META"}}'` shows `status open`, `tech_id`.
5. Click the approve link in the supervisor email → execution resumes; `POST /incidents/{id}/close` with the console key → `status closed`, `incident.closed` on SNS (visible in `gcp-events-q` once Fabricio's consumer exists).
6. `scripts/drift.py off all` and `--param="enabled=false"` to stop spend.
