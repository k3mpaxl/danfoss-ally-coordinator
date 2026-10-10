# Danfoss Ally Coordinator

Eine HACS-Custom-Integration für Danfoss-`eTRV0103`-Ventile über ZHA.

## Funktionen

- erkennt ausschließlich `manufacturer=Danfoss`, `model=eTRV0103` mit ZHA-Entities;
- gruppiert Ziele über die HA-Device-/Entity-/Area-Registry;
- findet Temperaturquellen integrationsübergreifend im selben Bereich;
- mittelt frische, endliche Werte in Celsius oder Fahrenheit;
- aggregiert Fenster-, Öffnungs- und Türkontakte;
- schreibt den Mittelwert über die vorhandene ZHA-Number-Entity für externe Raumtemperatur;
- synchronisiert den aggregierten Fensterstatus über die vorhandene ZHA-Switch-Entity;
- priorisiert den externen Temperatursensor nur über die explizite Option `prioritize_external_sensor`;
- prüft Temperatur- und Fenstersignal-Writes per Read-back; bei ZHA kann ein HTTP-200 vor der tatsächlichen Zustandsänderung eintreffen;
- verwendet persistente Sync-Historie, Vendor-Cadence und Delta-Suppression;
- startet standardmäßig mit deaktivierten Funkwrites.

Die Optionen im Config-/Options-Flow bieten getrennte Toggles für:

- gesamte Zigbee-Synchronisierung (`enabled`);
- Raumtemperatur-Writeback (`sync_temperature`);
- Fensterstatus-Writeback (`sync_windows`);
- Priorisierung des externen Temperatursensors (`prioritize_external_sensor`).

Fehlende neue Optionen in bestehenden Config Entries verwenden rückwärtskompatibel sichere Defaults; die Funkwrites bleiben insgesamt standardmäßig deaktiviert.

## Sicherheit

Die Integration verändert weder Climate-Sollwerte noch Danfoss-Heizsystemmodus oder Frostschutz. Nur die vorhandenen, typisierten ZHA-Number-/Switch-Entities werden verwendet. Die Bereichsauswahl erfolgt explizit im Config Flow; eine Gruppierung nach Entity-Namen findet nicht statt.

## Live-Verifikation

Bei einem produktiven Fenstersignal-Test nur ein Ventil auswählen und den Zustand anschließend wiederherstellen:

1. `switch.turn_on` auf der vorhandenen Danfoss-Entity ausführen;
2. nach kurzer Propagationszeit den State lesen und `on` bestätigen;
3. `switch.turn_off` ausführen und den stabilen `off`-State abwarten;
4. bei einem nur verzögerten ersten Read-back höchstens einmal kontrolliert wiederholen;
5. den aggregierten Raumkontakt und alle Zielstates abschließend prüfen.

Ein Service-Aufruf mit HTTP `200` ist dabei allein kein Erfolgsnachweis.

## Entwicklung

```sh
scripts/test
```

Die CI besteht aus Ruff-Lint/Format, pytest, Python-Compileprüfung und HACS-Validierung. Die Integration ist für Home Assistant 2026.9+ und den getesteten HA-Stand 2026.10.0 ausgelegt.
