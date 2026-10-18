# Architectuur

Zonentra modelleert één gebouw per Home Assistant-installatie. Eén centrale
config-entry en aparte ruimte-entries delen een `BuildingRuntime`. De UI beheert
topologie en instellingen; bestaande Home Assistant-entiteiten zijn de invoer en
apparatuur. De integratie heeft geen aanvullende runtime-dependencies.

## Modules

| Onderdeel | Modules |
|---|---|
| Configuratie en migratie | `config_flow.py`, `models.py`, `migration.py`, `topology.py` |
| Bronkwaliteit en klimaatvraag | `observations.py`, `temperature.py`, `demand.py`, `presence.py` |
| Groepsbeslissingen en simulatie | `engine.py`, `decision.py`, `indoor.py`, `control.py`, `override.py` |
| Regelplannen | `executor.py`, `thermostat.py`, `roomstate.py`, `shading.py`, `rain.py`, `wind.py`, `bookings.py`, `forecast.py`, `floor_heating.py` |
| Home Assistant-lifecycle en uitvoering | `__init__.py`, `runtime.py`, `runtime_commands.py`, `runtime_timing.py`, `runtime_helpers.py`, `runtime_shading.py`, `runtime_bookings.py`, `runtime_office.py`, `runtime_floor.py` |
| Entiteiten en diagnostiek | `entity.py`, `climate.py`, `sensor.py`, `number.py`, `switch.py`, `diagnostics.py` |

Alle modules staan in `custom_components/zonentra`. Pure kernmodules berekenen
plannen zonder Home Assistant-instance. De runtime vertaalt bronupdates naar
snapshots, beslissingen en toegestane serviceaanroepen. De entiteiten tonen
bediening, plannen, blokkades en resultaten.

## Lifecycle en aansturing

De runtime staat in `ConfigEntry.runtime_data`. Wijzigingen aan ruimtes stellen
het gezamenlijke gebouwmodel opnieuw samen. Listeners volgen gekoppelde bronnen;
deadline-timers bewaken wachttijden en commandobevestigingen. Weersverwachtingen
en agenda's worden daarnaast periodiek opgehaald. Unload ruimt listeners en timers op.

Groepsarbitrage voorkomt tegengestelde warmte-/koelcommando's aan een gedeelde
buitenunit. Analyse en coördinatie delen `engine.select_group_mode`: eenzijdige
geldige ruimtevraag wordt gevolgd; alleen tegengestelde vragen gebruiken scoredrempels.
Vraagtoestanden blijven leidend als hysterese tijdelijk een nul-score oplevert.
De groepsschakelaar blokkeert HVAC en lamellen; ruimteregeling, zonwering en
vloerverwarming hebben daarnaast hun eigen toestemming.
Vloerverwarming houdt een aparte vaste ondergrens aan (standaard 16 °C).
De airco volgt de thermische ruimtevraag zonder op de vloer te wachten;
koeling wacht op bevestigde uitschakeling van gekoppelde vloerthermostaten.
Actuele regen en wind hebben voor screens een beschermend openingsdoel met een
droge/rustige wachttijd. Dit deelt de bestaande coverdispatcher en bronlisteners.
Natte en winderige periodes overleven een herstart; tijd offline telt niet als
droog- of rustbewijs. Beide voorwaarden moeten vrijgeven voordat screens sluiten.
De dispatcher controleert blokkades opnieuw vóór verzending. Stoppen, bevestigen, wachten en opnieuw starten
vormen een afzonderlijke omschakelfase.

Serviceacceptatie en bevestiging door een entiteitsstate zijn afzonderlijk
zichtbaar. Timeouts leiden tot herbeoordeling en begrensde herpogingen. Een
bevestigde Home Assistant-state bewijst geen fysieke compressorstand.
De beslisregels en tijden staan in [ALGORITHM.md](ALGORITHM.md).

## Opslag en privacy

Configuratie blijft in Home Assistant-config-entries. De laatst toegepaste
HVAC-preset, geleerde thermische tempos, voorbereidingsresultaten en zonweringspauzes
gebruiken lokale HA-opslag. Het zonweringsrecord koppelt apparatuur-ID aan entiteit
en bewaart alleen de laatste beweging en de handmatige eindtijd. Een herstart telt
stabiliteit opnieuw; een reload draagt lopend regelgeheugen over. Thermische
profielen bewaren meettijd, spreiding en een begrensde extra voorbereidingsmarge.
Oude profielen zonder leeftijd blijven leesbaar, maar sturen de planning pas na
nieuw meetbewijs. `ZoneDemand.priority` weegt afspraakurgentie uitsluitend in
groepsarbitrage; bronvalidatie, autorisatie en schakeltijden blijven afzonderlijk gelden.

Diagnostiek redigeert namen, identifiers en gekoppelde bronnen en beperkt de
geëxporteerde observaties. Agenda-invoer bewaart geen titels, beschrijvingen of
locaties. Tests gebruiken fictieve entiteiten. Gebruikersconfiguratie, exports,
logs en lokale ontwikkelinstellingen horen niet in de publieke repository.

## Verificatie

`tests/` bevat kern-, flow-, lifecycle-, aansturings- en privacytests. CI voert
pytest en Ruff uit. Een praktijktest blijft nodig om de beschikbare feedback,
veiligheidstijden en bediening van aangesloten apparatuur te beoordelen.
