# Architecture

## Runtime and module boundaries

The integration uses REST for account discovery and climate telemetry, and AWS IoT MQTT over WebSocket for device commands and shadow reports. GrowCam uses LAN RTSP for video.

| Module | Responsibility |
| --- | --- |
| `api.py`, `aws_auth.py` | Account login, device discovery, REST requests and AWS credential exchange |
| `mqtt_client.py` | MQTT transport and subscriptions |
| `coordinator.py` | Per-device routing, reported state, pending commands, telemetry polling and connection lifecycle |
| `shadow.py`, `ports.py`, `vseed.py` | Model-scoped parsing, normalization and command payloads |
| Entity platforms | Native Home Assistant controls and readings |
| `manual_preferences.py` | Persistent Home Assistant manual selections |
| `support_capture.py`, `redaction.py` | Bounded opt-in evidence collection and privacy filtering |

Bootstrap logs in through `/user/login`, discovers account devices through `/iot/device/getTotalList`, exchanges `/iot/user/awsIdentity` with Cognito, connects MQTT and requests initial shadows and telemetry.

One coordinator routes devices by their device ID, client ID and topic prefix. Device classification determines entity families, with exact model handling for A10, A22, VGrow, VSeed and other model-specific protocols. Controller identity does not establish which external accessories are physically attached.

VSeed S40 discovery and point-log requests accept a device without a garden scene. A10 discovery also accepts a device without a garden scene, and the A10 makes no point-log requests. Cameras use account discovery and optional per-camera LAN IP configuration, but remain outside the MQTT device path. RTSP credentials are URL-encoded when building stream URLs.

### Garden recipe schedules

Garden recipes use independently read garden plan data, not a controller's shadow as the authoritative recipe. The app's `planStageContent` slot times are seconds from stage-relative midnight, with `86400` as the day endpoint. The verified zero-`timeOffset` projection adds the configured stage start's garden-local hour and minute to those slot times. Nonzero `timeOffset` and unresolved DST transitions remain unknown rather than guessed; no app maintenance or synchronization actions are copied.

Garden recipe, current-stage and configured equipment schedules belong only to garden service devices. Schedule families follow configured recipe slots, including gardens without a connected controller; they do not prove accessory attachment or execution. Empty gardens keep recipe/stage sensors without invented schedules. Failed reads stay unavailable until recovery.

Reported device plan changes trigger coalesced garden reads, not inferred garden state. Manual entity refresh and integration reload also refresh garden data. Without a device event, the periodic fallback can take roughly three minutes plus API latency and cloud propagation. All garden recipe and capture-stage reads share a three-request limit, with per-garden ordering preventing stale results from winning.

Successful complete inventory refreshes remove deleted garden service devices and their garden entities. Physical devices are detached or reassociated without losing their identities or measurements. Failed or superseded inventory reads never remove gardens.

On upgrade, exact legacy controller plan sensor registry rows are disabled and kept with their existing IDs, user metadata and historical records. Those rows no longer publish states. Dashboards and automations referencing old controller plan sensors must switch to the garden-owned IDs. Physical sensors and controls retain their IDs and behavior.

## State and command contracts

The integration uses the classic unnamed AWS shadow: `get`, `get/accepted`, `update`, `update/accepted` and `update/documents`. Controller `update/rejected` subscriptions are best effort: missing permission must not disrupt primary control. `update/delta` is not projected as live state because it describes desired/reported drift.

A desired-only acknowledgement confirms cloud receipt, not device execution. Genuine reported state remains separate from local requests. Light, circulation-fan and heater normalization emits only fields present in a fragment, so deep merge preserves omitted values. Explicit zero, null, invalid and sentinel values retain their field-specific clearing rules.

Controller level/mode projection preserves the legacy desired-only startup fallback until a local command or actual report establishes a different provenance. A first local command discards unconfirmed fallback for its affected field. Other legacy components and standalone devices retain their existing desired fallback. S40 seed values, physical port outputs and the spectrum cache use genuine reports only.

### Immediate controller feedback

A single typed lifecycle tracks pending fields by device, channel and field: light level, circulation-fan level, duct-fan level and duct-fan mode. Snapshots overlay the latest local intent on the independent reported cache. Home Assistant automations see this provisional state too.

- Each command has a generation and cloud `clientToken`. Desired acknowledgements correlate receipt but do not confirm output.
- Fresh reports settle fields independently. A level-only report cannot confirm a requested mode.
- Selected-leaf AWS timestamps and versions establish freshness. A higher whole-document version alone does not prove a field changed.
- GET is a cached snapshot. Settling a request through GET requires the selected reported timestamp to follow both the pre-request field and the accepted desired field, plus a later version.
- Direct reported writes or demonstrated previous/current field transitions can establish order within the same AWS timestamp second. Unchanged documents and metadata-free GETs cannot.
- Missing metadata preserves the last known field provenance. Equal-version complementary partials can update different fields.
- The selected timestamp follows normalization: circulation falls back to `manu.lv` for an invalid root level; duct fan preserves a present root null and its metadata.
- Independently fresh reports replace provisional state. A report matching a superseded local request remains ambiguous, so the latest request stays visible until confirmation or failure.
- Matching rejection, send failure, disconnect or a 15-second unconfirmed deadline removes pending state and reveals the last report. Late reports still apply. There are no automatic command retries.
- Requests already matching confirmed state still publish tokenized commands, but create no false pending timer. Their short-lived rejection receipts expire silently.

Mode-only duct commands supersede older pending manual output without inventing a speed. Optimistic state never establishes manual/no-plan permission for guarded operations.

AWS field timestamps have one-second precision and identify update order, not command authorship or physical actuation. Bounded optimism can temporarily mask an external change matching a superseded local request.

Protocol reference: [AWS Device Shadow documents](https://docs.aws.amazon.com/iot/latest/developerguide/device-shadow-document.html).

## Model-specific boundaries

### Controller lights and fans

Light zero means off, and positive brightness is clamped to the device minimum of 25%. Fan percentages use the discrete mappings in `const.py` and `shadow.py`.

Circulation Natural Wind is the sole native preset, at raw level `200`. Numeric speeds, off and unknown state have no active preset. Explicit percentage wins over a simultaneously supplied valid preset. OFF sends zero; bare ON while already in Natural Wind leaves it active. Raw `nw` is retained without assigning Night semantics. The native entity translation maps `natural_wind` to "Natural Wind" without changing its service or stored value.

Duct Auto uses code `2` on E42A and `1` on legacy controllers. Auto is logically on even at zero output, while the actual percentage remains zero. Native power toggle in Auto sends one manual-mode, zero-level OFF command. Plain ON retains its manual/default behavior.

Spectrum and circulation oscillation are separate default-off, per-controller options. Controller model or a reported spectrum value cannot prove lamp compatibility. The Spectrum select offers Seedling, Vegetative and Bloom and writes only `light.manu.spec`, without changing brightness, power, mode or plan. It requires genuine reported `mode=0` and `inPlan=0`; invalid or stale guard fields cannot authorize writes.

Oscillation opt-in exposes the native fan feature and existing Boolean `cFan.osc` command. Disabled controls reject stale service calls before publishing. It does not infer fan generation or translate multilevel oscillation.

### A10, A22 and VGrow ports

A22 has physical `usb1`, `usb2`, `plug1` and `plug2` switches. A10 `VSPLUGA10` has one AC outlet, `plug1`, with the same port schema as the A22 outlets. VGrow `VSCTLY18` has `usb1` and `usb2`. Stable identities follow the physical port, not its accessory selection.

Exact `VSPLUGA22` also exposes per-port native config select (Manual/Cycle/Auto), wallclock Cycle start time and number controls for Cycle on/off seconds and Auto low/high temperature/humidity. The firmware owns execution; HA passes start time as device-local seconds since midnight without timezone conversion. Temperatures use Celsius-native values and HA's temperature unit conversion, rounded to the device's 0.1°C step. These settings are reported-only, and writes require genuine per-leaf provenance and known mode/no plan; paired duration and threshold edits also require fresh counterparts. Missing reported fields preserve previous values, while explicit null/invalid leaves or parent blocks clear write authority only after freshness checks. Per-leaf version/timestamp watermarks survive metadata-free updates and disconnects. A new-session unchanged genuine GET can restore authority at the retained version; after an uncertain command, an unchanged GET requires a newer shadow version and a non-regressing leaf timestamp. These reads authorize use of current reported facts, not physical command completion. Paired edits reject overlap until requested values are genuinely reported with adequate ordering proof; a same-second reported write after a matching desired ACK can settle a pending shadow change, but the desired ACK alone cannot. Already-reported no-op requests retain bounded rejection receipts without blocking timers. Manual exit publishes mode first, then the reported binary output as `manu.lv`. Newer physical power or mode actions supersede stale Manual completion on that port, and send failures or rejected commands revoke affected write authority until a fresh read. Existing A22 power switches require the same reported-state authority as native settings; old-session MQTT callbacks are ignored, while VGrow keeps its original port implementation. HA's limits are conservative local structural bounds, not verified firmware setter ranges.

Current output comes from genuine reported root `lv`: exactly zero or 100. The stored manual level may differ during a plan. Output remains readable across modes and partial reports; unknown/nonbinary output makes the switch unavailable.

Writes require an available binary output and explicit reported manual mode with no plan. They change only `manu.lv` to zero or 100, never mode, plan or accessory type. Numeric `dev` is an accessory-code attribute, not a verified accessory name or a speed contract.

A10 and A22 are classified by model token before the generic GrowHub name check, so neither receives the controller entities (light, fans, inside/outside climate sensors, plan sensors). A10 has no built-in climate sensor, so it has no sensors besides connectivity, does not poll point logs and does not require a `sceneId`. Optional external sensors on the A10 are not handled.

### VSeed S40

Exact model `VSSBS40` uses reported `seed` state for Growth Stage, stored stage settings and Quick Irrigation. Desired-only acknowledgements cannot update those facts; partial reports preserve omitted fields.

Quick Irrigation requests the native 600-second manual cycle or zero to stop. Its switch reads reported `waterTime`, not command acceptance. Manual irrigation bypasses the manufacturer's automatic water-temperature checks. The integration adds no speculative interlocks or retries.

Stage settings are recipes, not actuator readings. Light Duration is `lgtOnDur` in seconds, distinct from the daily start-time field `lgtOnTime`. Current light intensity reads `seed.lgtLv`, separately from current/stored recipe levels.

Successful S40 point-log polls replace root/dome temperature, humidity, VPD and signal readings. Missing, invalid, sentinel or empty results clear prior values; invalid VPD does not fall back to a computed value. Network failure follows the coordinator error path rather than claiming an empty successful sample.

## Remembered manual settings

One default-off option enables preferences for controller lights and fans. Storage remains independent per stable device and channel.

Only explicit Home Assistant brightness, fan speed or Natural Wind selections train memory, including scripts and automations. Matching cloud token, desired selection and generation establish acceptance. Rejected, failed, expired, disconnected or out-of-order requests cannot replace a newer accepted choice. Cloud acceptance is not proof of execution.

OFF, bare ON, replayed preferences, app changes and reports do not train memory. A stored selection is replayed on bare ON while off only when the component genuinely reports manual mode and no plan. Unknown, stale or automatic facts prevent replay, not ordinary power-on. The fallback uses existing command behavior, including cold defaults of 25% for lights and 10% for fans, without a memory-specific validation error.

Config entries retain major version 1, minor version 2. One-time option migration maps valid nonempty legacy channel lists to enabled, and missing/empty/malformed selections to disabled. Independent version-2 Home Assistant storage migrates numeric preferences to validated tagged numeric/preset selections. Keeping the ConfigEntry major version preserves older handlers' ability to load entries after rollback.

Storage loads without startup/reconnect actuation. Disabling the option leaves preferences dormant. Persistence is best effort, so a crash or write failure can lose the latest accepted selection.

## Telemetry and support capture

Climate telemetry polls `/iot/data/getPointLog` on a 90-second coordinator interval, using the newest returned row. MQTT controls do not imply a fresh REST measurement.

Support capture is local and opt-in. Account discovery supplies its routing inventory independently of entity eligibility. Skipped devices with valid thing names can supply evidence without receiving controls.

A dedicated MQTT session subscribes to exact classic shadow responses and discovered channel topics, and requests a token-correlated shadow read for each candidate. Primary-session observations are recorded once; the capture session supplies skipped-device or otherwise unavailable observations. Repeated messages from the same source remain separate events.

Capture initializes asynchronously, exposes partial/failure outcomes and stops its connection/tasks on stop or unload. A new session clears old evidence. Reconnect refreshes active capture connections and snapshots; capture-only accounts can restart capture after failure.

Collection is bounded by device count, payload size, nesting, collection length and traversal budgets. The rolling buffer reports eviction and truncation. Latest shadow-read outcomes, REST results/samples and existing stage-info outcomes remain outside that ring under their own caps.

REST telemetry capture observes existing requests. While capture is enabled, genuinely reported stage IDs trigger at most one `/iot/plan/stageInfo` read per ID, capped at 25 IDs per session. Results preserve stage ID correlation, name, icon and parsed object content, or bounded failure status. Stop or restart rejects late results from earlier sessions. Capture does not edit programs, enumerate saved recipes or intercept proprietary app REST calls.

Capture preserves useful telemetry and device/scene/program identifiers. Recognized credentials, account identities, hardware/network identifiers and location fields are masked recursively. Client/topic pseudonyms preserve routing correlation. Normal inventory/debug output uses stricter identifier filtering. Unknown fields and free-form text can still contain personal information, so users must review exports before sharing.

See the [support capture guide](support-capture-recon-checklist.md), [Home Assistant diagnostics guidance](https://developers.home-assistant.io/docs/core/integration/diagnostics/) and [AWS shadow topics](https://docs.aws.amazon.com/iot/latest/developerguide/device-shadow-mqtt.html).

## Connection lifecycle

The coordinator reconnects dropped MQTT sessions and refreshes AWS credentials before expiry. Authentication expiry triggers full login and identity exchange. Reconnection reads current state without replaying commands.
