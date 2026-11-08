# Brændstof

Benzinlog for jeeperne. Tag et billede af pumpens display og bilens km-tæller – Claude (via HA's AI Task) aflæser tallene, du tjekker dem og gemmer.

## Sådan bruges den
1. Vælg bil øverst.
2. **Pumpe-display**: tag billede → liter, beløb og kr/L udfyldes.
3. **Km-tæller**: tag billede → kilometerstand udfyldes.
4. Ret evt. tallene, sæt flueben i "Fyldt helt op" og tryk **Gem tankning**.

Forbrug (km/L) beregnes fra fuld tank til fuld tank. Tanker du ikke helt op, lægges literne til næste fulde tankning. Har du glemt at registrere en tankning, så sæt "Der mangler en tankning siden sidst", så den periode ikke giver et forkert tal.

## Biler
Under **Biler** sættes om tælleren viser km eller miles, hvornår tælleren slår om (gamle 5-cifrede tællere: 100000) og tankens størrelse (giver rækkevidde).

## Indstillinger
- `ai_task_entity`: AI Task-entiteten (standard `ai_task.claude_ai_task`).
- `publish_sensors`: lægger `sensor.braendstof_<bil>_*` ud i HA (km/L, km-tæller, kr/km, literpris, udgift i år, sidst tanket). Sensorerne genskabes når appen starter.

## Data
- Database: `/data/braendstof.db` (med i HA's backup af appen).
- Billeder: `/media/braendstof/`.
- CSV-eksport under **Log**.
