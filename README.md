# EDA Portal (Home Assistant, HACS)

Holt die Viertelstundenwerte **deines Zählpunkts** aus dem EDA-Portal (prod.eda-portal.at) und schreibt sie als stündliche Langzeitstatistik in Home Assistant. Der manuelle xlsx-Upload entfällt.

Inoffizielle Integration: Sie nutzt die Web-Schnittstelle des Portals, die nicht dokumentiert ist und sich ohne Vorwarnung ändern kann.

## Statistiken (IDs wie beim bisherigen Upload)
- `sensor:eeg_<zaehlpunkt>_verbrauch` – Verbrauch nach Teilnahmefaktor (Schnittstelle `g`)
- `sensor:eeg_<zaehlpunkt>_eigendeckung` – Eigendeckung = `g − p` (p = Restbezug)
- `sensor:eeg_<zaehlpunkt>_vorlaeufig` – 1/0, vorläufige Stunden (Qualität ≠ L1 oder jünger als N Tage)

Fehlende Viertelstunden werden nie als 0 geschrieben, die Stunde wird dann übersprungen.
Nicht enthalten: die Aufteilung der Eigendeckung auf Maritta/Oliver (`_eigendeckung_maritta/_oliver`) und die Erzeuger.

## Installation
1. HACS → Integrationen → ⋮ → Benutzerdefinierte Repositories → `https://github.com/kaipelstefan/eda-portal` (Kategorie Integration)
2. Installieren, HA neu starten.
3. Einstellungen → Geräte & Dienste → Integration hinzufügen → *EDA Portal*: E-Mail, Passwort, Community-ID, Zählpunkt.

## Dienst
`eda_portal.backfill` (`start_date`, optional `end_date`, max. 400 Tage) lädt Historie nach.
Standardmäßig holt die Integration alle 6 Stunden die letzten 14 Tage (einstellbar).
