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

## Device selection

The integration currently selects one GrowHub device per config entry.

Selection is deterministic:

- devices are sorted by `(device_id, client_id, topic_prefix)`
- the first device is chosen

## MQTT path

### Used for

- light state and control
- circulation fan state and control
- duct fan state and control
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

## Climate telemetry path

### Used for

- inside temperature, humidity, and VPD
- outside temperature, humidity, and VPD
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

## Failure handling

### MQTT reconnect

The coordinator supervises the websocket session and reconnects when:

- the session drops
- AWS credentials approach expiry
- a full reauthentication is needed

### Credential refresh

AWS credentials are refreshed before expiry using the configured skew window. If refresh fails due to auth expiry, the coordinator performs a full login, identity exchange, and reconnect.
