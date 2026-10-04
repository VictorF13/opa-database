# 01. Source data

This document describes the raw sources: what they are, how they arrive,
their formats, and what has been measured about them. It is the factual
basis for the design in the documents that follow.

All statements marked **Profile** were measured on a November 2023 sample
and are re-measured on the full raw store in phase P0 (see the
[README](README.md#profile-facts)).

## 1. Where the raw data lives

The raw files are held in a remote file store (a Google Drive folder) that
the system reads and never writes to. The local machine holds a verified
mirror. See [04-raw-and-bronze.md](04-raw-and-bronze.md).

The exact contents of the remote store (which years exist for each source,
total size, any formats not described below) are not yet inventoried. The
first task of the roadmap is to list and profile it.

## 2. Sources at a glance

| Source | Content | Delivery unit | Volume (Profile) |
| --- | --- | --- | --- |
| AVL | GPS pings from on-board devices | One CSV file per local day | About 130 million pings per month |
| AFC | Fare taps nested inside operator trip records | One zipped XML dump per day | About 15.4 million taps and 0.94 million trips with taps per month |
| GTFS | Planned network and timetable | One zip per schedule export | About 130 exports from 2015 to 2026 |
| Vehicle dictionaries | Claimed mappings between fleet numbers and GPS identifiers | A handful of CSV files | A few thousand rows each |
| Reference lists | Operating companies, garages, terminals | Curated by hand | Tens of rows |

## 3. AVL (vehicle location)

### 3.1 Format

- Location: `DADOS_GPS/<year>/<month folder>/`, one CSV file per day.
  Month folder names are Portuguese month names with inconsistent spacing,
  punctuation, and accents (for example `NOVEMBRO-2023`, `ABRIL - 2023`).
  The same month can appear under two folder names, one of them a stray
  partial copy.
- Daily files have no header row. Fields, in order, as documented in the
  source's own field dictionary file (`GPS data fields.txt`):

| # | Source name | Meaning |
| --- | --- | --- |
| 1 | `direction` | Compass heading in degrees, 0 to 359 (see 3.2) |
| 2 | `latitude` | Decimal degrees |
| 3 | `longitude` | Decimal degrees |
| 4 | `metrictimestamp` | UTC timestamp, `YYYYMMDDHHMMSS` |
| 5 | `odometer` | Cumulative distance counter (unit to be confirmed) |
| 6 | `routecode` | Route number set on the device, 0 when unset |
| 7 | `speed` | Speed (unit to be confirmed, consistent with km/h) |
| 8 | `device_deviceid` | Device identifier, text |
| 9 | `vehicle_vehicleid` | Internal vehicle identifier of the AVL system, integer |

- The year 2018 uses a different layout: one file per month
  (`Paint<MM>2018.csv`) directly under the year folder, with a header row
  whose names differ slightly but whose column order is the same.
- Some files are present but empty. Some lines are truncated or corrupt.

### 3.2 Characteristics (Profile)

- **A daily file covers a local day, not a UTC day.** Timestamps are UTC
  and Fortaleza is UTC-3, so the file for local day D holds pings from
  03:00 UTC on D to 03:00 UTC on D+1. One UTC date is therefore spread
  across two files. Any design that writes "one output per UTC date per
  file" will overwrite data. Bronze is keyed by file for this reason
  (`BRZ-2`).
- The field named `direction` is a compass heading (360 distinct values),
  not a route direction. The value 0 is over-represented, consistent with
  a stationary vehicle.
- Median interval between consecutive pings of a device is 9 seconds; the
  90th percentile is 32 seconds and the 99th is 60 seconds.
- About 2% of consecutive pings of a device share the same timestamp. A
  device and a timestamp do not identify a ping uniquely.
- About 1,440 devices are active in a weekday daytime hour. Within an hour
  each device maps to exactly one `vehicle_vehicleid` and the reverse.
- Most device identifiers share one family prefix (`ep1-`); a few dozen
  use other formats.
- `routecode` is 0 in about 6% of pings. For buses whose device is known,
  the most common `routecode` in an hour equals the route recorded by fare
  collection in about 82% of bus-hours. It is useful evidence and not
  ground truth.
- Speed: 99th percentile 52, maximum 93. About 14% of pings have speed 0.
- A small share of consecutive pings imply an impossible speed (0.17%
  above 100 km/h): GPS glitches.
- Some pings carry the coordinate (0, 0), a "no fix" marker.
- Cross-track distance from an in-service ping to its route shape: median
  about 4 m, 90th percentile about 12 m, 99th percentile about 210 m.

## 4. AFC (fare collection)

### 4.1 Format

- Location: `DADOS_BILHETAGEM/<year>/V<YYYYMMDD>.zip`, sometimes with a
  trailing `t` before the extension. Each zip holds one XML file.
- The XML is deeply nested. Each level carries attributes:

```text
Movimentos
  MovimentoDiario   data_mov                         (service date)
    Categoria       Tipo
      Empresa       Codigo, modalidade               (operating company)
        Veiculo     Numero, validador                (fleet number, validator)
          Linha     Numero, jornada, num_operador, tabela,
                    hora_abertura, hora_fechamento   (line session)
            Viagem  data_hora_abertura, data_hora_fechamento,
                    catraca_inicio, catraca_final, sentido,
                    ponto_abertura, ponto_fechamento  (operator trip)
              Passageiro  data_hora, evento, Matricula, tipo,
                          integracao, integracao_bum, sigben,
                          valor_pago, valor_subsidio,
                          valor_repasse_metro,
                          latitude, longitude          (fare tap)
```

- The attribute list above is what is known. Files may carry attributes
  or elements beyond it; bronze keeps all of them (`BRZ-6`).
- Earlier years (before 2020) use a different format (per-month folders of
  `Viagenssigom<date>.csv` files). Its structure is profiled in P0.

### 4.2 Characteristics (Profile)

- **A dump is a delayed-upload backlog.** Validators buffer taps and upload
  when they reconnect, so the dump named for one day contains service
  dates going back days or weeks. The dump date is when data arrived, not
  when it happened.
- Timestamps are naive Fortaleza local time.
- `evento` (event identifier) is unique across dumps, except the value
  `0`, a placeholder present on about 1.5% of taps.
- `Matricula` (card identifier) is `0` on about 8% of taps (no card:
  consistent with cash or operator actions). A few other single values
  account for thousands of taps each and are also placeholders.
- 25 distinct `tipo` (passenger type) codes were observed. Their meanings
  are not documented. Shares and fare behavior differ sharply by code: a
  few codes always pay zero, a few are used by a single placeholder card.
- `integracao` takes the values 0 to 3; value 1 almost always pays zero
  (a free transfer). `integracao_bum` is 0 or 1.
- Fleet numbers are numeric with 4 or 5 digits; line numbers are numeric
  with 1 to 4 digits; company codes have 3 characters.
- **About 83% of taps carry a valid coordinate.** The share varies by
  company from 61% to 94%. About 7% of buses have no geotagged tap at all.
- **A tap's coordinate is the on-board device's own position.** For buses
  whose device is known, the tap coordinate equals a ping of that device
  within 60 seconds in more than half of cases (median distance 0 m, 84%
  within 100 m). The strongest rival device is a median 3.2 km away. This
  is the single strongest evidence for linking buses to devices.
- The recorded service date equals the local calendar date of the tap for
  99.86% of taps. The rest fall on the following calendar day, almost all
  between 00:00 and 02:59. Tap volume is lowest between 02:00 and 03:59
  local time and ramps up from 04:00.
- A closing timestamp of `1899-12-30` appears on a few trips: a marker for
  "never recorded".
- **One operating company (code 67, a different modality) has fare data
  and no AVL feed.** It accounts for about 14% of taps. About 61% of its
  taps are geotagged, so its buses still have a sparse position trace.
- Fare taps are not all passengers. Devices are also tapped to configure
  or test them. Which codes mark such taps is not known in advance.
- Whether the source contains trip records with no taps under them is not
  known. Bronze represents them if they exist (`BRZ-7`).

## 5. GTFS (schedule)

### 5.1 Format

- Location: `GTFS/<year>/exportacao*.zip`. Four filename conventions exist
  across the years (`exportacao_YYYY-MM-DD.zip`, `exportacaoDDMMYYYY.zip`,
  the same with a stray space, and `exportacao_DD-MM-YYYY.zip`). At least
  one filename has a typographical error in the year.
- Each zip is a full snapshot of the standard GTFS text files. The known
  set is `agency`, `calendar`, `calendar_dates`, `fare_attributes`,
  `fare_rules`, `routes`, `shapes`, `stop_times`, `stops`, `trips`, plus a
  UTF-16 duplicate of `stops`. Other files may be present.

### 5.2 Characteristics (Profile)

- A snapshot stays in effect until the next export. There is no "monthly
  schedule". Several exports can fall in one month.
- A few exports lack an entire file (`calendar_dates.txt` or
  `stop_times.txt`).
- Identifiers carry meaningful leading zeros. `route_id` is the line
  number padded to 4 digits (`0051`); `route_short_name` is unpadded
  (`51`).
- `direction_id` is empty. Direction is encoded in the shape identifier:
  `shape<route>-I` (outbound, "ida") and `shape<route>-V` (return,
  "volta"). Most routes have both; circular routes have one.
- Text fields contain trailing padding spaces.
- `arrival_time` and `departure_time` exceed `24:00:00` for trips that
  cross midnight.
- **A shape can carry several stop patterns.** In the export of 2023-11-10
  (315 routes, 617 shapes, about 62,000 trips), 17 shapes on 14 routes
  have more than one distinct ordered stop list. Example: circular route
  `0051` has one shape and four stop patterns that chain end to end around
  the loop (stop 5809 to 6079, 6079 to 6104, 6104 to 5822, 5822 to 5809),
  each numbered from stop sequence 1. A scheduled trip on such a route is
  one segment, not the whole loop.
- Stop order along a shape is not always geometrically consistent. A small
  share of stop lists contain stops that do not belong to their shape.
- Shapes are sometimes wrong: observed vehicles consistently follow a
  different street.
- Some services run a shortened version of a route on certain days without
  that variant existing in the schedule.

## 6. Vehicle dictionaries

- Six CSV files claim mappings between fare-collection fleet numbers and
  AVL identifiers: a current list, a 2018 list, an older list, two further
  lists with their own identifier ranges, and dated device-registry
  exports that also carry company, plate, and status.
- **They are not reliable.** They are snapshots taken long after most of
  the data, they cover only part of the fleet, and they contradict each
  other (fleet renumbering over the years). About 2% of codes map to more
  than one vehicle within a single snapshot.
- They are used as weak prior evidence only (`INF-32`).

## 7. Identifiers across sources

| Concept | AFC | AVL | GTFS | Dictionaries |
| --- | --- | --- | --- | --- |
| Bus | `Veiculo.Numero` (fleet number) | not present | not present | several forms |
| Device | `Veiculo.validador` (validator, a different device) | `device_deviceid` | not present | device registry |
| AVL vehicle | not present | `vehicle_vehicleid` | not present | several forms |
| Route | `Linha.Numero` (unpadded) | `routecode` (integer) | `route_id` (4 digits) | not present |
| Direction | `Viagem.sentido` (0 or 1) | not present | shape suffix `-I`, `-V` | not present |
| Company | `Empresa.Codigo` (3 characters) | not present | `agency_id` | company name |
| Stop | `ponto_abertura`, `ponto_fechamento` | not present | `stop_id` | not present |

The fleet number's first two digits (after padding to five) identify the
operating company. The AVL vehicle identifier and the fleet number look
alike and are different numbering spaces.

AFC direction 0 corresponds to GTFS `-I` and 1 to `-V`, with at least one
known exception: route 614 is reversed. Exceptions are reference data
(`REF-8`) and are also detected from observation (`INF-73`).

## 8. Time

- AVL timestamps are UTC. AFC timestamps are local. GTFS times are local
  times of day relative to a service day.
- The local zone is `America/Fortaleza` (UTC-3 with no daylight saving
  time in the period covered). Conversions use the zone name, never a
  fixed offset.
- Three different "days" exist and are kept distinct (`ARC-30`): the UTC
  date, the local calendar date, and the operational day that runs from
  early morning to early morning.

## 9. Known sources of error in the data

This list drives the categories defined in
[07-inference.md](07-inference.md).

| Error | Example |
| --- | --- |
| Trip record opened or closed at the wrong time | Opened in the garage; left open across several runs; closed hours late |
| Overlapping trip records for one bus | Two records covering the same minutes |
| Trip record with wrong route or direction | Driver selected the wrong line or did not switch direction |
| Service operated with no trip record | Taps recorded under the previous record |
| Non-service movement recorded as a trip | Garage to first terminal; return to garage |
| Partial runs | Short turn; breakdown; service cut on certain days |
| Device not mappable to a bus | Dictionary wrong; device swapped; stationary terminal device |
| Bus with fares and no device | Company without AVL feed; unit without GPS |
| Taps that are not passengers | Configuration and test taps |
| Schedule not matching the street | Wrong shape; split route; missing variant |
| GPS noise | No-fix coordinates; jumps; gaps |
| Late, duplicated, truncated, or empty files | Backlog dumps; stray folder copies; cut lines |
