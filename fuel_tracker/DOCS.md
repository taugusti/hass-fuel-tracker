# Fuel Tracker

A fuel log for your vehicles. Take a photo of the pump display and the odometer – Home Assistant's AI Task reads the numbers, you check them and save.

## Requirements
An AI Task entity that supports attachments (images), e.g. the Anthropic (Claude) integration's `ai_task.claude_ai_task`.

## Usage
1. Add your vehicles under **Vehicles**.
2. Pick a vehicle at the top.
3. **Pump display**: take or choose a photo – liters, total and price per liter are filled in.
4. **Odometer**: take or choose a photo – the reading is filled in.
5. Fields filled from photos are highlighted. Correct anything that is wrong, tick "Filled to the top" and press **Save refuel**. Nothing is saved before that.

When you use **Choose** (an existing photo), the date and time are taken from the photo's EXIF capture time (the pump photo wins over the odometer photo). Without EXIF, the file date is used and you are asked to check it.

Consumption (km/L) is calculated from full tank to full tank. Liters from partial refuels are added to the next full refuel. If you forgot to log a refuel, tick "A refuel is missing since the last one" so that period does not give a wrong number.

## Vehicles
Add, edit and delete vehicles under **Vehicles**: name, whether the odometer shows km or miles, when the odometer rolls over (old 5-digit mechanical odometers: 100000) and the tank size (gives the range). Deleting a vehicle also deletes its refuels and photos.

## Options
- `ai_task_entity`: the AI Task entity (default `ai_task.claude_ai_task`).
- `publish_sensors`: exposes `sensor.fuel_tracker_<vehicle>_*` in Home Assistant (km/L last tank and average, odometer, cost per km, fuel price, cost this year, last refuel). The sensors are recreated when the app starts and removed when a vehicle is deleted or renamed.
- `currency`: currency label shown in the app and on sensors (default `kr`).

## Data
- Database: `/data/fuel_tracker.db` (included in the app's backup).
- Photos: `/media/fuel_tracker/`.
- CSV export under **Log**.
