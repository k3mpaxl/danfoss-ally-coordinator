# Danfoss Ally Coordinator

[![Lint](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml)
[![Validate](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml)

A Home Assistant HACS custom integration for **Danfoss eTRV0103 valves paired directly with ZHA**. It adds the *room-level coordination* that a Danfoss gateway would otherwise perform for two specific functions: supplying room-sensor temperature to each valve and distributing a room's external window-open signal. No proprietary Danfoss gateway is required for these functions. Tested against Home Assistant **2026.10.0**.

## Why this integration exists

Think of each valve as a radiator that can talk, but only about *itself*. ZHA gives Home Assistant a **climate** entity to set its target temperature, a **number** entity for a temperature measured elsewhere in the room, and a **switch** to tell it that a window is open. Depending on the installed ZHA quirk, you may also see a switch for external-temperature priority and extra valve status or settings. The optional overlay quirk adds more valve entities; HACS does **not** install it.

The valve does not know which wall thermometer or window contact belongs to its room. This integration is the messenger: it finds them by the Home Assistant area, averages *recent* room temperatures (not valve/device temperatures), and passes that value and the combined window signal to every eligible valve in the room. Sensors and contacts may come from other HA integrations. It also shows the calculated room temperature, number of valid sensors, and window state in HA. This supplies **some** gateway-style coordination while the valves remain paired directly to ZHA—not every Danfoss gateway feature.

### Compared with Danfoss's Feature Catalogue

Checked against Danfoss, [*Feature Catalogue Ally electronic Radiator Thermostat (eTRV)*](https://assets.danfoss.com/documents/latest/602270/AU417130778872en-000201.pdf), document `AU417130778872en-000201` (June 2022). “Gateway-like” below describes **only the implemented subset**, not gateway equivalence. The catalogue describes Zigbee interfaces and recommended gateway behavior; it does not certify this integration or every `eTRV0103` firmware variant.

| Catalogue feature | What this installation provides |
| :--- | :--- |
| §2.3 external room sensor (`0x0201/0x4015`) | **Integration:** arithmetic mean of fresh, eligible sensors in the *same selected area*, written to each valve's ZHA external-temperature number. For an exposed radiator, the valve itself applies its adaptive offset; the integration does not calculate that offset. Writes are normally no closer than 30 minutes apart for a change of at least 0.1 °C, with a refresh before the catalogue's three-hour timeout. |
| §3.3 external window detection (`0x0201/0x4003`) | **Integration:** any `on` room window/opening/door contact requests external window-open mode on all eligible valves in that area; when none is `on`, the external signal is cleared. This is contact aggregation, **not** coordination based on another valve's own open-window detection (`0x4000`). The valve's window feature (`0x4051`) must be enabled for the signal to have the intended effect. |
| §2.4 covered-radiator external room sensor | **Not supported as a dedicated operating mode.** The catalogue requires a faster cadence (at least every 30 minutes, no more often than every five minutes for a 0.1 K change; timeout after 35 minutes). The current writer always uses the *exposed-radiator* cadence; do not rely on it to keep a covered-radiator lease alive. The separately opted-in external-temperature priority switch does not implement the covered-radiator cadence. |
| Device attributes and diagnostics | **ZHA / separately installed overlay quirk**, not this HACS integration: certain Danfoss controls and readable statuses may be exposed as entities. Availability depends on the installed quirk and device. This integration adds its own room mean, source-count, window-state, and error diagnostics. |
| §2.2 load balancing; §3.2 adaptive preheat | **Not implemented.** No room radiator-load mean (`0x4040`) is distributed and no preheat command (`0x42`) is coordinated. Averaging *temperature sensors* is not load balancing. |
| §1.2 time/DST; §3.1 weekly schedules | **Partial time support in ZHA's built-in Danfoss quirk:** on cluster binding it writes time, time status, and time zone. The coordinator and optional overlay do **not** provide periodic/after-reset resynchronization or the catalogue's three additional DST attributes. The integration does not create, back up, and restore weekly schedules after power loss/OTA. A programming-mode entity exposed by the overlay is not a complete schedule manager. |
| §2.1 setpoints; §2.7 boiler mode; §3.9 OTA | **Not managed by this integration.** It does not synchronize room setpoints, select heating-system mode, or perform firmware updates/rejoin recovery. Other ZHA/HA capabilities, if available, are separate. |

The PDF also recommends avoiding unnecessary radio traffic (§1.1). The integration reacts to HA source-state changes and evaluates periodically, but suppresses insignificant/redundant writes; its HA entity-state readback is **not** proof of a Zigbee-cluster acknowledgement. This is deliberately a narrow, opt-in coordinator rather than a replacement for all gateway functions.

## Requirements

- Home Assistant with ZHA, tested on 2026.10.0 (Python 3.14); other versions are not independently verified.
- Danfoss `eTRV0103` valves paired to ZHA, with the external temperature number and external window switch entities present and enabled.
- The **optional** [Danfoss Ally overlay quirk](zha_custom_quirks/danfoss_ally_optimized.py) adds a scheduled-setpoint number, programming-mode select, and readable diagnostic entities. The integration can work with the **built-in ZHA Danfoss quirk alone** if ZHA exposes enabled `external_measured_room_sensor` (number) and `external_open_window_detected` (switch) entities for each valve. This path is supported by the current ZHA entity definitions, but the live installation was tested with the overlay; verify the entities on your system. HACS does not install the optional quirk.
- Correct HA area assignments for each valve, temperature source, and opening contact.

## Installation

### HACS custom repository

1. In HACS, add `https://github.com/k3mpaxl/danfoss-ally-coordinator` as a custom repository of category **Integration**.
2. Install **Danfoss Ally Coordinator**, then restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, search for **Danfoss Ally Coordinator**, and select the intended areas.

### Optional ZHA overlay quirk

This repository also contains [`zha_custom_quirks/danfoss_ally_optimized.py`](zha_custom_quirks/danfoss_ally_optimized.py), the same overlay used in the tested setup. It is **not required** if the two ZHA control entities above are already available; it is not installed automatically by HACS. To use its extra controls and diagnostics, copy that file separately to `/config/zha_custom_quirks/danfoss_ally_optimized.py` on the Home Assistant host. Add `custom_quirks_path: /config/zha_custom_quirks` under your existing `zha:` configuration (do not add a duplicate `zha:` block), then restart Home Assistant and confirm ZHA applied the overlay to `Danfoss` / `eTRV0103`. This quirk is device-specific and may need retesting after ZHA or Home Assistant upgrades. Do not place it inside `custom_components`.

**Time synchronization:** You are right that some of it is already there: ZHA's built-in Danfoss quirk writes three time-related values when the relevant clusters bind; the overlay inherits this behavior. This is **not** the complete six-attribute Danfoss gateway procedure (time, status, zone, DST start/end/shift), nor a verified recurring or post-battery-change recovery service. A current `ok` clock diagnostic does not prove the clock or DST is accurate. The coordinator does not currently change the valve clock.

### Manual

Copy `custom_components/danfoss_ally_coordinator` into `/config/custom_components/` and restart Home Assistant. Install the optional overlay separately only if you want its extra entities; if the required external-temperature number or external-window switch is missing or disabled, the integration cannot control that valve until compatible ZHA entities are available.

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
# Optional quirk tests require pytest, zha==2.3.1, zha-quirks==2.1.1, zigpy==2.3.0:
python -m pytest quirk_tests -q
```

The GitHub Actions **Lint** workflow runs Ruff on the integration and optional quirk; **Validate** runs separate integration and quirk tests, Python compile checks, and HACS validation. `tests/test_localization.py` checks EN/DE translation-key parity and English fallback text. No production valve is used for automated CI tests. Report bugs with HA/ZHA versions, room membership, relevant state/registry attributes, and redacted logs; never publish HA access tokens or private network credentials.

## License

Apache-2.0; see [LICENSE](LICENSE).
