# Dieser Fork ist eingefroren (seit 2026-09-04)

Letzte Verhaltens-Version: **`4.6.19-maja.41`**. Grundlage: maja-hq **ADR 0034**
(„Media-Betriebsmodus: begrenzte Automatik, Quasarr-Fork eingefroren").

**Warum:** 41 Versionen in 8 Wochen haben jeweils eine Instanz desselben Problems repariert.
Quasarr verspricht Sonarr einen Newznab-/SABnzbd-Vertrag (festes Datum, Größe, eindeutige ID,
Titel = Inhalt, Fehlschlag = Fehlschlag), den DDL-Quellen strukturell nicht liefern. Das ist
mit Code nicht abschließbar. Der Hebel liegt im Betriebsmodus (Sonarr ohne eigene Upgrades und
Retries), nicht im Fork.

**Was noch passiert:**
- Upstream-Rebases über die Auto-Rebase-Pipeline (`.github/workflows/`), damit das Image nicht
  hinter `rix1337/Quasarr` zurückfällt.
- Sicherheitsfixes und CI-Pflege.

**Was nicht mehr passiert:**
- Verhaltens-Fixes für neue Fehlerformen (Numbering, Sprach-Flags, Regrab-Schleifen, …).
  Die Antwort auf die nächste Anomalie ist ein manueller Grab oder eine Config-Änderung im
  Betrieb. Ausnahme nur per Rufus-Entscheid und Nachtrag in ADR 0034.

Das Wissen aus den 41 Versionen bleibt dokumentiert in maja-hq (Skill-Resource
`media-quasarr-jd.md`, Incident `2026-08-31-regrab-loop.md`, `tests/test_arr_contract.py` hier).
