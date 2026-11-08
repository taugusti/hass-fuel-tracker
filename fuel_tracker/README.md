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

Consumption is calculated from full tank to full tank. Liters from partial refuels are added to the next full refuel. If you forgot to log a refuel, tick "A refuel is missing since the last one" so that period does not give a wrong number.

## Vehicles
Add, edit and delete vehicles under **Vehicles**: name, whether the odometer shows km or miles, when the odometer rolls over (old 5-digit mechanical odometers: 100000) and the tank size (gives the range). Deleting a vehicle also deletes its refuels and photos.

## Settings
Everything is configured in the app under **Settings** (the app has no options in Home Assistant's app configuration):
- Currency label.
- Distance unit (km or miles) and volume unit (liters, US gallons or UK gallons). Data is stored in km and liters, so you can switch units at any time.
- Consumption shown as distance per volume (km/L, mpg) or volume per 100 distance (L/100 km, gal/100 mi).
- Warning threshold for the distance since the last refuel.
- Fuel types (suggested in the form) with a CO₂ factor in kg per liter for the CO₂ statistic.
- The AI Task entity that reads the photos (must support attachments, e.g. `ai_task.claude_ai_task`).
- Whether to publish sensors `sensor.fuel_tracker_<vehicle>_*` (consumption last tank and average, odometer, cost per distance, fuel price, cost this year, last refuel) in the chosen units. Sensors of deleted or renamed vehicles are removed.

## Data
- Database: `/data/fuel_tracker.db` (included in the app's backup).
- Photos: `/media/fuel_tracker/`.
- CSV export under **Log**.
