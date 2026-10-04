# Glossary

| Term | Meaning |
| --- | --- |
| Activity | A stretch of a device's timeline with one kind of behavior: in a garage, laying over at a terminal, following a pattern, deadheading, stopped, moving without classification, or a gap |
| AFC | Automated fare collection: the system that records fare taps and operator trip records |
| As-of join | A join that pairs each row with the latest row of another table at or before its time |
| Asset | A table (or other data product) that the orchestrator knows: it has upstream assets, a partitioning, a code version, and checks |
| AVL | Automatic vehicle location: the system that records GPS pings from on-board devices |
| Baseline method, full method | The two specified ways of solving an inference problem: the simplest that can work, and the complete treatment adopted when the gates demand it |
| Block | Everything a vehicle does from leaving a garage to returning to one |
| Boarding stop | The stop at which a tap is inferred to have happened |
| Bronze | The lossless, text-typed copy of raw files, one output per raw file |
| Bus | A vehicle as fare collection knows it, identified by its fleet number (`bus_id`) |
| Card key | The pseudonym of a fare card, a keyed hash of its identifier |
| Catalog | The database that records which files form which lake table at which snapshot |
| Coverage | The share of a pattern's length that a run traversed |
| dbt model | One SQL transformation that produces one table, with its tests and documentation |
| Deadhead | Movement without passengers between a garage, a terminal, and the start or end of service |
| Declared | As entered by the operator in fare collection, as opposed to observed |
| Device | An on-board GPS unit as AVL knows it, identified by `device_id` |
| Dump | One delivered fare collection file; a backlog of taps uploaded late, not one day of service |
| Entity-day | One device or one bus on one operational day; the unit of parallel work |
| Event date | The UTC date of an event |
| Fare class | Whether a tap is a passenger, not a passenger, or unknown |
| FAST, BULK, BACKUP | The three storage tiers |
| Frozen test set | Human labels, randomly sampled and made without seeing a prediction, never used to fit or tune anything |
| Gate | A numeric condition a build must meet to be released |
| Gold | The analysis-ready dimensional model |
| GTFS | General Transit Feed Specification: the schedule format |
| Inference | The layer of facts that no source states: links, timelines, patterns, reconciliation, stop events |
| Lake | The transactional tables, stored as Parquet files, that are the system of record |
| Layover | Time a vehicle waits at a terminal between runs |
| Link | The inferred fact that a device was on a bus during a date interval |
| Local date | The calendar date of an event in `America/Fortaleza` |
| Manifest | The catalog of every raw file version, with checksums |
| Materialization | One execution that produces or replaces a table or one partition of it |
| Observed | As inferred from the vehicle's track, as opposed to declared |
| Operational date | The local date of an event shifted back by the operational-day cutoff, so service after midnight belongs to the previous day |
| Orchestrator | The service that runs every step, in dependency order, and records every result |
| Parameter | A named, versioned tunable value with a recorded justification |
| Partition | A slice of a table that is materialized on its own; for time-based tables, one month |
| Pattern | A way of operating a route: route, direction, ordered stops, and line geometry |
| Ping | One GPS record of a device |
| Production-ready | The state in which the first release exists and backups are proven on a separate device and off the machine |
| Profile fact | A measurement of the source data on a sample, to be re-measured on the full data |
| Progress | A position along a pattern, as a fraction of its length or a distance in meters |
| Protected resource | Anything on the host that this system does not own and people rely on |
| Publish | To load a release into the serving database |
| Quarantine | Where bronze keeps records it could not split into fields |
| Raw | The original files, never modified |
| Reconciliation | Relating each operator trip record to the observed timeline |
| Reference data | Curated facts kept in the repository as seeds and parameters: companies, zones, rule exceptions, code meanings, thresholds |
| Reject | A bronze row that silver could not give a key or an event time; kept with a reason |
| Release (code) | A tagged version of the software |
| Release (data) | A named snapshot of the lake that passed every gate, with the months it covers; what the serving database publishes |
| Run | A maximal stretch of a vehicle following one pattern with forward progress |
| Seed | A small reference table kept as a file in the repository and loaded by dbt |
| Service class | Whether an operator trip record is revenue service, non-revenue, or undetermined |
| Service date | The date fare collection attributes a record to, kept exactly as recorded |
| Serving database | The PostgreSQL and PostGIS instance that analysts and applications query |
| Silver | Typed, cleaned, deduplicated, conformed tables, one per source entity |
| Snapshot | A consistent state of every lake table at one moment; created by every committed change |
| Stop event | The arrival, departure, and dwell of a run at one stop of its pattern, or the reason they are unknown |
| Table format | The layer that turns a directory of Parquet files into transactional tables with snapshots |
| Tap | One fare event recorded by a validator; also called a boarding |
| Tap-derived track | A sparse track built from the coordinates of a bus's taps, used when the bus has no linked device |
| Tier | One of the three storage roles, each a configured directory |
| Track | The ordered pings of one device over an operational day |
| Trip record | The operator-entered record of a trip in fare collection (opened and closed by the driver) |
| Validator | The on-board fare device |
| Working state | The latest snapshot of the lake; never served to readers |
| Zone | A garage or terminal, as an area on the map |
