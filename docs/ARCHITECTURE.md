# Architecture

## Summary

The integration uses a hybrid runtime model:

- REST bootstrap for login, device discovery, and AWS identity exchange
- MQTT over AWS IoT websocket for control and reported shadow state
- REST polling for climate telemetry

This split exists because control and reported device state are available through the AWS IoT shadow, while current climate readings are retrieved from the point-log API.

## Bootstrap sequence

1. `POST /user/login`
2. `GET /iot/device/getTotalList`
3. `POST /iot/user/awsIdentity`
4. Cognito credential exchange
5. MQTT websocket connect
6. Shadow `get`
7. Initial point-log refresh

The coordinator owns this sequence in [coordinator.py](../custom_components/vivosun_growhub/coordinator.py).

## Device discovery and routing

The integration discovers all supported devices on the account during bootstrap.

Current device classes:

- GrowHub controller
- AeroStream humidifier
- AeroFlux heater
- GrowCam camera

MQTT-capable devices are tracked inside one coordinator and routed by `device_id`, `client_id`, and `topic_prefix`.

Cameras are discovered from the same account payload, but they do not participate in the MQTT/shadow path because they use LAN RTSP streaming instead.

## MQTT path

### Used for

- light state and control
- circulation fan state and control
- duct fan state and control
- humidifier state and control
- heater state and control
- connection state

### Topics

The integration uses the classic unnamed shadow:

- `$aws/things/{thing}/shadow/get`
- `$aws/things/{thing}/shadow/get/accepted`
- `$aws/things/{thing}/shadow/update`
- `$aws/things/{thing}/shadow/update/accepted`
- `$aws/things/{thing}/shadow/update/documents`
- `$aws/things/{thing}/shadow/update/delta`

### Important behavior

`update/delta` is not surfaced as live entity state.

Reason:

- delta can temporarily reflect desired-versus-reported drift
- treating delta as live state caused false UI snaps, including light values jumping back to `50`

Only reported, accepted, and documents payloads are merged into the visible state snapshot.

## Support capture

Support capture is local and opt-in. `getTotalList` provides its account-scoped routing inventory independently of entity discovery. Non-camera entries with valid thing names remain capture candidates when fields such as `sceneId` or `topicPrefix` are missing. Capture never creates controls for rejected entries. An account with only skipped entries can load discovery diagnostics and enable capture through the existing options or service.

Capture uses a dedicated MQTT connection for the exact classic shadow responses and transitions, plus `{topicPrefix}/channel/app` when a prefix exists. It requests one fresh classic shadow per candidate with a unique `clientToken` and a 10-second response timeout. Only the matching thing and token complete that read. Named-shadow and prefix wildcard probes have been removed because prior live captures disconnected during those attempts without collecting useful evidence.

For entity-capable devices, the primary MQTT session already receives classic shadow and channel messages. Capture stores those observations once as `mqtt_publish` events. The dedicated connection still correlates its own shadow reads and records `support_probe_mqtt_publish` for skipped devices, rejected-topic responses and messages received while the primary connection is unavailable. Repeated identical messages from one source remain separate observations.

Capture initializes in a background task so slow broker acknowledgements and snapshot requests do not delay Home Assistant setup or primary MQTT reconnection. Each request still has its own generous timeout, and the capture records `capture_initializing`, `capture_initialized`, or `capture_initialization_failed` in its events. A diagnostics download during initialization may contain incomplete snapshots. Stop and unload cancel any unfinished capture task and close the capture connection.

`support_capture.shadow_read_results` retains the latest per-device read outcome and bounded, redacted payload outside the rolling event buffer. Statuses distinguish accepted, rejected, subscription-rejected, deferred, disconnected, publish-failed and timed-out reads. Setup failures include the AWS identity, Cognito credential or MQTT connection stage. `subscription_results` records each exact filter's broker outcome. Starting a new capture clears old evidence; stopping closes its capture connection. Normal primary reconnects refresh active capture connections and snapshots. Capture-only accounts can restart the capture to retry after a disconnect or credential expiry.

Capture processes at most 25 unique things. It records the number of omitted candidates. MQTT payloads are limited to 64 KiB, eight nesting levels, 80 entries per collection, a 1,500-value traversal budget and 256 characters per string. The larger MQTT value budget preserves both sides of typical `update/documents` transitions without losing late device fields. Individual rolling events are limited to 72 KiB, and the rolling event buffer to 4 MiB total. Old events are evicted and counted in `dropped_events`. Truncation is explicit. Oversized, binary, unstructured and scalar bodies retain a size/status summary without their contents.

Opted-in capture preserves ordinary telemetry strings and device, scene and program IDs. Recognized credentials, account identities, network details and location fields are masked recursively, including inside arrays. Account-bearing client IDs and MQTT topic routes remain pseudonymised. Program references such as `userPlanId` and `userPlanStageId` remain readable. Diagnostics applies this evidence policy separately to `support_capture`; ordinary debug logs and the normal device inventory retain their stricter identifier redaction.

Device IDs identify resources; authenticated identities and authorization policies govern access. Keeping an ID does not establish that Vivosun's authorization has no vulnerabilities. Home Assistant's [diagnostics guidance](https://developers.home-assistant.io/docs/core/integration/diagnostics/) requires protecting credentials, location and personal information, and AWS documents [identity-based authorization](https://docs.aws.amazon.com/iot/latest/developerguide/iot-authorization.html). Users should review diagnostics before sharing because unknown vendor fields and free-form text can contain personal data. Removing a public attachment after resolution reduces continued availability but cannot remove existing copies.

### REST telemetry evidence

While capture is active, each successful `getPointLog` poll records the latest row in `rest_point_log` events and `support_capture.rest_telemetry_results`. The latter retains the latest result for up to 25 devices outside the rolling event buffer. `support_capture.rest_samples` also retains up to 20 distinct, bounded rows per device from the existing API responses, at most 16 KiB per row, to preserve intermediate state transitions when they appear in the returned window. An empty response replaces the latest result with `status: empty`; earlier samples remain in the bounded capture history. Starting a new capture clears all buffers. Normal sensor parsing and polling remain unchanged; this does not add API requests.

`telemetry` contains flat or nested numeric, string, boolean and null values in the API's original units, including unknown sensor fields and USB accessory data. Numeric strings remain strings. Device, scene and program IDs remain readable. The REST collector masks recognized credential, account, network and location fields regardless of their value type. These checks cover camelCase, snake_case and dotted names such as `account_id`, `probe.userId`, `gpsLat` and `authHeader`. Arbitrary keys that do not match the bounded field-name format are omitted. Non-finite numbers and integers exceeding 63 magnitude bits are masked. REST retains the original 500-value traversal budget, eight nesting levels, 80 entries per collection, 256 characters per string and 64 KiB serialized-size cap. `telemetry_truncated` reports omissions and `telemetry_redacted_values` counts fully masked values.

`deviceId` and `sceneId` route the REST request. Capture does not include its body, authentication headers or response envelope. The result's device correlation identifier and model token remain readable. Point-log rows normally contain readings and a sample timestamp, but the API has no universal marker separating private data from telemetry. Key-based redaction of recognized private fields does not guarantee anonymisation of unknown fields.

REST evidence requires a device already eligible for point-log polling. A skipped device's MQTT routing alone does not supply its missing REST scene information. The integration polls every 90 seconds and the cloud reports at minute resolution. A quick control sweep can capture MQTT transitions but does not guarantee a REST sample for each intermediate setting. The requester checklist uses one session and one export; separate timed measurements are a device-specific follow-up only when REST transitions are needed.

App-driven changes can reveal reported and desired fields, including accessories in their parent's shadow. Capture does not enumerate arbitrary MQTT topics, retrieve historical broker traffic or intercept proprietary app REST requests. A subscription acknowledgement alone does not prove that messages arrived. Devices without a discovered thing name remain metadata-only. Custom program catalogs and command mappings still require evidence from the relevant device owner.

AWS documents the classic shadow read/response protocol in [Device Shadow MQTT topics](https://docs.aws.amazon.com/iot/latest/developerguide/device-shadow-mqtt.html). HTTPS `GetThingShadow`, named-shadow listing and MQTT topic operations have separate permissions; failure of one does not establish the others' permissions.

## Climate telemetry path

### Used for

- inside temperature, humidity, and VPD
- outside temperature, humidity, and VPD
- humidifier probe temperature, humidity, and VPD
- heater probe temperature, humidity, and VPD
- humidifier water level
- optional core temperature and RSSI

### Endpoint

- `POST /iot/data/getPointLog`

### Polling model

- a recent window is requested
- the newest row from `iotDataLogList` becomes the current sensor snapshot
- the coordinator refresh interval is 90 seconds

## Device-specific mappings

### Light

- `0` means off
- `1..24` are clamped to `25`
- `25..100` pass through unchanged

### Duct fan

Home Assistant exposes a 10-step speed model. The device shadow uses non-linear `manu.lv` values.

| App level | Shadow value |
| --- | --- |
| 0 | 0 |
| 1 | 30 |
| 2 | 35 |
| 3 | 40 |
| 4 | 50 |
| 5 | 60 |
| 6 | 70 |
| 7 | 80 |
| 8 | 85 |
| 9 | 90 |
| 10 | 100 |

### Circulation fan

| App level | Shadow value |
| --- | --- |
| 0 | 0 |
| 1 | 44 |
| 2 | 51 |
| 3 | 60 |
| 4 | 64 |
| 5 | 70 |
| 6 | 75 |
| 7 | 80 |
| 8 | 85 |
| 9 | 90 |
| 10 | 100 |

Special mode:

- `natural_wind` is represented by `lv = 200`

### Humidifier

- represented as a native Home Assistant `humidifier`
- supports manual and auto mode
- exposes target humidity and current water level when available

### Heater

- represented as a native Home Assistant `climate` entity
- supports manual and auto mode
- exposes target temperature through the standard climate interface

### Camera

- represented as a Home Assistant `camera` entity
- uses a per-device camera IP mapping plus LAN credentials exposed by the account payload
- RTSP stream URL is derived at runtime and credentials are URL-encoded before use

## Failure handling

### MQTT reconnect

The coordinator supervises the websocket session and reconnects when:

- the session drops
- AWS credentials approach expiry
- a full reauthentication is needed

### Credential refresh

AWS credentials are refreshed before expiry using the configured skew window. If refresh fails due to auth expiry, the coordinator performs a full login, identity exchange, and reconnect.
