# Changelog

## 0.3.1 – 2026-11-08
- Number fields accept both comma and dot as decimal separator, and thousands separators ("35336,2", "35.336,2", "35,336.2"). Values are shown in the browser's number format.
- "Will be saved as" line under the form shows exactly what will be stored; numbers that cannot be read block saving.

## 0.3.0 – 2026-11-08
- All settings in the app (new Settings tab): currency, distance and volume units, consumption format, warning threshold, fuel types with CO₂ factors, AI Task entity, sensors on/off. No app options in Home Assistant any more.
- Sensors renamed to consumption_last / consumption_avg / cost_per_distance and published in the chosen units.
- Navigation moved to the top; "+ Register refuel" on Statistics and "← Statistics" on Refuel; starts on Statistics when there is data; adding the first vehicle opens Refuel.
- The pump reading also reports whether the display shows liters or gallons.

## 0.2.0 – 2026-11-08
- Renamed to Fuel Tracker throughout (slug `fuel_tracker`, sensors `sensor.fuel_tracker_*`, `/media/fuel_tracker`).
- Everything in English; numbers and dates follow the browser's locale.
- Vehicles can be added, edited and deleted; no vehicles are pre-created.
- Sensors of deleted or renamed vehicles are removed.
- Currency option.

## 0.1.x – 2026-11-08
- First versions (Danish): photo reading via AI Task, highlighted auto-fill, EXIF date for chosen photos, log, statistics, CSV export and HA sensors.
