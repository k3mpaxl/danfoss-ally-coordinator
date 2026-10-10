# Danfoss Ally Coordinator

[![Lint](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml)
[![Validate](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml)

A Home Assistant HACS custom integration for **Danfoss eTRV0103 valves paired directly with ZHA**. It adds the *room-level coordination* that a Danfoss gateway would otherwise perform for two specific functions: supplying room-sensor temperature to each valve and distributing a room's external window-open signal. No proprietary Danfoss gateway is required for these functions. Tested against Home Assistant **2026.10.0**.

## Why this integration exists

Think of each valve as a radiator that can talk, but only about *itself*. ZHA gives Home Assistant a **climate** entity to set its target temperature, a **number** entity for a temperature measured elsewhere in the room, and a **switch** to tell it that a window is open. Depending on the installed ZHA quirk, you may also see a switch for external-temperature priority and extra valve status or settings. The separate quirk makes these valve controls available; HACS does **not** install the quirk.

The valve does not know which wall thermometer or window contact belongs to its room. This integration is the messenger: it finds them by the Home Assistant area, averages *recent* room temperatures (not valve/device temperatures), and passes that value and the combined window signal to every eligible valve in the room. Sensors and contacts may come from other HA integrations. It also shows the calculated room temperature, number of valid sensors, and window state in HA. This supplies **some** gateway-style coordination while the valves remain paired directly to ZHA—not every Danfoss gateway feature.

### Compared with Danfoss's Feature Catalogue

Checked against Danfoss, *Feature Catalogue Ally electronic Radiator Thermostat (eTRV)*, document `AU417130778872en-000201` (June 2022). “Gateway-like” below describes **only the implemented subset**, not gateway equivalence. The catalogue describes Zigbee interfaces and recommended gateway behavior; it does not certify this integration or every `eTRV0103` firmware variant.

| Catalogue feature | What this installation provides |
| :--- | :--- |
| §2.3 external room sensor (`0x0201/0x4015`) | **Integration:** arithmetic mean of fresh, eligible sensors in the *same selected area*, written to each valve's ZHA external-temperature number. For an exposed radiator, the valve itself applies its adaptive offset; the integration does not calculate that offset. Writes are normally no closer than 30 minutes apart for a change of at least 0.1 °C, with a refresh before the catalogue's three-hour timeout. |
| §3.3 external window detection (`0x0201/0x4003`) | **Integration:** any `on` room window/opening/door contact requests external window-open mode on all eligible valves in that area; when none is `on`, the external signal is cleared. This is contact aggregation, **not** coordination based on another valve's own open-window detection (`0x4000`). The valve's window feature (`0x4051`) must be enabled for the signal to have the intended effect. |
| §2.4 covered-radiator external room sensor | **Not supported as a dedicated operating mode.** The catalogue requires a faster cadence (at least every 30 minutes, no more often than every five minutes for a 0.1 K change; timeout after 35 minutes). The current writer always uses the *exposed-radiator* cadence; do not rely on it to keep a covered-radiator lease alive. The separately opted-in external-temperature priority switch does not implement the covered-radiator cadence. |
| Device attributes and diagnostics | **ZHA / separately installed overlay quirk**, not this HACS integration: certain Danfoss controls and readable statuses may be exposed as entities. Availability depends on the installed quirk and device. This integration adds its own room mean, source-count, window-state, and error diagnostics. |
| §2.2 load balancing; §3.2 adaptive preheat | **Not implemented.** No room radiator-load mean (`0x4040`) is distributed and no preheat command (`0x42`) is coordinated. Averaging *temperature sensors* is not load balancing. |
| §1.2 time/DST; §3.1 weekly schedules | **Not implemented by this integration.** It does not synchronize all six time/DST attributes or create, back up, and restore valve weekly schedules after power loss/OTA. A programming-mode entity exposed by a quirk is not a complete schedule manager. |
| §2.1 setpoints; §2.7 boiler mode; §3.9 OTA | **Not managed by this integration.** It does not synchronize room setpoints, select heating-system mode, or perform firmware updates/rejoin recovery. Other ZHA/HA capabilities, if available, are separate. |

The PDF also recommends avoiding unnecessary radio traffic (§1.1). The integration reacts to HA source-state changes and evaluates periodically, but suppresses insignificant/redundant writes; its HA entity-state readback is **not** proof of a Zigbee-cluster acknowledgement. This is deliberately a narrow, opt-in coordinator rather than a replacement for all gateway functions.

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
4. First leave **Enable Zigbee synchronization** off and check the HA area assignments of the valves, room sensors, and contacts (the setup screen shows candidate counts, not individual source names). A wrong area assignment could otherwise send the *wrong room's* temperature or window state to a valve. Then turn synchronization on to start sending values; this is a one-time safety check, not a requirement to keep the integration off.

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
