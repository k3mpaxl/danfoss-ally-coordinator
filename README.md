# Danfoss Ally Coordinator

[![Lint](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml)
[![Validate](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml)

A Home Assistant HACS custom integration for **ZHA Danfoss eTRV0103** radiator valves. It discovers eligible valves, room temperature sensors, and opening contacts by Home Assistant area; averages valid room readings; and synchronizes external temperature and window state through the existing ZHA number/switch entities. Tested against Home Assistant **2026.10.0**.

> This repository is **not** the Danfoss ECL 310 Modbus integration. It controls Danfoss Ally Zigbee valves already paired to ZHA.

## Requirements

- Home Assistant with ZHA, tested on 2026.10.0 (Python 3.14); other versions are not independently verified.
- Danfoss `eTRV0103` valves paired to ZHA, with the external temperature number and external window switch entities present and enabled.
- A Danfoss Ally overlay quirk was used in the tested setup to expose additional entities. The quirk is **not included** in this HACS repository; install a compatible quirk separately if your ZHA setup lacks the required controls. HACS does not install custom ZHA quirks.
- Correct HA area assignments for each valve, temperature source, and opening contact.

## Installation

### HACS custom repository

1. In HACS, add `https://github.com/k3mpaxl/danfoss-ally-coordinator` as a custom repository of category **Integration**.
2. Install **Danfoss Ally Coordinator**, then restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, search for **Danfoss Ally Coordinator**, and select the intended areas.
4. Keep **Enable Zigbee synchronization** off initially. Verify discovered sensors and contacts before enabling any writes.

### Manual

Copy `custom_components/danfoss_ally_coordinator` into `/config/custom_components/` and restart Home Assistant. If your ZHA installation does not expose the required valve number/switch controls, deploy a compatible custom quirk separately via ZHA's custom quirks path and restart ZHA/HA before adding this integration.

## Operation

- Actuators must be ZHA devices with manufacturer `Danfoss`, model `eTRV0103`, and required control entities. Other integrations may supply **inputs**, but not actuator targets.
- Room membership comes from the HA entity/device/area registries, not entity-name patterns. The area selector shows the area name followed by `TRV` (valves), `T` (temperature candidates), and `W` (window/opening contacts); these abbreviated counts are language-neutral.
- Temperature candidates must be room sensors, not sensors on target valves or ZHA `device_temperature` diagnostics. Disabled, unavailable, nonnumeric, nonfinite, unsupported-unit, and stale readings are excluded. Values in °F are converted to °C; the remaining readings form an arithmetic mean. The default age limit is **120 minutes**, configurable from **15 to 720 minutes**. Candidate counts in setup can exceed the *currently valid* count displayed by the diagnostic sensor.
- Binary sensors with `window`, `opening`, or `door` device classes contribute to the room status. If **any** contact is `on`, the aggregate is open. A missing/unavailable contact is not treated as open; inspect source availability if safety depends on it.
- When opted in, the mean is written to the existing external-temperature number entity and the aggregate window state to the existing external-window switch. External temperature priority is a **separate** opt-in setting. If no valid room temperature remains, no stale external value is written and priority is released so the valve can use its internal sensor.
- Writes use delta suppression, persistent successful-sync history, and a battery-friendly cadence (normally at least 30 minutes between writes, with lease refresh ahead of the three-hour device timeout). HA entity-state readback is checked after writes; this is not a direct Zigbee-cluster acknowledgement. A service call returning success alone does not prove device acceptance.

## Safety

### Configuration toggles

The Config Flow and Options Flow expose independent toggles for **master synchronization** (`enabled`), **temperature writeback** (`sync_temperature`), **window writeback** (`sync_windows`), and **external temperature priority** (`prioritize_external_sensor`). The master toggle defaults to **off**. The last toggle also defaults to **off**; temperature and window writeback default to on *only when the master toggle is enabled*. Existing entries retain backward-compatible defaults.

The integration does **not** change the climate setpoint, heating-system mode, frost protection, or window contacts themselves. Validate each area's physical room boundaries before enabling writes. A manual refresh service, `danfoss_ally_coordinator.refresh`, rediscovers sources and evaluates them; it may write to valves if master synchronization is enabled.

### Controlled window-signal test

Only when an operator explicitly authorizes a live test: choose **one** valve, turn on its external-window switch, wait for the entity-state readback, turn it off again, and confirm the final `off` state and room aggregate. A ZHA switch may acknowledge a service call before its entity state propagates. Do not leave an artificial open signal asserted.

## Languages

UI config/options, diagnostic entity names, and refresh-service labels are available in **English** (default `strings.json` and `translations/en.json`) and **German** (`translations/de.json`). Existing HA entity IDs and stored device names may remain unchanged when the UI language switches; translations apply to displayed labels, not historical registry identifiers.

## Development

```sh
scripts/test
```

The GitHub Actions **Lint** workflow runs Ruff; **Validate** runs pytest, Python compile checks, and HACS validation. `tests/test_localization.py` checks EN/DE translation-key parity and English fallback text. No production valve should be used for automated CI tests. Report bugs with HA/ZHA versions, room membership, relevant state/registry attributes, and redacted logs; never publish HA access tokens or private network credentials.

## License

Apache-2.0; see [LICENSE](LICENSE).
