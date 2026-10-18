# Zonentra 0.13.6 — uitlegbare klimaatintelligentie

Dit document beschrijft de berekeningen en actieve aansturing. De pure kern
levert waarnemingen, vraag, voorstellen en plannen; de runtime verstuurt de
commando's voor ingeschakelde regelingen. Een voorstel bewijst geen uitvoering en
verandert de waargenomen actuele modus niet. Actieve functies zijn standregeling,
ruimteregeling, setpoints, ventilatorstanden, lamellen en zonwering.

Sinds 0.6 worden de centrale configuratie en de losse ruimte-items vóór elke
berekening samengevoegd tot één gebouwmodel (`topology.assemble`). De algoritmes
hieronder zijn daardoor identiek voor het oude enkele item en de nieuwe opbouw.

## Gevalideerde bronnen en ruimtesnapshots

HA-states worden op de eventloop gekopieerd naar immutable `SourceState`-waarden.
De pure kern bouwt daaruit per ruimte een `ZoneSnapshot`: temperatuur, twee
grenzen, optionele buitentemperatuur, aanwezigheid, contacten en
IDs van gekoppelde HVAC-, vloerverwarmings- en zonweringsapparatuur.

Vooraf lost het model per ruimte de twee centrale keuzes op. Grenzen komen uit
het gebouw of uit de ruimte; hysterese en meetleeftijd vormen een apart blok.
Alleen actieve instellingen worden voor klimaatvraag gebruikt. Grenzen worden
ingesteld op het centrale apparaat of ruimteapparaat, zonder externe grensentiteiten.

Buitentemperatuur komt uitsluitend uit het gebouw, met een eigen maximale leeftijd.
Eén gevalideerde Reading wordt gedeeld door alle ruimtes. De bron wordt eenmaal
gevolgd en ingepland voor veroudering; buitenweer blijft optionele context.

`Reading.quality` onderscheidt:

| Quality | Betekenis |
|---|---|
| valid | Waarde en eenheid zijn geaccepteerd (bij update_based ook de rapportleeftijd) |
| missing | Bron niet geconfigureerd (`not_configured`) of niet aanwezig in HA (`source_missing`) |
| unknown | HA meldt expliciet unknown (`source_unknown`) |
| unavailable | HA meldt expliciet unavailable (`source_unavailable`) |
| stale | Alleen bij update_based: geen HA-rapport binnen de maximale updateleeftijd (`no_report_within_max_age`) |
| unreliable | Ongeldige/niet-interpreteerbare waarde: `not_numeric`, `not_finite`, `unsupported_unit`, `out_of_range`, `invalid_timestamp`, `untrusted_source` (restored/assumed), `unsupported_state` |

`unknown` en `unavailable` zijn samen de categorie “onbeschikbaar”; `unreliable`
is de categorie “ongeldig”. De bestaande namen blijven behouden zodat automations
op `sensor_health` en `source_health` niet breken. De reden-code staat in
`temperature_validation.reason` en in de diagnostiek.

Temperaturen met °C en °F worden naar Celsius genormaliseerd. Geen eenheid of een
onbekende eenheid wordt niet geraden. Temperatuurmetingen moeten tussen −50 en
65 °C liggen; analysegrenzen tussen 5 en 35 °C. NaN, infinity en niet-numerieke
waarden zijn ongeldig. Deze
bereiken zijn plausibiliteitschecks, geen garantie van sensorcalibratie.

### Validatiestrategie (0.3.1)

Home Assistant biedt drie timestamps: `last_changed` (alleen bij een andere
state), `last_updated` (state of attributen veranderd) en `last_reported` (elke
keer dat de integratie de state schrijft, ook als niets veranderde). Veel bronnen
– KNX zonder `always_callback`, Modbus/MQTT met change-only publicatie, sommige
Shelly-configuraties – schrijven **niets** zolang de waarde gelijk blijft. Dan
blijven ook `last_reported` en `last_updated` staan. Ouderdom bewijst daar geen
storing.

| Strategie | Gebruik | Leeftijdcontrole |
|---|---|---|
| `state_based` (standaard) | Elke bron, ook change-only (KNX) | Geen. Een geldige, beschikbare HA-state is bruikbaar, hoe lang ongewijzigd ook |
| `update_based` (optioneel) | Alleen bronnen die **aantoonbaar periodiek rapporteren** | `stale` als `now − last_reported ≥ max_age_seconds` |

`update_based` gebruikt `last_reported`, nooit `last_changed`: een ongewijzigde
waarde met een recent rapport blijft geldig en herstelt direct bij het volgende
rapport. De strategie is per ruimte in te stellen, of centraal voor ruimtes die
de centrale meetparameters gebruiken. `update_based` vereist een maximale
updateleeftijd groter dan 0. Bij `state_based` wordt `max_age_seconds` bewaard
maar genegeerd. De strategie geldt voor de temperatuur van de
ruimte. De ingestelde temperatuurgrenzen hebben geen leeftijdcontrole.
Buitentemperatuur gebruikt ongewijzigd de eigen
`outdoor_max_age_seconds` (0 = geen leeftijdcontrole).

Beide strategieën blijven unknown/unavailable/restored/assumed-states, ongeldige
eenheden, NaN/infinity, waarden buiten bereik en ongeldige timestamps afkeuren.
Een toekomsttimestamp >5 seconden of een ontbrekende/tijdzoneloze timestamp is
onbetrouwbaar.

**Versheid wordt niet geclaimd zonder bewijs.** `freshness_verified` is alleen
`true` bij update_based met een rapport binnen de maximale leeftijd. Bij
state_based is het altijd `false`: de waarde is bruikbaar, maar niet bewezen
actueel. Ook een HA-rapport bewijst niet dat de fysieke sensor opnieuw gemeten
heeft; gecachte gatewaydata blijft een beperking van de bron.

Root cause 0.2/0.3.0: elke ruimtebron (ook grensentiteiten) werd na standaard
1800 s zonder `last_reported`-update `stale`, en een deadline-timer dwong dat af.
Een KNX-temperatuur die 44 minuten stabiel 20,7 °C bleef, viel daardoor uit de
vraagberekening (`insufficient_data`, `reliable_zones 0`) en herstelde pas bij
een nieuwe telegramwaarde.

Aanwezigheid: on → occupied, off → unoccupied, ontbrekend/ongeldig → unknown.
Contacten: on → open, off → closed. Voor statische binaire toestanden is er geen
leeftijdtimeout; unavailable/unknown/restored/assumed blijven wel ongeldig.
Afwezigheid, open ramen en buitenweer beïnvloeden in 0.3 de
klimaatvraag nog niet: dit is verzamelde context voor volgende modules.

Externe grensentiteiten worden niet meer ondersteund. Oude koppelingen worden
bij het laden verwijderd. Een complete vaste comfortband blijft behouden; een
onvolledige lokale band valt terug op de centrale grenzen. Centrale koppelingen
zonder complete vaste band krijgen de standaard comfortgrenzen 21 / 24 °C.
Opgeslagen thermostaatgrenzen blijven gelden volgens de bestaande voorrang.
Een ontbrekende optionele sensor blokkeert temperatuurvraag niet.

## Aanwezigheid en beweging (0.8)

Vóór de klimaatvraag kiest `presence.py` per ruimte een band, naar het model van
Versatile Thermostat:

1. Aanwezigheid gebouw = `absent` → **afwezig**-band (`away_heating_limit` /
   `away_cooling_limit`, standaard 16 / 30 °C).
2. Anders: ruimte heeft een bewegingssensor en de beweging is `inactive` →
   **eco**-band (standaard 19 / 26 °C).
3. Anders → **comfort**: de normale grenzen, centraal of van de ruimte.

Beweging is `active` zodra de sensor minstens `motion_delay_seconds` (30 s) aan
staat, en blijft dat `motion_off_delay_minutes` (30 min) nadat hij uitging, mits
de beweging eerder bevestigd was. Korte passages tellen niet. Een onbekende of
onbeschikbare aanwezigheid of bewegingssensor geeft comfort; een sensorfout maakt
het dus nooit kouder of warmer dan normaal. De eco- en afwezig-band moeten net als
de comfortband ruimer zijn dan tweemaal de hysterese. De runtime plant een
herbeoordeling op het moment dat de detectie- of nalooptijd verstrijkt. De
bewegingsgeschiedenis staat alleen in het geheugen; na een herstart begint een
ruimte zonder recente beweging op eco.

## Temperatuurvraag per ruimte

Per ruimte: gemeten `T`, verwarmingsgrens `H`, koelgrens `C`, hysterese `h` en
weging `w`. Nieuwe UI-voorstellen: H=21 °C, C=24 °C, h=0,3 °C en w=1.
Dit zijn analysegrenzen; ze worden nooit naar VTherm of apparatuur geschreven.
Bestaande 0.1-configuraties krijgen geen stilzwijgende comfortgrenzen.

Voorwaarde: alle drie de benodigde readings zijn valid en `H + 2h < C`.
Anders: demand=unknown, scores=null, reden insufficient_data of invalid_limits.

1. Vorige vraag heating_demand en `T < H+h`: behoud heating_demand.
2. Vorige vraag cooling_demand en `T > C−h`: behoud cooling_demand.
3. Anders `T < H−h`: heating_demand.
4. Anders `T > C+h`: cooling_demand.
5. Anders neutral.

Bij een onbekende vorige vraag gelden de instapgrenzen uit stap 3/4. Een fout
reset de vorige vraag naar unknown; herstel begint weer met de instapgrenzen.

Scores (graden afwijking × configureerbare weging, geen kW of energie):

```text
heating_score = w × min(5, max(0, H−T))  bij heating_demand; anders 0
cooling_score = w × min(5, max(0, T−C))  bij cooling_demand; anders 0
```

Weegfactor 0,1–10, standaard 1. Afwijkingen zijn begrensd op 5 K per ruimte, zodat
één extreme meting niet onbeperkt domineert. Scores worden op vier decimalen
afgerond. Een door hysterese behouden vraag kan score 0 hebben als de grens al
bereikt is; vraagtoestand en huidige afwijking zijn verschillende grootheden.

## Buitenunitgroep: vraag, actuele modus en voorstel

Groepslidmaatschap komt uit de gekoppelde HVAC-apparatuur. Een groep telt iedere
ruimte **eenmaal**, ook bij meerdere binnenunits. Een zonegroep kan als standaard
voor haar HVAC-apparatuur dienen. Tegenstrijdige expliciete apparaat-/zonegroepen
zijn een configuratiefout. Vloerverwarming en zonwering tellen niet als HVAC-vraag.

Alle gekoppelde ruimtes moeten betrouwbare temperatuurvraag hebben en alle
HVAC-entiteiten moeten beschikbaar zijn. Een lege groep of onvolledige kritieke
data levert desired/proposed=unknown en blocked_reason=insufficient_data.
Beschikbare scores blijven zichtbaar met `reliable_zones` en `member_zones` zodat
gebruikers zien waarom ze nog geen volledig groepsbeeld hebben.

De actuele groepsmodus komt **alleen** uit een expliciet geselecteerde, betrouwbare
feedback-entiteit. Geldige waarden: heat, cool, off. Auto, heat_cool en ontbrekende,
verouderde of onbetrouwbare bronnen betekenen current_mode=unknown. Individuele
climate-setpoints of gewenste VTherm-modi bewijzen de fysieke buitenunitmodus niet.
Wanneer binnenunits binnen een beperkte groep expliciet zowel heat als cool
melden, blokkeert `observed_group_conflict` het voorstel van de analyse-engine.
De actieve standregeling kan via de coördinator alsnog een leidende modus uit
de volledige klimaatvraag kiezen en conflicterende units corrigeren.

Bereken `S_heat=sum(heating_score)`, `S_cool=sum(cooling_score)` en `Δ=S_heat−S_cool`.
Standaard minimale score M=0,5; score-deadband D=0,5:

1. Geen enkele ruimte vraagt verwarmen of koelen: desired=off (`no_zone_demand`).
2. Alleen `heating_demand` of alleen `cooling_demand`: kies die richting, onafhankelijk
   van M en D. De vraagtoestand is leidend, ook als de score nul is tijdens het afmaken
   van de ruimtehysterese (`heating_only_demand` / `cooling_only_demand`).
3. Beide richtingen vragen: bij `max(S_heat,S_cool) < M` blijft een huidige modus met
   geldige vraag behouden (`weak_demand_keep_current_mode`); zonder zo'n modus volgt off.
4. Anders, bij `abs(Δ) < D`: behoud de huidige heat/cool-modus met vraag, ook bij
   score nul. Zonder passende huidige modus is desired=unknown (`balanced_demand`).
5. Bij voldoende verschil kies heat bij Δ>0, anders cool.
6. Actuele modus unknown: proposed=unknown; geen fysieke feedback wordt verzonnen.
7. Gewenste modus verschilt van bekende current en minimuminterval is nog niet verstreken:
   proposed=current, blocked_reason=minimum_switch_interval, blocked_until=deadline.
8. Anders proposed=desired; een omschakelvoorstel vereist alle bestaande voorwaarden.

`select_group_mode` wordt door analyse en actieve coördinatie gedeeld. De coördinator
mag binnen een gebalanceerde/zwakke gemengde vraag de bekende gecoördineerde modus
met vraag vasthouden. Bij waargenomen conflicterende unitmodi bepaalt dezelfde helper
de herstelrichting. Ontbrekende meetgegevens of apparatuur worden hiermee niet omzeild.

De minimumtijd (standaard 900 seconden) geldt voor alle overgangen, ook vanaf/naar
off. Hij wordt gemeten vanaf de laatste **waargenomen** modusverandering of
betrouwbare heracquisitie. Een voorgestelde verandering reset de klok niet.
Op herstart en options-reload start een nieuwe conservatieve wachttijd. Dat kan
langer wachten betekenen, maar voorkomt dat restart een wachttijd omzeilt.
Er is geen persistente uitvoerstatus of hardwareplan om te herstellen.

### Rekenvoorbeeld

A: 20 °C, H=21, C=24 → warmte, score 1. B: 23 °C → neutraal, score 0.
C: 24,8 °C → koeling, score 0,8. Met alle gewichten 1 is Δ=0,2, dus binnen de
standaarddeadband. Een bekende heat/cool-modus met voldoende vraag wordt behouden;
bij actuele off blijft het voorstel unknown wegens balanced_demand.
Met weging A=2 wordt Δ=1,2: desired=heat. Of proposed ook heat wordt, hangt af van
betrouwbare modusfeedback en de omschakelwachttijd. Zonder feedback blijft proposed
unknown. Dit kiest bewust stabiliteit boven een omschakeling bij een klein verschil.

## Binnenunits en afgeleide groepsmodus (0.4)

Binnenunits zijn de als `hvac`-apparatuur gekoppelde climate-entiteiten. De pure
module `indoor.py` interpreteert ze; de beslisengine (`engine.py`) is niet
gewijzigd en gebruikt deze interpretatie niet.

**Per binnenunit** wordt gelezen: beschikbaarheid, selected mode (entity state),
`hvac_action`, `current_temperature`, `temperature` (setpoint), `fan_mode` en
`last_updated`. Ontbrekende attributen worden niet geraden.

| Beschikbaarheid | Betekenis |
|---|---|
| available | Geldige state van een betrouwbare bron |
| missing | Entiteit bestaat niet (meer) in HA |
| unknown / unavailable | HA meldt dit expliciet |
| untrusted | `restored` of `assumed_state` |

| Selected mode | Classificatie |
|---|---|
| heat / cool | Verwarmen / koelen geselecteerd |
| off | Uit (geen bewijs dat de buitenunit uit is) |
| fan_only | Geen verwarmen of koelen geselecteerd |
| auto / heat_cool | Niet te classificeren zonder activiteit (`auto_mode_unclassified`) |
| dry | Apart; nooit automatisch gelijk aan cool (`dry_mode_selected`) |
| overig | `unsupported_mode_selected` |

`hvac_action`: heating/preheating/defrosting → heating; cooling → cooling;
drying → drying; idle/off/fan → inactief; ontbrekend → onbekend
(`hvac_action_missing`). Een selected mode die de gemelde activiteit niet kan
veroorzaken is een **mismatch** (bijv. off + heating, heat + cooling, fan_only +
cooling). heat + idle is normaal: ingesteld op verwarmen, nu niet actief.

**Per groep:**

1. *Selected consensus* (beschikbare units): heat én cool → `mixed`
   (`selected_heat_and_cool`); anders heat → `heat`, cool → `cool`; alleen
   off/fan_only → `off`; anders (auto/dry/onbekend) → `unknown`.
2. *Reported activity*: heating én cooling → `mixed`
   (`reported_heating_and_cooling`); anders heating, cooling, drying of
   `inactive`; zonder bruikbare `hvac_action` → `unknown`.
3. *Derived mode*: elk conflict → `mixed` met het conflict als reden. Anders
   consensus heat/cool (tenzij de activiteit tegenovergesteld is →
   `selected_heat_reported_cooling` / `selected_cool_reported_heating`),
   consensus off → `inactive` (`all_units_off` / `no_heat_or_cool_selected`),
   bij een ambigue modus alleen heat/cool als de activiteit dat meldt
   (`ambiguous_mode_heating_reported`), anders `unknown`. Geen units →
   `no_indoor_units`; geen beschikbare units → `no_available_indoor_units`.
   Een conflict wordt nooit opgelost door één unit voorrang te geven.
4. *Kwaliteit*: `completeness` = complete / partial (unit onbeschikbaar of
   modus onbruikbaar) / none. `quality` = consistent (alles bekend en eens) /
   selected_only (hvac_action ontbreekt) / partial / conflict / none.

Een off-unit naast een verwarmende unit is normaal en geen conflict.

**Vergelijking met de gewenste modus** (`observation_reason`, alleen diagnose):

| Situatie | Reden |
|---|---|
| Afgeleid gelijk aan gewenst (off ↔ inactive telt als gelijk) | observed_matches_desired |
| Gewenst heat/cool, units inactief | heat_requested_units_inactive / cool_requested_units_inactive |
| Gewenst heat, afgeleid cool (of omgekeerd) | mode_mismatch_observed |
| Gewenst off, units verwarmen/koelen | units_active_without_demand |
| Afgeleid mixed / unknown | indoor_units_conflict / indoor_observation_unknown |
| Gewenst unknown | desired_mode_unknown |
| Geen binnenunits | no_indoor_units |

**Veiligheidsregel.** `physical_mode` is uitsluitend de bestaande `current_mode`
uit de expliciet ingestelde modus-feedback van de groep. De afgeleide modus vult
die nooit in, verandert `desired_mode`, scores of `proposed_mode` niet, en kan
`current_mode_unknown` niet opheffen of een omschakeling vrijgeven.

## Gesimuleerde regelstrategie (0.5)

**Alles in dit hoofdstuk is simulatie.** De pure modules `override.py` en
`control.py` importeren geen Home Assistant en roepen geen services aan. Elk
resultaat draagt `simulation: true`; stappen worden nooit als uitgevoerd of
bevestigd gerapporteerd. De Decision Engine (0.2–0.4) is ongewijzigd.

### Niveau A — leidende groepsmodus

| Decision Engine | Groepsmodus |
|---|---|
| desired heat / cool | heat / cool |
| desired off (geen geldige vraag) | neutral |
| zwakke/gemengde of gebalanceerde vraag | huidige gecoördineerde modus zolang die geldige ruimtevraag heeft, anders neutral |
| observed_group_conflict (units al in heat én cool) | dezelfde gedeelde keuze op complete ruimtevraag bepaalt de herstelrichting; drempels gelden alleen bij tegengestelde vragen |
| insufficient_data, onbeschikbare units, ontbrekende sensoren | unknown → alle nieuwe acties geblokkeerd |

De groepsmodus is leidend voor iedere binnenunit in de groep, ook tijdens een
handmatige override. Bij neutral kiest Zonentra nooit zelf heat of cool. Een
"laatst bekende modus als voorkeur" bij neutral is bewust niet geïmplementeerd.

### Niveau B — actie per binnenunit

Volgorde (de eerste regel die geldt beslist):

1. Unit onbeschikbaar/ontbrekend/onbetrouwbaar → `blocked` (`indoor_unit_unavailable`).
2. Groepsmodus unknown → `blocked` (`group_mode_unknown`).
3. Groepsomschakeling bezig → zie state machine; een override omzeilt die niet.
4. Manual override actief:
   - groep heat/cool: compatibele modus → `hold` (`manual_override`), anders
     `correct_mode` naar de groepsmodus (`manual_override_mode_corrected`);
   - groep neutral: als geen andere unit in de tegengestelde modus draait →
     `hold` (`manual_override_neutral`); `auto` volgt draaiende units of wordt
     `blocked` (`mode_undetermined`); tegengestelde draaiende units → `blocked`
     (`group_conflict`), nooit willekeurig een kant kiezen.
5. Zonevraag unknown (sensorfout) → `blocked` (`zone_demand_unknown`).
6. Zonevraag gelijk aan groepsmodus → `turn_on_heat` / `turn_on_cool`, `hold`
   (`following_group_mode`) of `correct_mode`.
7. Groep neutraal (te weinig totale vraag) maar de ruimte vraagt nog en haar unit
   draait al in die modus → `hold` (`room_still_asks`, 0.13.4); een unit die uit
   staat start dan niet.
8. Anders gewenst `off`: `no_zone_demand`, `group_mode_neutral`,
   `zone_demand_conflicts_group_mode` of `override_expired` → `turn_off` of `hold`.

Compatibele modi: bij heat → heat, fan_only; bij cool → cool, dry, fan_only.
`desired_mode` is de eindtoestand, `action` alleen de eerstvolgende stap en
`steps` de volledige reeks (bijv. `turn_off → wait_changeover → turn_on_cool`).

### Anti-pendel

| Instelling | Standaard | Soort | Regel |
|---|---|---|---|
| `min_on_seconds` | 600 s | comfort | geen `turn_off` binnen deze tijd na inschakelen |
| `min_off_seconds` | 180 s | veiligheid | geen `turn_on` binnen deze tijd na uitschakelen |
| `changeover_wait_seconds` | 300 s | veiligheid | wachttijd tussen stoppen en de tegengestelde modus |
| groep `min_switch_seconds` | 900 s | comfort | bestaande parameter, hergebruikt: minimale tijd per groepsmodus |

De tijd sinds de laatste overgang is HA `last_changed` van de climate-state; na een
herstart begint die opnieuw (conservatief). Een geblokkeerde actie toont
`blocked_until`, en de runtime plant daarop een herbeoordeling. De standaard
veiligheidswaarden zijn **niet geverifieerd** met Mitsubishi-documentatie.

### Groepsomschakeling (state machine)

| Fase | Wanneer |
|---|---|
| idle / completed | geen omschakeling nodig (completed blijft staan na de laatste omschakeling) |
| preparing | omschakeling gewenst, minimale groepsmodusduur nog niet verstreken (`waiting_until`) |
| blocked | groepsmodus unknown, units onbeschikbaar of harde conflicten |
| stopping | units in een incompatibele modus draaien nog → gepland `turn_off` |
| waiting | niets meer te stoppen; veiligheidswachttijd vanaf het eerste moment zonder draaiende units |
| applying | units met vraag of override staan nog niet in de nieuwe modus |
| verifying | units staan in de nieuwe modus, afgeleide groepsmodus nog niet consistent |
| failed | geselecteerde modus en gemelde activiteit spreken elkaar tegen |

De fase volgt **uitsluitend waarnemingen**: zonder commando's blijft een
omschakeling in read-only bijvoorbeeld in `stopping` staan zolang units in de oude
modus draaien. `switch_allowed_in_simulation` zegt of de voorwaarden in de
simulatie vervuld zijn; `active_switch_permitted` is in 0.5 altijd `false`
(blockers `read_only_simulation` en, zonder fysieke modusfeedback,
`current_mode_unknown`). Voor toekomstige coördinatie zonder buitenunitfeedback
zijn de voorwaarden: alle units beschikbaar, geen harde conflicten, verlopen
anti-pendeltijden, en een geverifieerde (selected én reported) eindtoestand.

### Manual override

Bron: de state-events van de gekoppelde climate-entiteit. Classificatie van één
`state_changed`-event:

| Overgang | Classificatie | Override |
|---|---|---|
| off → heat/cool/dry/fan_only/auto/heat_cool | manual_on | start (tenzij uitgeschakeld) |
| actief → off | manual_off | eindigt |
| actief → andere actieve modus | mode_change | ongewijzigd (timer niet verlengd) |
| zelfde state, andere attributen | attribute_update | ongewijzigd |
| unknown/unavailable → actief | from_unavailable | nooit |
| entiteit verschijnt / HA-start / setup | initial / geen event | nooit |
| restored of assumed_state | untrusted | nooit |
| overeenkomend met een Zonentra-commando | zonentra_initiated | nooit |

Same-value-rapporten (`state_reported`) en timers zijn geen overgangen.
**Beperking:** HA-events bewijzen de herkomst niet. De runtime herkent een
HA-gebruiker aan `user_id` en een automation/script aan `parent_id`. Een herkende
automation die een unit aanzet start geen override; een overgang met kale context
kan van een roomcontroller komen en telt als handmatige bediening.

`CommandTracker` registreert voor de actieve aansturing per entiteit de
verwachte state met een begrensde bevestigingsperiode (60 s). Alleen een
overgang naar precies die state binnen die periode telt als Zonentra-initiatief;
daarna is elke overgang weer extern. Er is geen open tijdsvenster dat echte
gebruikersacties onderdrukt. De runtime gebruikt deze tracker voor HVAC-modi,
setpoints en coverposities, met een eigen bevestigingstermijn per toepassing.

Timer: `expires_at = start + manual_override_duration_minutes` (standaard 30,
5–120). Alleen een nieuwe off→aan-overgang start een nieuwe override; updates,
moduscorrecties en rapporten verlengen niets. De runtime plant een herbeoordeling
op `expires_at`, zodat de override ook zonder nieuwe events afloopt. Daarna geldt
de normale regeling (`override_expired` → `turn_off` zonder vraag). Tijd komt uit
één expliciete bron (`dt_util.utcnow` in de runtime, `now` als parameter in de
pure kern), waardoor alles deterministisch testbaar is.

**Herstart:** overrides staan alleen in het geheugen en overleven een herstart
niet. Een verlopen override kan dus nooit terugkomen en een herstart start nooit
een override. Een unit die na een herstart aan staat zonder vraag krijgt de normale
`turn_off`-intentie (met minimale aan-tijd vanaf de herstart).

## Actieve standregeling (0.7)

Dit was de eerste actieve functie. `executor.plan_mode_commands()` is puur; de runtime
voert de commando's uit via `climate.set_hvac_mode` en `climate.turn_off`.

**Doelmodus** = de leidende groepsmodus uit de coördinator (heat/cool). Bij neutral of
unknown wordt niets gestuurd en blijft de laatst gezette stand (`PresetMemory`).

**Wissel** (doel ≠ laatst gezette stand): alleen als sinds de vorige wissel de
bestaande groepsparameter `min_switch_seconds` (standaard 900 s) verstreken is.
Dan per beschikbare unit:
- uit → `set_hvac_mode(doel)`, na 3 s `turn_off` (`preset_pulse`); de uit-puls wordt
  overgeslagen als de unit intussen een andere state heeft;
- aan in een niet-compatibele modus → `set_hvac_mode(doel)` (`switch_running_unit`);
- aan in een compatibele modus (heat: heat, fan_only; cool: cool, dry, fan_only) → niets.

**In sync:** een draaiende unit in een niet-compatibele modus wordt na 30 s
gecorrigeerd (`correct_running_unit`), per unit hooguit eens per 120 s. De runtime
plant een herbeoordeling op het moment dat een correctie mogelijk wordt.

**Veiligheid:** elk commando wordt vooraf in de CommandTracker geregistreerd
(verwachte state, 60 s), zodat de eigen overgang geen Manual Override start.
Fouten worden gelogd (`failed: …`) en breken de runtime niet. Zonder climate-service
wordt niets gestuurd en ook niets als "gezet" onthouden. De schakelaar per groep
(RestoreEntity, standaard aan) stopt alle commando's direct, inclusief openstaande
uit-pulsen; tot hij na een herstart is hersteld, wordt niets gestuurd. De laatst
gezette stand staat in HA-opslag en overleeft herstarts.

**Herkomst van overgangen** (Manual Override): de HA-context van het
`state_changed`-event. Een gebruiker (`user_id`) of een kale context (KNX-roomcontroller)
→ `manual_on`; een context met `parent_id` (automation/script) → `automation_on`,
geen override. Dit is een sterke aanwijzing, geen bewijs.

## Ruimtethermostaat en ruimteregeling (0.9)

`thermostat.py` is puur; de runtime bewaart de thermostaat per ruimte in HA-opslag
(`zonentra.<centrale>.thermostats`) en houdt een kopie in `hass.data`, zodat een
herlaad van de centrale configuratie een net gemaakte wijziging niet verliest.

**Band.** `active_band(preset, bezetting)`: bij een afwezig gebouw altijd `away`;
preset `comfort` volgt de bezetting uit 0.8 (zonder beweging eco); `eco` en
`away` zijn vaste presets. De thermostaat toont de band die nu geldt als preset en
diens grenzen als doeltemperaturen; de gekozen preset staat in `chosen_preset`. Een
doeltemperatuur op de thermostaat is een eenmalige handmatige afwijking
(`manual_*`, de actieve preset blijft zichtbaar): die vervangt de grenzen van de band waarin hij is gezet,
tot `manual_override_duration_minutes` is verstreken (`manual_until`, zoals een
Manual Override), een preset wordt gekozen of de band wisselt; de configuratie
verandert niet. De
comfortband is de eigen band van de thermostaat als die is ingesteld, anders de
geconfigureerde grenzen (`demand.comfort_limits`). Eco en afwezig blijven centraal.
Een eigen band moet voldoen aan `verwarmen + 2·hysterese < koelen`, beide 5–35 °C.

**Thermostaatmodus** (standaard HA-modi, `room_demand`): `heat_cool` laat de vraag
ongemoeid; `heat` maakt koelvraag `neutral` (`thermostat_heat_only`), `cool` maakt
verwarmingsvraag `neutral` (`thermostat_cool_only`), `off`, `dry` en `fan_only` maken
alle vraag `neutral` (`thermostat_off`, `thermostat_dry`, `thermostat_fan_only`).
De thermostaat biedt `dry`, `fan_only` en ventilatorstanden alleen aan als alle
binnenunits van de ruimte ze kennen, en noemt `heat_cool` `auto` als de units dat
doen (opgeslagen blijft `heat_cool`). Met ruimteregeling zet `plan_room_mode` de
units eenmaal per thermostaatwijziging in `dry`/`fan_only` (na `min_off_seconds`,
niet tijdens een Manual Override; `dry` alleen als de groep mag koelen, anders gaat
een door Zonentra drogende unit uit), en `plan_fan` geeft de ventilatorstand
eenmaal per wijziging door aan draaiende units. De groep weegt daarna zoals altijd; een modus dwingt nooit de
buitenunit. Eén doeltemperatuur (`heat`/`cool`) zet de verwarmings- of koelgrens;
de andere grens blijft staan, of schuift mee als de band anders ongeldig wordt
(`single_target`).

**Aan/uit (alleen met Ruimteregeling aan).** De runtime voert de bestaande actie per
binnenunit uit (0.5): `turn_on_heat`/`turn_on_cool` → `set_hvac_mode`,
`turn_off` → `climate.turn_off`. `correct_mode` blijft aan de standregeling,
`hold`/`blocked` sturen niets. Inschakelen gebeurt alleen in een *veilige* modus:
voor een gekoppelde groep is dat de klaargezette stand, en de groepsschakelaar
moet aan staan. Met de groepsschakelaar uit of nog niet hersteld worden ook
ruimteregeling, setpoints, ventilator en lamellen geblokkeerd. Een binnenunit
zonder groep volgt de eigen ruimtevraag. Zolang een eigen commando nog niet is bevestigd
(CommandTracker), of de standregeling in dezelfde ronde of met een openstaande
uit-puls met de unit bezig is, wacht de ruimteregeling.

Een openstaande uit-puls van de standregeling wordt overgeslagen
(`skipped_room_demand`) als de ruimte geregeld wordt en de unit in die stand nodig
heeft (`desired_mode` van de actie = doelstand).

**Zelfregulerend setpoint** (`plan_setpoint`) voor een draaiende unit zonder actieve
override, met geldige ruimtetemperatuur en een `temperature`-attribuut:

- verwarmen: basis = verwarmingsgrens + hysterese,
  setpoint = basis + clamp(basis − T_ruimte, −max, +max);
- koelen: basis = koelgrens − hysterese,
  setpoint = basis − clamp(T_ruimte − basis, −max, +max);
- grenzen van de actieve band; afgerond op `target_temp_step` (standaard 0,5) en
  begrensd op `min_temp`/`max_temp` van de unit (standaard 16–30 °C);
- `max` = `setpoint_max_offset` (standaard 2 °C); versturen alleen bij een verschil
  van minstens een halve stap en hooguit eens per `setpoint_interval_seconds`
  (standaard 300 s); de runtime plant een herbeoordeling na de wachttijd.

De versterking is bewust 1 en zonder integratie: de unit regelt zelf, Zonentra
verschuift alleen zijn doel zodat de ruimtesensor in plaats van de unitsensor telt.

**Mensen gaan voor.** In een geregelde ruimte start naast `manual_on` ook
`manual_off` (unit blijft uit, `manual_override_off`) en `manual_setpoint` een
Manual Override. Een setpointwijziging is `zonentra_setpoint` als ze het verwachte
eigen setpoint bevestigt, `automation_setpoint` met een `parent_id`-context en
`device_setpoint` met een kale context: zonder herkenbare gebruikerscontext
behandelt Zonentra de wijziging als apparaat- of gatewayfeedback en stuurt het
setpoint na de wachttijd terug. Alleen een wijziging door een
HA-gebruiker (`user_id`) is `manual_setpoint`.

## Ruimtetoestand (0.10)

`roomstate.py` is puur.

**Grenzen per ruimte.** Volgorde per grens: de eigen waarde van de ruimte (gezet op
het ruimteapparaat, bewaard bij de thermostaat), dan een eventuele waarde uit de
ruimteconfiguratie, dan de centrale waarde (centraal apparaat). Een ruimte zonder
eigen comfortband houdt de centrale comfortgrenzen. Elke band moet
voldoen aan verwarmen + 2·hysterese < koelen, beide 5–35 °C.

**Raam.** `window_state`: geen contacten → `not_configured`; geen open contact →
`closed` (alle contacten ongeldig → `unknown`; een onbekend contact telt als dicht);
bypass → `bypass`; anders `opening` tot `window_delay_seconds` (standaard 30 s) na
het eerste open contact, daarna `open`. Bij `open` wordt de klimaatvraag `neutral`
(`window_open`) en zet de ruimteregeling draaiende units direct uit, zonder
minimale aantijd; er gaat niets aan tot het raam dicht is.

**Veiligheid.** `safe` als de ruimtetemperatuur `valid` is, anders `sensor_fault`
(de klimaatvraag is dan al `unknown` en er wordt niet op temperatuur geregeld).

**Trend.** Per nieuwe meting (nieuwe `reported_at`) een exponentieel gemiddelde
`ema += α·(T − ema)` met `α = 1 − e^(−Δt/300 s)`, en de helling
`(ema_nieuw − ema_oud)/Δt` in °C/h, gladgestreken met tijdconstante 900 s. Een
onveranderde waarde wordt na 600 s opnieuw bemonsterd, zodat de helling terugloopt
naar nul. Alleen in het geheugen.

**Automatische ventilator.** Geen opgeslagen ventilatorstand = `auto`
(0.13.5); een vaste stand geldt `manual_override_duration_minutes` (`fan_until`).
Zonentra's eigen automaat alleen als de units zelf geen `auto` hebben. Afstand =
verwarmen: grens + hysterese − T; koelen: T − (grens − hysterese). Stand = de
`int(afstand / auto_fan_step)`-de van de units (`auto_fan_step` centraal, standaard
0,5 °C) (zonder `off`), begrensd op de hoogste; zonder
verwarmen of koelen de laagste. Gestuurd via `set_fan_mode`, eenmaal per gekozen
stand en hooguit eens per `setpoint_interval_seconds`.
Een herkende teruggemelde snelheid blijft gelden tot de afstand tot het doel
de volgende snelheidsgrens met minstens `min(0,1 °C, auto_fan_step / 4)` overschrijdt,
of onder de huidige grens minus die marge daalt. Meerdere standen mogen tegelijk
worden overgeslagen bij een grote afwijking. Zonder bruikbare vorige stand geldt
de gewone indeling; zonder temperatuur of in dry/fan_only geldt de laagste stand.
**Ventilatorbediening.** De Zonentra-climate-entiteit publiceert geen `FAN_MODE`,
`fan_mode` of `fan_modes`, zodat HomeKit geen ventilatorsnelheidsbediening aanmaakt.
Ook de snelheidskeuze op de HA-thermostaat vervalt. De automatische regeling,
`fan_control`-schakelaar en bestaande tijdelijke interne keuzes blijven werken.
`fan_names` en `unit_fan` ondersteunen de interne vertaling van bestaande
opgeslagen keuzes naar unitstanden.
Een eerder in HomeKit aangemaakte ventilatorservice kan een eenmalige
`homekit.reset_accessory` van de thermostaat vereisen.

**Lamel.** Draaiende units van de ruimte in `cool`/`dry` → koelstand, in `heat` →
verwarmstand (`vane_mode`). Via `select.select_option`, `number.set_value` of `switch.turn_on/turn_off`
(ook `input_*`; een schakelaar voor lamel auto/stop of swing/stop), eenmaal per start of modewissel; alle units uit wist het
geheugen, zodat de volgende start de stand opnieuw zet.

## Zonwering (0.11)

`shading.py` is puur. **Zon schijnt** (`sunny_state`): vermogen ≥ drempel gedurende
300 s → aan; < 70 % van de drempel gedurende 900 s → uit; geen vermogen → onbekend.
**Zon op de gevel** (`sun_on_facade`): hoogte ≥ 5° en hoekverschil tussen
zonsazimut en gevelrichting ≤ 80°. **Besluit** (`decide_screen`): hoogte < 0 → open
(nacht); zon onbekend → ongewijzigd; geen zon of niet op de gevel → open;
verwarmingsvraag → open; koelvraag → zonwering; anders zonwering als de
ruimtetemperatuur ≥ midden van de actieve band + 0,5 °C (eenmaal omlaag: tot
midden − 0,5 °C), anders open. **Bewegen** (`plan_screen_move`), alleen met
Zonweringregeling: niet bij blokkering, niet tijdens handmatige bediening
(`screen_override_minutes`, standaard 120), niet bij < 10 % verschil.
`screen_min_move_minutes` begrenst bewegingen op standaard eens per 30 minuten.
`stable_screen_target` bewaakt bovendien dezelfde doelpositie gedurende
`screen_shade_delay_minutes` (standaard 5) of `screen_open_delay_minutes` (15).
Dit geldt ook voor de eerste beweging en alle beslisredenen. Een gewijzigde reden
voor dezelfde positie herstart de timer niet; hold, blokkering, handmatige bediening,
uitgeschakelde regeling en onbruikbare bronnen wel. Interval en stabiliteit lopen
gelijktijdig. De runtime plant hun deadlines en bewaart de doelstandtimer bij reload.
`stabilizing` en `next_check` maken het wachten zichtbaar in de zonweringssensor.
`cover.set_cover_position` wordt bevestigd via een eigen CommandTracker
(240 s). Een standwijziging die niet van Zonentra komt en geen automation is,
start de handmatige periode. Herstel vanuit `unknown`, `unavailable` of een
herstelde/aangenomen state telt niet als bediening. Laatste automatische beweging
en handmatige eindtijd worden per fysieke cover opgeslagen en bij een volledige
HA-herstart hersteld. Onbekende, verlopen of ongeldige tijden worden genegeerd;
een gewijzigde entiteitskoppeling erft de vorige pauze niet. Stabiliteitsduur
wordt bij een volledige herstart opnieuw opgebouwd.

`runtime_shading.py` blokkeert bij ontbrekende of onbeschikbare covers. Bij een
ingestelde blokkeerbron staat alleen een betrouwbare `off` beweging toe; ontbrekend,
`unknown`, `unavailable`, hersteld of aangenomen geeft `block_unknown`. De dispatcher
controleert dit vlak voor de serviceaanroep opnieuw. Alleen een geslaagde aanroep
zet het tijdstip van de laatste beweging; een fout annuleert de commandotracker en
plant een herbeoordeling na 120 s. De retry blijft bij centrale reloads behouden.

De zonstand-entiteit is instelbaar (standaard `sun.sun`). Ongeldige azimut/elevatie
of een onbeschikbare bron geeft `sun_unknown` en een Reparaties-melding, die na
herstel of bij verwijderen van de screens verdwijnt.

## Weersverwachting (0.12)

`forecast.py` is puur. De runtime vraagt elke 30 minuten (en bij het laden; zonder
verwachting na een fout opnieuw na 120 s) `weather.get_forecasts` met type `hourly`
op. `summarize_forecast` neemt de uren met een tijd in (nu − 1 uur, nu + 12 uur]:
maximum en minimum. `day_type`: verwachting ouder dan 3 uur of leeg → `unknown`;
maximum ≥ `warm_day_temperature` (22) → `warm`; ≤ `cold_day_temperature` (12) →
`cold`; anders `neutral`. `demand_margins` geeft (verwarmen, koelen):
warm → (`forecast_margin`, 0), koud → (0, `forecast_margin`), anders (0, 0).
Tijdens een actieve handmatige temperatuurafwijking of actieve afspraak-/
kantoorvoorbereiding zijn beide marges nul. Bij het vervallen van die bediening
worden de weersmarges weer toegepast, met behoud van de normale vraaghysterese.
`calculate_demand` start verwarmen pas onder grens − hysterese − marge
(daartussen: neutraal, reden `heating_deferred_warm_day`) en koelen pas boven
grens + hysterese + marge (`cooling_deferred_cold_day`); een lopende vraag houdt de
gewone hysterese. `decide_screen` krijgt dagtype en bezetting (band comfort of een
handmatige temperatuur): na nacht/zon/gevel en de verwarmings-/koelvraag kiest een
lege ruimte bij een bekend dagtype zonwering (`empty_room_keep_cool`), op een koude
dag open (`empty_room_solar_gain`); een bezette ruimte op een warme dag zonwering
(`warm_day_forecast`), op een koude dag open (`cold_day_forecast`); anders de
ruimtetemperatuur zoals in 0.11.

## Events en herbeoordeling

Bronveranderingen gebruiken `async_track_state_change_event`. Voor rapporten met
ongewijzigde waarde is er een gefilterde `EVENT_STATE_REPORTED`-listener, zoals de
actuele HA-API voorschrijft. De runtime plant slechts één timer naar de eerstvolgende
sensorvervaldatum of geblokkeerde omschakeldeadline. Er is geen periodieke polling,
behalve het opvragen van de weersverwachting (elke 30 minuten, alleen met weerentiteit)
en agenda’s (elke vijf minuten, alleen met ingestelde ruimteagenda).
Binnenunits zijn al gevolgde apparatuurentiteiten: een `hvac_action`-wijziging
is een attribuutwijziging en komt via hetzelfde state_changed-event binnen.
Override-afloop, anti-pendelblokkades en wachttijden van de omschakeling worden
als deadlines in dezelfde ene timer meegenomen. Ook de bevestigingsdeadlines van
commando’s en de back-off na een fout of ontbrekende bevestiging worden meegenomen.
Bij unload worden zowel bronlisteners als de timer verwijderd.

De beslisengine importeert geen Home Assistant en heeft geen uitvoerinterface.
Elke fysieke apparatuurvermelding blijft daarnaast een hold-beslissing behouden
in de bestaande Status-sensor, ook als de nieuwe groepssensor een mode voorstelt.

## Adaptieve afspraakvoorbereiding

Een optionele `calendar_entity` per zone vraagt via `calendar.get_events` de
volgende 24 uur op. `runtime_bookings.py` actualiseert elke vijf minuten en bij
agenda-statewijzigingen. Alleen start- en eindtijden worden behouden; hele-dagitems
worden genegeerd, overlappende en aaneengesloten intervallen samengevoegd. Een
response ouder dan tien minuten of een onbetrouwbare kalenderbron geeft `unknown`
en kiest eco, of afwezig als het gebouw afwezig is.

De voorbereidingsparameters komen uit de centrale configuratie, tenzij de ruimte
`use_building_preconditioning = false` gebruikt. Dit betreft maximumtijd, marge
en beide starttempos. Bestaande expliciete ruimtewaarden blijven lokaal actief;
nieuwe ruimtes erven standaard centraal. Geleerde tempos blijven per ruimte.

`bookings.plan_booking` schat de tijd tot de comfortband uit de temperatuurafstand
(tot verwarmingsgrens + hysterese of koelgrens − hysterese), gedeeld door het
verwarmings- of koeltempo, plus de ingestelde marge. Standaard zijn tempo’s
0,05 °C/min, de marge 10 min en het maximum 180 min. Voor onbekende of nog niet
ingeleerde buitencondities geldt een conservatieve beginschatting:
verwarmen `basis / (1 + 0,03 × max(0, 10 − buiten))`, koelen
`basis / (1 + 0,03 × max(0, buiten − 25))`. Bij onbekende buitentemperatuur is
de deler 1,3. Dit is een begrensde startheuristiek, geen fysisch capaciteitsmodel.

Tijdens actieve, ongeblokkeerde ruimteregeling worden geldige temperatuurverschillen
uit intervallen van 5–30 minuten gebruikt voor een EMA (α = 0,25). Alleen een tempo
van 0,005–0,5 °C/min in de actieve richting telt mee. Verwarmen en koelen hebben
ieder een eigen tempo per buitenklasse (< 0, 0–10, 10–20, 20–30, ≥ 30 °C, onbekend).
Vanaf vier samples met bekende meettijd, hooguit 30 dagen oud, en relatieve
spreiding ≤ 0,5 wordt het profiel gebruikt. De EMA-variantie gebruikt dezelfde
α; standaardafwijking/gemiddelde bepaalt de spreiding. De schatting verlaagt het
gemiddelde met die spreiding. Een sneller tempo dan de beginschatting wordt
geleidelijk vertrouwd: factor `min(1, samples/8) × (1 − spreiding)`. Een lager tempo
werkt direct conservatief. Profielen zonder tijdstempel, te oude of te variabele
profielen gebruiken de beginschatting. Bij nieuw leren na 30 dagen of zonder
bekende leeftijd begint het aantal samples opnieuw. Een mode- of buitenklassewisseling
start een nieuw meetinterval. Profielen blijven opgeslagen; meetankers beginnen
na reload opnieuw.

Bij de overgang van voorbereiding naar afspraak wordt eenmalig de comfortband
beoordeeld, met een hooguit vijf minuten oude temperatuurwaarneming. Uitgeschakelde
regeling, overrides, open/onbekende contacten, apparaatuitval, bekende commandofouten
of agenda-uitval tijdens voorbereiding sluiten marge-aanpassing uit. Een geldige
`too_cold`/`too_warm` verhoogt de richtingsmarge in die buitenklasse met vijf minuten,
maximaal dertig extra; `ready` verlaagt de oorspronkelijke richtingsmarge met één.
Onbekende meetgegevens veranderen de marge niet. De maximale voorbereidingstijd
blijft alles begrenzen. Het laatste resultaat en de correcties blijven opgeslagen.

Een comfortvraag van een gereguleerde agendaruimte krijgt groepsprioriteit
`1 + clamp(1 − resterende_minuten / max(1, voorbereidingstijd), 0, 1)` tijdens
voorbereiding en 2 tijdens de afspraak. De scores blijven verder gelijk; alleen
groepsarbitrage vermenigvuldigt ze met de prioriteit. Deze wordt tijdens voorbereiding
elke minuut opnieuw beoordeeld. Eco, Afwezig, thermostaat-uit, uitgeschakelde
ruimteregeling en apparaat-overrides krijgen geen verhoging. Groepsblokkades,
onbetrouwbare bronnen, deadband en minimale schakeltijd blijven gelden.

Tijdens `preparing` en `meeting` vraagt de agenda comfort, ook als gebouwpresentie
`absent` is, zodat voorbereiding vóór aankomst mogelijk blijft. Bij `meeting`
en bewezen gebouwafwezigheid geldt vanaf `start + 15 minuten` weer Afwezig
(`booking_no_arrival`). De runtime plant ook deze deadline in, begrensd door het
afspraakeinde. Latere aanwezigheid hervat comfort; opnieuw afwezig na de deadline
geeft direct Afwezig. De marge wordt niet herstart bij een refresh of reload.
Onbekende of niet ingestelde gebouw-aanwezigheid wordt niet als afwezig behandeld.
Overlappende en aansluitende afspraken gelden als één aaneengesloten venster;
de aankomstmarge begint bij de start van dat venster.
Een gestart voorbereidingsvenster blijft tot het einde actief als de
ruimte zijn temperatuur al bereikt; een aangepaste maximumtijd blijft begrenzen.
De forecast-startmarges worden voor een actieve afspraak niet toegepast. Expliciete
thermostaatinstellingen en alle bestaande apparaat-/groepsblokkades blijven leidend.
Buiten `preparing` en `meeting` gebruikt een agendaruimte eco, onafhankelijk van
beweging, tenzij het gebouw afwezig is. Ook bij een onbekende, verouderde of onbruikbare agenda
geldt eco (`calendar_unknown`) of de afwezig-band bij gebouwafwezigheid; een herstelde agenda hervat de actuele planning.
Expliciete presets Eco en Afwezig blijven gelden. Timers evalueren start en einde zonder nieuw
sensorevent. `BookingSensor` toont alleen planning en schatting, geen afspraakinhoud.

## Centrale openingstijden

`office_schedule_entity` is één optionele centrale `schedule`-helper, te selecteren
onder Aanwezigheid. Alle ruimtes zonder agenda volgen deze helper; agendaruimtes
blijven uitsluitend hun afspraken volgen. Er bestaat geen ruimte-override voor
het openingstijdenschema.

`workday_entity` is een afzonderlijke optionele centrale `binary_sensor` voor
vandaag (offset 0). Zonder sensor verandert de planning niet. Met sensor mogen
kantoorvoorbereiding en aankomstmarge alleen bij een betrouwbare `on`-state werken.
`off` geeft `not_workday`; ontbrekende, unknown/unavailable, restored/assumed of
ongeldige states geven `workday_unknown`. Deze blokkade verandert aanwezigheid,
agenda-afspraken en de gewone temperatuurregeling niet. Sensorwijzigingen worden
gevolgd en de planning wordt bij lokale middernacht opnieuw beoordeeld.
Een opening op een andere lokale datum wacht tot middernacht
(`waiting_for_workday`): de status van vandaag wordt niet gebruikt om een andere
dag als werkdag te beschouwen.

Bij `off` is `next_event` de volgende opening. De bestaande adaptieve planner
berekent hiervoor een voorbereidingsvenster vanuit de comfortband, kamertemperatuur,
buitentemperatuur en geleerde tempos. Deze ruimtes leren ook thermische tempos,
met dezelfde kwaliteitscontroles als agendaruimtes. Maximumtijd, beginsnelheden en
buffer volgen de bestaande voorbereidingsinstellingen. Een actieve voorbereiding
vraagt comfort en krijgt dezelfde begrensde groepsprioriteit als afspraakvoorbereiding;
forecast-startmarges vervallen gedurende de voorbereiding.

Bij `on` is `next_event` de sluiting. Alleen een voortgezette voorbereiding krijgt
bij bewezen afwezigheid maximaal 15 minuten aankomstmarge vanaf de geplande opening,
begrensd door de sluiting. Een herstart tijdens al geopende uren maakt geen nieuwe
aankomst aan. De marge blijft bij configuratieherladen behouden voor dezelfde
schema- én werkdagsensor-entiteit. Een timer vlak vóór het werkelijke `on`-event houdt voorbereiding
maximaal 30 seconden vast (`awaiting_opening`) om een uit/aan-cyclus te voorkomen.
Zonder bevestigde opening stopt deze voorbereiding daarna. Aaneengesloten
blokken die `on` blijven starten geen nieuwe marge.

Na aankomst of afloop van de marge geldt de normale aanwezigheid/bewegingsregeling;
ook buiten openingstijden blijft werkelijke aanwezigheid werken. Het schema verandert
nooit gebouwpresentie. Onbekende, herstelde of onbruikbare schemabronnen vragen geen
voorbereiding. Een verplaatste/verwijderde opening annuleert de vorige voorbereiding.
Expliciete Eco/Afwezig, thermostaat-uit, uitgeschakelde regeling en groepsblokkades
blijven leidend. Vloerverwarming en regen-/windbescherming houden hun eigen regels.
Planning en starttijd staan in attributen van de ruimtevraagsensor en in diagnostiek;
de schema- en werkdagsensor-entiteit worden bij export geredigeerd.

## Herstel en actieve timing na de vervolg-review

De actieve uitvoering bewaakt minimale aan-/uit-tijd en heat/cool-omschakelpauze
via `runtime_timing.py`, ook voor groepspresets en zelfstandige units. Stopaanroepen
moeten eerst in HA als uit worden waargenomen. Alleen al serviceacceptatie start de
wachttijd niet. Eerder draaiende units worden daarna hervat zonder preset-uit-puls;
een reeds uitgeschakelde unit krijgt pas na zijn minimale uit-tijd een presetpuls.
Bij correctie naar de bestaande groepsstand blijft minstens de 30 s
bedieningsvertraging gelden, naast de ingestelde minimale aan-tijd.

Voor modus, setpoint, ventilator en lamel geldt 60 s bevestigingstijd, voor covers
240 s. Een verlopen bevestiging krijgt een herbeoordeling en maakt de bijbehorende
toegepast-cache ongeldig. Retry start op 120 s en verdubbelt bij herhaald falen tot
maximaal 1.800 s. Alleen de actuele planner mag opnieuw verzenden: gewijzigde vraag,
uitgeschakelde regeling of een override mag geen oud commando laten herleven.
Een geaccepteerde preset-uit-puls die niet in de state terugkomt, krijgt eveneens
een herpoging. Bestaande minimumintervallen voor setpoints en covers blijven gelden.

Ventilator en lamel worden pas na een geslaagde aanroep als toegepast gemarkeerd;
de marker blijft niet bestaan na een servicefout of bevestigingstime-out. Tijdens
een handmatige apparaat-override worden ventilatorcommando’s gepauzeerd en de cache
gewist, zodat automatisch regelen na afloop weer kan hervatten.

Onbekend zonvermogen wist `pending_since`, maar bewaart de laatst bevestigde
zonnigheidsstatus. Thermisch leren gebruikt het tijdstip van een nieuw bronrapport,
niet van een willekeurige runtime-update. Een oud rapport of een rapport van vóór
het actieve regelinterval levert geen nieuw meetanker op.

## Activiteitsweergave en ventilatorvolgorde

De thermostaat gebruikt bruikbare `hvac_action` per binnenunit vóór de geselecteerde
modus. Idle geeft geen verwarmings- of koelactiviteit. Bij ontbrekende feedback
blijft de geselecteerde modus een terugval; `activity_source` benoemt of de weergave
gerapporteerd, afgeleid, gemengd of niet beschikbaar is. Thermostaat-uit blijft Off.
Bekende ventilatornamen en numerieke standen met dezelfde eenheid worden
oplopend gesorteerd. Opaque of gemengde fabrikantstanden behouden hun volgorde.

## Vloerverwarming met airco-voorrang

Een ingeschakelde Vloerverwarmingregeling stuurt uitsluitend climate-thermostaten
met een kamertemperatuursetpoint. Een aparte vaste ondergrens (standaard 16 °C)
geldt centraal, met optionele eigen waarden per ruimte. Deze volgt geen
Comfort/Eco/Afwezig-band of agenda-voorbereiding. Apparaatstappen worden afgerond;
onbruikbare feedback of een doel buiten de apparaatgrenzen blokkeert opdrachten.
Eerst wordt het setpoint bevestigd, daarna mag de vloer naar heat. De eigen
vloerthermostaat regelt de warmteafgifte. Oude number-koppelingen worden alleen
geobserveerd. De airco volgt direct de bestaande thermische ruimtevraag; er is
geen vloerwachttijd of automatische gasbijverwarming.

Bij koeling/dry, thermostaat uit of een bevestigd open raam stopt de actieve
vloerregeling. Aircokoeling wacht op betrouwbare off-feedback van iedere gekoppelde
climate-vloerthermostaat. De vloer mag niet verwarmen zolang een gekoppelde airco
in dezelfde ruimte op cool/dry staat, ook zonder actuele koelvraag. De dispatcher
controleert dit opnieuw vóór iedere opdracht. Restwarmte in de vloer wordt niet
geschat. Handmatige wijzigingen worden gedurende de centrale overnameduur
gevolgd, behalve noodzakelijke stopopdrachten voor raam/uit/koeling.
De groepsschakelaar blokkeert airco en lamellen; vloerregeling is onafhankelijk.
Regeling uit laat het laatste vloersetpoint en de apparaatstand staan.
De koelblokkering staat los van die regeltoestemming: gekoppelde climate-vloeren
moeten ook bij uitgeschakelde vloerregeling betrouwbare off-feedback geven.
Dezelfde controle blokkeert cool/dry bij de laatste controle vóór verzending.
Uitgeschakelde vloerregeling verstuurt daarbij geen vloercommando’s.

De sensor Opwarming met airco volgt ruimtes met gekoppelde vloer en airco waarvan
beide regelingen zijn ingeschakeld. Alleen gemelde aircoactiviteit heating,
preheating of defrosting tijdens warmtevraag telt; alleen de geselecteerde stand
heat is onvoldoende bewijs. Na 60 minuten onafgebroken gemelde verwarmingsactiviteit
signaleert de sensor onvoldoende opwarming als de ruimte nog minstens 1 °C onder
de verwarmingsgrens ligt en minder dan 0,3 °C is gestegen. Er moet na de start
opnieuw een geldige ruimtetemperatuur zijn gerapporteerd. Zonder nieuwe rapportage
is de uitkomst onbekend. Onderbreking of wegvallende vraag reset de observatie;
voldoende stijging begint een nieuwe observatieperiode. Dit is uitsluitend een
zichtbaar signaal, geen rendementsmeting of automatische verhoging van het gasdoel.

## Regenbeveiliging van screens

Regenbeveiliging gebruikt de actuele state van `screen_rain_entity` (weather of
binary_sensor), of bij een lege keuze de centrale `weather_entity`. Zonder bron
blijft de gewone zonregeling gelden. `screen_rain_protection` staat standaard aan.
`rainy`, `pouring`, `lightning-rainy`, `hail`, `snowy` en `snowy-rainy` vragen omhoog;
`lightning` wordt uit voorzorg hetzelfde behandeld, omdat Pirate Weather onweer
naar die algemene HA-conditie vertaalt. Voor een binary_sensor is on nat en off
droog. Een voorspelling, neerslagkans of dagsom wordt niet als regen-nu gebruikt.

Bij neerslag krijgt iedere ingeschakelde screen een doel van 100%. Deze opening
slaat zonne-stabilisatie, minimumtijd tussen bewegingen en handmatige pauzes over.
Een lopende sluitopdracht wordt vervangen; een reeds lopende opening wordt niet
herhaald. De centrale blokkeer-entiteit, beschikbaarheid van de cover en de
ruimteschakelaar blijven leidend. De laatste servicecontrole blokkeert sluiten
ook als de actuele bron sinds de planning nat of onbekend is geworden.

Na regen blijft het openingsdoel staan tot de bron onafgebroken droog is geweest
voor `screen_rain_dry_minutes` (standaard 30). Nieuwe regen of onbruikbare data
reset de droogtijd. Daarna gelden de normale zonvoorwaarden, minimumtijd tussen
bewegingen en sluitstabilisatie opnieuw. Uitval/restored/assumed/ongeldige states
of weather-rapporten ouder dan `screen_rain_max_age_minutes` (standaard 60) gelden als
onbekend. Zonder eerdere regen blokkeert onbekend alleen het sluiten; na regen
houdt het ook het openingsdoel vast. Rapporten worden bij verlopen opnieuw
beoordeeld via een deadline, ook zonder nieuwe events. Een binary_sensor volgt
state-based validatie: een ongewijzigde beschikbare off-state wordt niet oud
verklaard, omdat fysieke regensensoren vaak alleen veranderingen melden.

Een configuratiereload bewaart de lopende droogtijd. De nat-status wordt lokaal
opgeslagen voor dezelfde bron; na een volledige herstart begint de droogtijd
opnieuw, omdat tijd offline geen bewijs van droog weer is. Servicefouten krijgen
een begrensde herpoging; een mislukte sluiting vertraagt de regenopening niet.
De screensensor en diagnostiek tonen reden, regenstatus, blokkade en vrijgavetijd.

Pirate Weather levert weersinformatie op afstand; de huidige conditie kan een
lokale bui missen of later melden. Een gekoppelde fysieke regensensor kan dezelfde
regeling voeden. Broncontrole: [Pirate Weather 1.9.2 weather.py](https://github.com/Pirate-Weather/pirate-weather-ha/blob/v1.9.2/custom_components/pirateweather/weather.py).

## Windbeveiliging via actuele weergegevens

`screen_wind_protection` staat standaard aan en gebruikt de centrale weather-bron.
Actuele `windy`/`windy-variant` geeft direct hetzelfde beschermende openingsdoel als
regen. Dit werkt ook zolang fabrikantgrenzen onbekend zijn; er wordt geen numerieke
veiligheidsgrens verzonnen. `screen_wind_speed_limit` en `screen_wind_gust_limit`
zijn afzonderlijk optioneel in km/u. Een ingestelde grens wordt ook beoordeeld:
wind >= grens of windstoot >= grens vraagt direct omhoog, ongeacht of de andere
waarde beschikbaar is. De weerprovider-windmelding blijft een extra reden om te
openen, ook met ingestelde grenzen. Richting, gevel en voorspellingen spelen voor
dit beschermende doel geen rol.

De weather-attributen `wind_speed` en `wind_gust_speed` gebruiken de gepubliceerde
`wind_speed_unit`: km/h, m/s, mph, kn of ft/s worden omgerekend naar km/u. Negatieve,
niet-eindige of ontbrekende waarden en onbekende eenheden zijn geen nulmeting.
Zonder numerieke grens telt uitsluitend een bekende actuele weerconditie; met
grenzen moeten alle beoordeelde waarden beschikbaar zijn om sluiten vrij te geven.

Een windalarm blijft staan tot de provider geen windmelding meer geeft en, waar
grenzen zijn ingesteld, alle beoordeelde waarden <= 80% van hun grens zijn.
Daarna moet dit onafgebroken `screen_wind_calm_minutes` (standaard 30) aanhouden.
Ontbrekende, restored, assumed of te oude gegevens (`screen_wind_max_age_minutes`,
standaard 60) resetten de rustige periode. Zonder eerder alarm blokkeren onbekende
gegevens alleen sluiten; na alarm blijft ook het openingsdoel staan. Wind gaat
voor regen in de getoonde beslisreden; beide blokkades moeten vrij zijn voordat
de normale regeling weer mag zakken. Gewone minimumtijden, handmatige pauze en
sluitvertraging volgen dezelfde regels als bij regenbeveiliging.

De actieve windstatus wordt lokaal bewaard voor dezelfde weerbron; na herstart
begint een rustige periode opnieuw. Een configuratiereload bewaart de timer;
wijziging van bron of windgrenzen start een lopende rustige periode opnieuw.
Bron, werkwijze, actuele waarden, grenzen en vrijgavetijd zijn zichtbaar in de
screensensor en de geredigeerde diagnostiek. Een centrale blokkeer-entiteit houdt
voorrang op alle bewegingen, ook als het weer een windalarm meldt.

Pirate Weather levert actuele weerinformatie op afstand, geen lokale windmeting.
Een ontbrekende windmelding bewijst daarom geen veilige windbelasting bij de gevel.
Voor numerieke grenzen blijven de specificaties van de screenfabrikant leidend.
Bronnen: [Pirate Weather 1.9.2](https://github.com/Pirate-Weather/pirate-weather-ha/blob/v1.9.2/custom_components/pirateweather/weather.py),
[Home Assistant weather-attributen](https://www.home-assistant.io/integrations/weather/#state-attributes).
