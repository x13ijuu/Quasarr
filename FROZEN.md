# Dieser Fork ist eingefroren (seit 2026-09-04)

Letzte Verhaltens-Version: **`4.6.19-maja.41`**. Grundlage: maja-hq **ADR 0034**
(„Media-Betriebsmodus: begrenzte Automatik, Quasarr-Fork eingefroren").

**Warum:** 41 Versionen in 8 Wochen haben jeweils eine Instanz desselben Problems repariert.
Quasarr verspricht Sonarr einen Newznab-/SABnzbd-Vertrag (festes Datum, Größe, eindeutige ID,
Titel = Inhalt, Fehlschlag = Fehlschlag), den DDL-Quellen strukturell nicht liefern. Das ist
mit Code nicht abschließbar. Der Hebel liegt im Betriebsmodus (Sonarr ohne eigene Upgrades und
Retries), nicht im Fork.

**Was noch passiert:**
- Upstream-Rebases über die Auto-Rebase-Pipeline (`.github/workflows/`) **innerhalb desselben
  Majors**, damit das Image nicht hinter `rix1337/Quasarr` zurückfällt. Konfliktlösungen dürfen
  Upstream-Verhalten in die Fork-Pfade nachziehen, wenn beide sonst widersprechen (Beispiel
  `4.7.3-maja.42`: AL-Zeilenbeschriftungen gelten auch für die Fork-Prüfung `_tab_offers_episode`).
- Sicherheitsfixes, CI-Pflege und Abhängigkeits-Pins, ohne die ein Rebuild bricht.

Aktueller Upstream-Stand: **`6.0.0-maja.51`** (2026-11-10, v6-Re-Port).

**Upstream-MAJORS sind keine Automatik (seit 2026-11-10).** Ein neuer Major verschiebt oft genau
die Pfade, auf denen die ~20 maja-Verhaltens-Patches sitzen — das ist ein Projekt-Re-Port, keine
Konfliktlösung im Vorbeigehen. Der Auto-Rebase-Workflow hält einen neuen Major deshalb per
**Major-Guard** zurück (`action=hold`: kein Branch, kein Issue, nur Log) und rebaset nur noch
innerhalb des aktuellen Majors automatisch. Der Guard hebt sich selbst auf, sobald ein Port
`maja/main` auf den neuen Major hebt.
- **v6.0.0** (2026-11-08, Neuschreibung sites→templates + Config/DB-Migration) wurde als Projekt
  re-portet (#49, Branch `maja/port-v6` → #50): die gesamte maja-unique Logik (async-accept,
  ban-parking, Regrab-Loop D1/D2/D3, MX, cloudflare-Cap, AL-Anime-Numbering) auf die
  Template-Architektur umgezogen, test-first (volle Suite grün). Fork-Version `6.0.0-maja.51`.
  Der Live-Deploy (Config/DB-Migration + Probe-Batch) bleibt Rufus-gated.

**Was nicht mehr passiert:**
- Verhaltens-Fixes für neue Fehlerformen (Numbering, Sprach-Flags, Regrab-Schleifen, …).
  Die Antwort auf die nächste Anomalie ist ein manueller Grab oder eine Config-Änderung im
  Betrieb. Ausnahme nur per Rufus-Entscheid und Nachtrag in ADR 0034.

Das Wissen aus den 41 Versionen bleibt dokumentiert in maja-hq (Skill-Resource
`media-quasarr-jd.md`, Incident `2026-08-31-regrab-loop.md`, `tests/test_arr_contract.py` hier).
