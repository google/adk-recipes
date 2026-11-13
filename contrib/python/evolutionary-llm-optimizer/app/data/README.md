# Bundled data: London cycle hire sample

`cycle_hire.parquet` is a sample of real Santander Cycles journeys in London.

- **Source:** Transport for London (TfL) cycle hire usage data,
  <https://cycling.data.tfl.gov.uk/> (`usage-stats/` journey extracts
  `02b`, `03`, `04`, `05`, `06` and `07` for 2016). The same journeys are in
  BigQuery as `bigquery-public-data.london_bicycles.cycle_hire`.
- **Licence:** TfL open data terms, based on the Open Government Licence
  v2.0, which allow copying, redistribution and adaptation with attribution.
  Powered by TfL Open Data.
- **What is included:** journeys that started between 2016-03-01 and
  2016-05-31, 1 in 40 of them (selected by DuckDB `hash(rental_id) % 40 = 0`),
  60,672 rows. Rows with a missing id, date, duration or station were
  dropped.
- **Columns:** `rental_id`, `duration` (seconds), `bike_id`, `end_date`,
  `end_station_id`, `end_station_name`, `start_date`, `start_station_id`,
  `start_station_name`: the core columns of the BigQuery table, with the same
  names and types. Times are as published by TfL (London local time).
- **Layout:** sorted by `start_date`, Zstandard-compressed Parquet with row
  groups of 4,096 rows, so a filter on `start_date` can skip row groups the
  way a partitioned or clustered BigQuery table skips data.
