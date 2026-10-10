# Danfoss Ally Coordinator

[![Lint](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/lint.yml)
[![Validate](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml/badge.svg)](https://github.com/k3mpaxl/danfoss-ally-coordinator/actions/workflows/validate.yml)

A Home Assistant HACS custom integration for **Danfoss eTRV0103 valves paired directly with ZHA**. It adds the *room-level coordination* that a Danfoss gateway would otherwise perform: supplying room-sensor temperature to each valve, distributing a room's window-open signal, keeping the target temperature of several valves in one room equal, and sharing their radiator load. No proprietary Danfoss gateway is required. Tested against Home Assistant **2026.10.0**.

## Why this integration exists

Think of each valve as a radiator that can talk, but only about *itself*. ZHA gives Home Assistant a **climate** entity to set its target temperature, a **number** entity for a temperature measured elsewhere in the room, and a **switch** to tell it that a window is open. ZHA also shows a switch labelled *Prioritize external temperature sensor*: this is Danfoss's **covered-radiator mode** (`0x4016`), in which the valve regulates on the external room temperature instead of its own sensor.

The valve does not know which wall thermometer or window contact belongs to its room. This integration is the messenger: it finds them by Home Assistant area, averages *recent* room temperatures (not valve/device temperatures), and passes that value and the combined window signal to every valve in the room, at the pace each valve's mode requires. Sensors and contacts may come from any HA integration. It shows the calculated room temperature, valid sensor count, window state and per-valve diagnostics in HA.

### Compared with Danfoss's Feature Catalogue

Checked against Danfoss, [*Feature Catalogue Ally electronic Radiator Thermostat (eTRV)*](https://assets.danfoss.com/documents/latest/602270/AU417130778872en-000201.pdf), document `AU417130778872en-000201` (June 2022). The table describes **only the implemented subset**, not gateway equivalence.

| Catalogue feature | What this installation provides |
| :--- | :--- |
| §2.3 external room sensor, exposed radiator (`0x4015`) | **Integration:** mean of fresh sensors in the *same area*, written to each valve's external-temperature number. The valve derives its own adaptive offset. Writes at most every 30 min for a change ≥ 0.1 °C and refreshes after 2 h 45 min, inside the catalogue's 3-hour timeout. |
| §2.4 external room sensor, covered radiator (`0x4015` + `0x4016`) | **Integration:** when a valve's covered-radiator switch is on, the same mean is written at most every 5 min for a change ≥ 0.1 °C and refreshed after 25 min, inside the catalogue's 35-minute timeout. The integration only **reads** the mode; you choose it per valve. If room sensors fail, nothing stale is written and the valve returns to its own sensor when the value times out. |
| §3.3 window open (`0x4003`) | **Integration:** any available open contact in the area opens the room; optionally, a valve's own `Open` detection is shared with the other valves and released once no valve reports a local opening (catalogue deactivation rule). In covered-radiator mode the valve's own detection is disabled by Danfoss. |
| §2.1 setpoints | **Integration, per room (on by default):** when one valve's target changes — at the dial, from HA, an automation or a valve schedule — the other valves in the area get the same target (ZHA sends it as a user-interaction setpoint command). |
| Local temperature offset (`0x0010`) | **Optional, one-shot action:** set one valve's offset from the measured room gap; see *Operation*. |
| §2.2 load balancing (`0x404A`, `0x4040`, `0x4032`) | **Integration, per room (off by default), rooms with ≥ 2 valves:** averages the valves' own load estimates (discarding values below −500 and older than 90 min) and sends the mean to every valve every 15 min. Turning it off clears the mean (−8000) and switches off each valve's load-balancing mode. If a room has fewer than two supported valves, any saved room opt-in and active load-balancing mode on its remaining valve are automatically turned off. The catalogue recommends it for rooms with several covered radiators. |
| §3.2 adaptive preheat | **Not implemented** (needs a schedule in the valves). |
| §1.2 time/DST; §3.1 weekly schedules | **Partly in ZHA's built-in quirk:** on cluster binding it writes time, time status and time zone, not the catalogue's DST attributes or a periodic/after-reset resync. Schedules are not managed. |
| §2.7 boiler mode; §3.9 OTA | **Not managed** by this integration. |

The integration reacts to source-state changes and evaluates locally every 5 minutes, but suppresses redundant writes (§1.1). Its HA entity-state readback is **not** proof of a Zigbee-cluster acknowledgement.

## Requirements

- Home Assistant with ZHA, tested on 2026.10.0 (Python 3.14).
- Danfoss `eTRV0103` valves paired to ZHA with **ZHA's built-in Danfoss quirk** (`zhaquirks.danfoss.thermostat`). No custom quirk is needed; everything the integration uses comes from the entities ZHA creates itself.
- Correct HA area assignments for each valve, temperature sensor and opening contact.

## Installation

### HACS custom repository

1. In HACS, add `https://github.com/k3mpaxl/danfoss-ally-coordinator` as a custom repository of category **Integration**.
2. Install **Danfoss Ally Coordinator**, then restart Home Assistant.
3. Open **Settings → Devices & services → Add integration**, search for **Danfoss Ally Coordinator**, select the areas, and turn on **Enable Zigbee synchronization** when the discovered sources look right.

### Manual

Copy `custom_components/danfoss_ally_coordinator` into `/config/custom_components/` and restart Home Assistant.

### Coming from the former overlay quirk

Earlier versions shipped an optional ZHA overlay (`danfoss_ally_optimized.py`). It has been removed: it added only display entities and a slow-setpoint input, and it replaced ZHA's own clusters. If you installed it, delete `/config/zha_custom_quirks/danfoss_ally_optimized.py` (keep `custom_quirks_path` only if other custom quirks remain there) and restart. ZHA then applies its built-in Danfoss quirk again; entity IDs of the regular entities stay the same. Remove the overlay's leftover entities, now shown as *not provided*: *Scheduled heating setpoint*, *Programming operation mode*, *Status externes Raumthermometer*, *Ventilübungszeit*, *Status der Raumlastverteilung*, *Absolute maximum heating setpoint* and *Danfoss system status*.

## Operation

- **Discovery:** every ZHA device with manufacturer `Danfoss`, model `eTRV0103` and a climate entity is found by its device, not by individual entities. Room membership comes from the entity/device/area registries, never from names. The area selector shows `TRV` (valves), `T` (temperature candidates) and `W` (contacts) counts.
- **Valves without control entities:** ZHA sometimes marks a valve's Danfoss attributes as unsupported during pairing and then does not create the *external temperature sensor* or *external window sensor* entity. Such a valve is counted as `⚠` in the area selector, listed in the room's `incomplete_valves` attribute and reported under **Settings → Repairs**; it is never written. Fix: enable the entity if it is disabled, otherwise **ZHA → device → ⋮ → Reconfigure** and restart Home Assistant.
- **After a Home Assistant restart:** the Zigbee mesh re-forms and sleepy valves check in over several minutes, so no valve is written during a start-up delay (default **10 min**, option; the mean sensor shows `writes_resume_at`). Values restored at start-up look fresh but prove nothing, so a temperature sensor only counts again after it has reported since the restart (status `waiting_since_restart`). Whenever ZHA reports a valve as unavailable, it is skipped and retried in the next round (`online: false` in the `valves` attribute).
- **Room temperature:** temperature sensors in the area, excluding the valves' own entities and ZHA `device_temperature` diagnostics. Unavailable, non-numeric, unsupported-unit and stale readings are dropped; °F is converted. The remaining readings are combined by **median** (default; a single outlier such as a sensor near the radiator does not pull the value) or **mean** (option). A sensor counts as fresh while its integration keeps reporting it (default **120 min**, configurable 15–720 min).
- **Window contacts:** `window`, `opening` and `door` binary sensors. Any available `on` contact opens the room. Unavailable contacts are ignored; if *no* contact is available, the state is unknown and nothing is written.
- **Which sources a room uses:** each room device (*Danfoss Ally – Room*) has one **source switch** per discovered sensor and contact under *Configuration*. On = used, off = ignored; the change applies immediately without reloading. The icon shows the status (used, ignored, or unusable because unavailable/stale/unsupported unit). The mean sensor's `sources` attribute and the window sensor's `contacts` attribute list every source with its status (and value). The same ignore list is editable under **Configure → Ignored sources**. To add a sensor from elsewhere, assign it to the room's area.
- **Write cadence:** each valve's covered-radiator switch selects the cadence (see table). The mean sensor's `valves` attribute shows each valve's mode, reachability, active error bits from ZHA's *software error* sensor (for example `Invalid_clock_information` after the valve lost its time) and last confirmed write.
- **Room switches** (on the room device, rooms with two or more valves):
  - *Synchronize target temperature* (on by default): after a valve's target has settled for 3 s, the other valves in the room get it, limited to each valve's own min/max setpoint. The integration's own writes are recognised and never start another round. A valve in the middle of an adaptation run is left alone, because a setpoint command would cancel the run (catalogue §1.6). Existing differences are not changed until the next change.
  - *Load balancing* (off by default): see the table above. Its attributes show each valve's load estimate and the last mean sent.
- **Valve window sharing (optional, off by default):** requires window writeback. A valve's own `Open` report closes all valves in the area; `External` (the shared signal) is never treated as a new detection, and an unknown report never means closed.
- **Local sensor calibration (explicit action only):** ZHA's `local_temperature_calibration` number is the valve's **configured offset**; the coordinator's per-valve *temperature gap* sensor is *room mean − valve reading*. The `danfoss_ally_coordinator.calibrate_local_sensor` action writes `old offset + gap`, rounded to 0.1 °C and limited to the number entity's range, and returns the values used. It needs a fresh room mean and a valve reading that has *changed* recently (a refreshed climate state alone does not prove the valve's temperature is current), and it is refused in covered-radiator mode, where the local sensor does not control the valve. With external temperature sync on an exposed radiator, the valve already applies its own adaptive offset; calibration matters mostly without sync.
- **Diagnostics:** **Download diagnostics** on the integration exports configuration, discovered rooms (including incomplete valves), source status, valve modes, errors and the last confirmed writes.

## Safety

### Configuration toggles

The Config Flow and Options Flow expose a **master synchronization** toggle plus temperature writeback, window writeback, valve-window sharing and the room-temperature method. Master synchronization and valve-window sharing default to **off**; nothing is written while master synchronization is off, including setpoint sync and load balancing. With synchronization off, the integration calculates and displays everything but writes nothing. No valve is written before Home Assistant has fully started.

The integration never changes covered-radiator mode, heating-system mode, frost protection or contacts. It changes setpoints only through the per-room target synchronization. It enables load balancing only when you switch it on for a room with multiple valves, and disables it automatically if fewer than two supported valves remain. The old `prioritize_external_sensor` option, which wrongly toggled covered-radiator mode, is ignored. `danfoss_ally_coordinator.refresh` rediscovers sources and evaluates them; it writes only if synchronization is enabled. Calibration is a separate, explicit one-valve action.

Writes are serialized, suppressed when unchanged, and recorded as successful only after the target entity reports the requested value; the successful-write history survives restarts.

### Controlled window-signal test

Only when you explicitly want a live test: choose **one** valve, turn on its external-window switch, wait for the entity state, turn it off again, and confirm the final `off` state and room aggregate. A ZHA switch may acknowledge a service call before its entity state updates.

## Languages

UI, entity names and actions are available in **English** (default) and **German**. Existing entity IDs stay unchanged when the UI language switches.

## Development

```sh
scripts/test
```

`tests/test_init.py` runs config flow, setup, writes, options reload, room switches, start-up delay, repairs, actions, diagnostics and unload against a real Home Assistant core (`pytest-homeassistant-custom-component`); the other tests cover discovery, planning, coordinator decisions and translations. The **Lint** workflow runs Ruff; **Validate** runs the tests, compile checks and HACS validation. No production valve is used in CI. Report bugs with HA/ZHA versions, the integration's diagnostics download and redacted logs; never publish access tokens.

## License

Apache-2.0; see [LICENSE](LICENSE).
