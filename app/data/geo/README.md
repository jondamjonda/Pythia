# Municipality gazetteer seed provenance (AQ-05)

Authoritative source: **ČSÚ (Český statistický úřad) číselníky**, fetched
2026-07-02 from `https://apl2.czso.cz/iSMS/do_cis_export`.

Raw fetched files:

| File | ČSÚ export | Meaning |
|------|-----------|---------|
| `vaz_obec_orp.csv`   | `kodcis=43 typdat=1 cisvaz=65_1182`  | obec (43/CISOB) → SO ORP (65) |
| `vaz_obec_okres.csv` | `kodcis=43 typdat=1 cisvaz=101_333`  | obec → okres (101, OKRES_NUTS) |
| `vaz_obec_kraj.csv`  | `kodcis=43 typdat=1 cisvaz=100_1250` | obec → kraj (100, KRAJ_NUTS) |
| `cis_okres_101.csv`  | `kodcis=101 typdat=0`                 | okres číselník → `cznuts` LAU1 code |
| `cis_kraj_100.csv`   | `kodcis=100 typdat=0`                 | kraj číselník → `cznuts` NUTS3 code |
| `cis_orp_65.csv`     | `kodcis=65 typdat=0`                  | ORP číselník (reference) |

Joined output: **`municipality_gazetteer.json`** (6,258 municipalities) — the
loader input for `regsuper.ingest.municipality_gazetteer`. Each row carries the
6-digit obec code + name, okres (LAU1 `okres_code` + name), ORP (`orp_code` +
name), kraj (NUTS3 `kraj_code` + name), plus the ČSÚ internal 101/100 codes as
join provenance.

Rebuild: refetch the six files above, then re-run the join (see the loader
docstring / the session report). Verified count ≈ 6,254 (CZ has ~6,254 obce;
the export yields 6,258 including a few military-district / edge entries).
