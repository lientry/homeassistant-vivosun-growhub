<div align="center">

# Home Assistant Vivosun GrowHub

Unofficial Home Assistant integration for Vivosun GrowHub lighting, fans, humidifiers, heaters, cameras, and climate telemetry.

![Status](https://img.shields.io/badge/Status-Working-green)
![Home Assistant](https://img.shields.io/badge/Home%20Assistant-Custom%20Integration-blue)
![Runtime](https://img.shields.io/badge/Runtime-Hybrid%20MQTT%20%2B%20REST-4c8bf5)
![Python](https://img.shields.io/badge/Python-3.11%2B-blue)

<a href="#why-this-integration">Why This Integration?</a>
◆ <a href="#quick-start">Quick Start</a>
◆ <a href="#installation">Installation</a>
◆ <a href="#what-it-exposes">What It Exposes</a>
◆ <a href="#runtime-model">Runtime Model</a>
◆ <a href="#troubleshooting">Troubleshooting</a>

</div>

## Why This Integration?

This integration connects a Vivosun GrowHub account to Home Assistant and exposes supported devices as native `light`, `fan`, `humidifier`, `climate`, `camera`, `sensor`, and `binary_sensor` entities.

What is working:

- UI config flow with credential validation
- Multi-device support within one config entry
- Grow light control with correct minimum brightness handling
- Circulation fan control with 10-step mapping and `natural_wind` preset
- Duct fan control with 10-step mapping and auto-threshold service
- AeroStream humidifier support
- AeroFlux heater support
- GrowCam camera support with optional LAN IP setup
- Climate telemetry polling for inside/outside temperature, humidity, and VPD
- Grow plan sensors: active stage name, light schedule, and read-only recipe schedules for fan, humidifier, dehumidifier, drip irrigation, heater, and air conditioner
- Redacted diagnostics export

What this integration is not:

- It is not an official Vivosun integration
- It does not offer local/offline control
- It still depends on the Vivosun cloud, AWS IoT, and valid device credentials

## Compatibility

Verified working:

- GrowHub `E42A`
- GrowHub `E42`
- GrowHub `E25`

Supported Home Assistant version:

- `2026.3.0` or newer

Notes:

- The integration has been tested against current Home Assistant releases, not older 2024-era builds
- Older Home Assistant versions may partially work, but they are not a supported target for this repository

Likely compatible but not yet confirmed:

- GrowHub `E42A+`

## Quick Start

### Manual install

1. Copy `custom_components/vivosun_growhub` to your Home Assistant `config/custom_components` folder.
2. Restart Home Assistant.
3. Add the integration via `Settings -> Devices & Services`.

### HACS install

1. Add this repository as a custom repository in HACS with type `Integration`.
2. Install `Vivosun GrowHub`.
3. Restart Home Assistant.
4. Add the integration from the UI.

## Installation

### Requirements

- Home Assistant with support for custom integrations
- A Vivosun account with at least one GrowHub device
- Outbound internet access from Home Assistant to the Vivosun API and AWS IoT endpoints

### Configuration

The config flow asks for:

- `email`
- `password`

The options flow currently exposes:

- `temp_unit`: `celsius` or `fahrenheit`
- `support_capture_enabled`: enable local support capture so diagnostics exports include a rolling redacted MQTT/support buffer
- `camera_ip`: optional GrowCam LAN IP, if one is present on the account

## What It Exposes

### Light

- `light.growhub_<device>_grow_light`
- Brightness maps to the GrowHub light level
- Values between `1` and `24` are clamped to `25` because the device enforces a minimum on-state brightness

### Fans

- `fan.growhub_<device>_circulation_fan`
- `fan.growhub_<device>_duct_fan`

Fan behavior is device-accurate rather than linear:

- Both fans expose a 10-step speed model in Home Assistant
- The underlying device uses non-linear shadow values
- Plain `turn_on` defaults to the lowest safe level, not maximum speed
- The circulation fan also exposes `natural_wind` as a preset mode

### Humidifier

- `humidifier.growhub_<device>_humidifier`
- Exposes AeroStream humidifier state and mode

### Climate

- `climate.growhub_<device>_heater`
- Exposes AeroFlux heater state, mode, and target control

### Camera

- `camera.growhub_<device>_camera`
- Uses the optional `camera_ip` setting and LAN credentials from the account payload to build the RTSP stream URL

### Sensors

Enabled by default:

- Inside Temperature
- Inside Humidity
- Inside VPD
- Outside Temperature
- Outside Humidity
- Outside VPD

Disabled by default:

- Core Temperature
- WiFi Signal

### Binary sensor

- `binary_sensor.growhub_<device>_connected`

### Entity service

`vivosun_growhub.set_duct_fan_auto_threshold`

Fields:

- `field`: threshold key such as `tMin`, `tMax`, `hMin`, `hMax`, `vpdMin`, `vpdMax`
- `value`: integer or `null` to clear the threshold

### Support capture

For issue investigation, you can enable support capture from the integration options. When enabled, the integration keeps a local, redacted rolling support buffer that is included in `Download diagnostics`.

The integration still exposes advanced services for manual control if you need them:

- `vivosun_growhub.start_support_capture`
- `vivosun_growhub.stop_support_capture`

## Runtime Model

This integration is hybrid.

### MQTT shadow path

Used for:

- light control
- fan control
- reported device state
- connection state

The working control/state path is the classic unnamed AWS IoT shadow:

- `$aws/things/{thing}/shadow/get`
- `$aws/things/{thing}/shadow/update`
- corresponding `accepted`, `documents`, and `delta` topics

### REST polling path

Used for:

- climate telemetry
- current sensor snapshots

Climate telemetry is fetched from:

- `POST /iot/data/getPointLog`

The coordinator polls recent samples and uses the newest point-log row as the current climate snapshot.

For implementation details, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Troubleshooting

### Setup succeeds but entities stay unavailable

Check:

- Home Assistant can reach the Vivosun API and AWS IoT websocket endpoint
- the account has at least one GrowHub device
- the device appears online in the Vivosun app
- if you use GrowCam, the optional LAN IP is configured correctly

### Controls work oddly

The fans are not percentage-native devices. Home Assistant percentages are mapped onto the GrowHub's discrete app levels. If you expect strict linear percentages, the device will appear inconsistent.

If you enable a GrowHub recipe or plan in the Vivosun app, the device may ignore manual control requests from Home Assistant while that plan is active. The entities can still appear available, but the device may not respond to manual light or fan changes until plan mode is disabled in the Vivosun app.

### Climate sensors stay `unknown`

Climate telemetry comes from REST polling, not from the MQTT shadow. After startup or reload, give the integration one poll cycle to populate the sensors.

### Diagnostics

Use `Download diagnostics` on the integration device page. Sensitive fields are redacted before export.

For deeper troubleshooting, enable `support_capture_enabled` in the integration options before reproducing the issue, then export diagnostics. If you prefer manual control, you can still use the `vivosun_growhub.start_support_capture` and `vivosun_growhub.stop_support_capture` services around the reproduction window.

The diagnostics export includes the local support-capture buffer so users can choose whether to share the resulting file for analysis.

## License

MIT.

## Trademark note

`VIVOSUN` and related marks belong to their respective owners. This project is unofficial and uses vendor names only to identify device compatibility.
