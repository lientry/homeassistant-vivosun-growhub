# Vivosun GrowHub E42A Technical Specification

## Architecture

The GrowHub E42A is a cloud-connected device.

The integration uses two communication layers:

1. REST API: authentication, device discovery, AWS identity exchange, and climate telemetry
2. AWS IoT MQTT: device control and shadow state synchronization

## REST API

### Base URL

```text
https://api-prod.next.vivosun.com
```

### Headers

Required for unauthenticated requests:

```text
Content-Type: application/json
```

Required for authenticated requests:

```text
login-token: <loginToken>
access-token: <accessToken>
```

### Standard response envelope

```json
{
  "code": 0,
  "success": true,
  "message": "success",
  "data": {}
}
```

## Authentication

### POST /user/login

Request:

```json
{
  "email": "<email>",
  "password": "<password>",
  "spAppId": "com.vivosun.android",
  "spClientId": "<uuid-v4>",
  "spSessionId": "<uuid-v4>"
}
```

Response data fields:

```json
{
  "accessToken": "<jwt>",
  "loginToken": "<jwt>",
  "refreshToken": "<jwt>",
  "userId": "153685567990966911"
}
```

### Token lifecycle

| Token | Approximate lifetime |
| --- | --- |
| `loginToken` | ~10 months |
| `accessToken` | ~3 months |
| `refreshToken` | ~10 months |
| AWS STS credentials | ~1 hour |

Reauthentication is performed with a fresh login when needed.

## Device discovery

### GET /iot/device/getTotalList

Relevant fields from a GrowHub device entry:

```json
{
  "clientId": "vivosun-VSCTLE42A-153685488534068007-153685488534068013",
  "deviceId": "153685488534068013",
  "name": "GrowHub E42A",
  "topicPrefix": "vivosun/VS_COMMON/VSCTLE42A/153685488534068007/153685488534068013",
  "onlineStatus": 1,
  "scene": {
    "sceneId": 66078
  }
}
```

Field meanings:

- `clientId`: thing name used for shadow topics
- `deviceId`: device identifier
- `topicPrefix`: channel topic prefix
- `onlineStatus`: `1` for online, `0` for offline
- `scene.sceneId`: required for climate telemetry requests

### POST /iot/device/detail

Request:

```json
{
  "deviceId": "153685488534068013"
}
```

This returns expanded device metadata, including AWS thing-related parameters.

## AWS identity

### POST /iot/user/awsIdentity

Request:

```json
{
  "awsIdentityId": "",
  "attachPolicy": true
}
```

Relevant response data:

```json
{
  "awsHost": "<aws-iot-endpoint>",
  "awsRegion": "us-east-2",
  "awsIdentityId": "<identity-id>",
  "awsOpenIdToken": "<token>",
  "awsPort": 443
}
```

## Climate telemetry

### POST /iot/data/getPointLog

Request:

```json
{
  "sceneId": 66078,
  "timeLevel": "ONE_MINUTE",
  "reportType": 0,
  "orderBy": "asc",
  "startTime": 1772781060,
  "endTime": 1772781733,
  "deviceId": "153685488534068013"
}
```

Parameters:

| Field | Type | Description |
| --- | --- | --- |
| `deviceId` | string | Device ID |
| `sceneId` | int | Scene ID from device discovery |
| `startTime` | int | Unix timestamp, seconds |
| `endTime` | int | Unix timestamp, seconds |
| `reportType` | int | `0` for sensor data |
| `orderBy` | string | `asc` or `dsc` |
| `timeLevel` | string | `ONE_MINUTE` |

Response data contains `iotDataLogList`, with each row carrying point-in-time telemetry.

Example row:

```json
{
  "inTemp": 2004,
  "inHumi": 5508,
  "inVpd": 105,
  "outTemp": 1985,
  "outHumi": 5449,
  "outVpd": 105,
  "coreTemp": 3839,
  "rssi": -35,
  "light.mode": 0,
  "light.lv": 0,
  "cFan.lv": 0,
  "dFan.lv": 0,
  "time": 1772781720
}
```

Value scaling:

| Key | Scaling | Example | Displayed value |
| --- | --- | --- | --- |
| `inTemp`, `outTemp` | divide by 100 | `2004` | `20.04 C` |
| `inHumi`, `outHumi` | divide by 100 | `5508` | `55.08 %` |
| `inVpd`, `outVpd` | divide by 100 | `105` | `1.05 kPa` |
| `coreTemp` | divide by 100 | `3839` | `38.39 C` |
| `rssi` | no scaling | `-35` | `-35 dBm` |

Sentinel value:

- `-6666` means the sensor is not connected

## MQTT topics

### Shadow topics

The working state and control path uses the classic unnamed shadow:

```text
$aws/things/{thing}/shadow/get
$aws/things/{thing}/shadow/get/accepted
$aws/things/{thing}/shadow/update
$aws/things/{thing}/shadow/update/accepted
$aws/things/{thing}/shadow/update/documents
$aws/things/{thing}/shadow/update/delta
```

### Channel topic

```text
{topicPrefix}/channel/app
```

## Shadow schema

Relevant keys:

```json
{
  "light": {
    "mode": 0,
    "manu": {
      "lv": 25,
      "spec": 20
    }
  },
  "cFan": {
    "mode": 0,
    "manu": {
      "lv": 70
    },
    "osc": 0,
    "nw": 0
  },
  "dFan": {
    "mode": 0,
    "manu": {
      "lv": 60
    },
    "auto": {
      "tMin": -6666,
      "tMax": 2800,
      "hMin": -6666,
      "hMax": 7000,
      "vpdMin": -6666,
      "vpdMax": 180
    }
  },
  "connection": {
    "connected": true
  }
}
```

## Device-specific control mappings

### Light

- `0` means off
- `1..24` are clamped to `25`
- `25..100` are passed through

### Duct fan mapping

| App level | Shadow `manu.lv` |
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

### Circulation fan mapping

| App level | Shadow `manu.lv` |
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
| Natural Wind | 200 |
