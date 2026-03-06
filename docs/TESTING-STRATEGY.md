# Testing Strategy

## Scope

This integration is tested as a hybrid cloud integration:

- REST bootstrap and telemetry polling
- MQTT websocket transport and shadow updates
- Home Assistant entities, config flow, and diagnostics

## Test layers

### Unit tests

Focused tests cover:

- REST API parsing and error handling
- Cognito and SigV4 credential flow
- MQTT packet encoding and decoding
- shadow parsing and desired payload construction
- fan/light mapping helpers

### Integration tests

Coordinator and entity tests cover:

- bootstrap sequence
- reconnect behavior
- shadow state merging
- point-log sensor refresh
- config flow and options flow
- diagnostics redaction

### Smoke tests

Smoke coverage verifies:

- full setup path
- control roundtrips for light and fans
- sensor population from the hybrid runtime model
- unload and reload behavior

## Current commands

### Full test suite

```bash
.venv/bin/pytest -q
```

### Lint

```bash
.venv/bin/ruff check .
```

### Type checking

```bash
.venv/bin/python -m mypy --explicit-package-bases custom_components/vivosun_growhub
```

## CI expectations

CI should fail on:

- test regressions
- lint failures
- integration package type errors
- HACS validation failures
- hassfest validation failures

## Manual verification before release

1. Manual install into Home Assistant
2. Config flow setup with a real account
3. Light control
4. Circulation fan control including `natural_wind`
5. Duct fan control including low-speed default behavior
6. Climate sensor population after poll refresh
7. Diagnostics export
8. Cold restart and reconnect verification
