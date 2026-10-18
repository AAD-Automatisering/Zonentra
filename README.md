# Zonentra

Lokale klimaatregeling voor één gebouw met meerdere ruimtes, als Home Assistant
Custom Integration. Zonentra gebruikt bestaande entiteiten om klimaatvraag te
berekenen en airco's, ventilatoren, lamellen en zonwering te coördineren.
Aanwezigheid, raamcontacten, weersverwachtingen en ruimteagenda's kunnen de
regeling beïnvloeden. Vloerverwarming kan als basisverwarming worden aangestuurd.

## Installatie

Vereist Home Assistant **2026.10.1** of nieuwer; getest met 2026.10.1.
Geen aanvullende runtime-dependencies, YAML of Versatile Thermostat nodig.

1. Open **HACS → Aangepaste repositories** en voeg
   `https://github.com/AAD-Automatisering/Zonentra` toe als **Integration**.
2. Download Zonentra en herstart Home Assistant.
3. Ga naar **Instellingen → Apparaten & diensten → Integratie toevoegen → Zonentra**.
4. Kies eenmalig **Centrale configuratie** en stel het gebouw en de
   buitentemperatuursensor in.
5. Open **Configureren** bij de centrale configuratie en voeg buitenunitgroepen toe.
6. Voeg Zonentra opnieuw toe voor elke **Ruimte** en koppel de temperatuursensor,
   bestaande climate-entiteiten en hun buitenunitgroep.

Handmatig installeren kan ook: kopieer alleen `custom_components/zonentra` naar
`<Home Assistant config>/custom_components/zonentra` en herstart Home Assistant.

## Eerste gebruik

**Actieve standregeling staat per buitenunitgroep standaard aan.** Daarmee kan
Zonentra direct HVAC- en lamelcommando's versturen. Zet deze schakelaar uit
voordat je binnenunits koppelt als je eerst alleen wilt observeren. Uitgeschakeld
blokkeert hij alle HVAC- en lamelcommando's voor de groep; plannen blijven zichtbaar.

**Ruimteregeling**, **Vloerverwarmingregeling** en **Zonweringregeling** staan per ruimte standaard uit.
Controleer eerst de sensoren en schakel daarna de gewenste regeling per ruimte in.
Zonwering heeft een eigen schakelaar en wordt niet door de HVAC-groepsstop geblokkeerd.
Vloerverwarming heeft ook een eigen schakelaar. Koppel een climate-thermostaat
met een gewenste kamertemperatuur. Deze houdt standaard een aparte ondergrens
van **16 °C** aan; Comfort/Eco/Afwezig en agenda-voorbereiding blijven door de
**airco** geregeld. De airco wacht niet op de vloer. De ondergrens is instelbaar
via centrale **Ruimteregeling**, met een afwijking per ruimte. Bij koeling wacht
de airco op bevestigd uitgeschakelde vloerthermostaten. De vloer blijft uit
zolang een airco in dezelfde ruimte op cool/dry staat. Uitschakelen van de
vloerregeling laat de laatste instelling van de vloerthermostaat staan.
De koelblokkering blijft daarbij actief: ook bij uitgeschakelde vloerregeling
moeten gekoppelde vloerthermostaten betrouwbaar `off` melden voordat de airco
mag koelen of ontvochtigen. Zonentra stuurt de vloer dan geen commando’s.
Een KNX-vloerthermostaat met alleen `hvac_modes: heat` wordt ook ondersteund voor
het instellen van de doeltemperatuur. Zonder `off` kan Zonentra de vloer niet
uitschakelen: dan verschijnt **Uitschakelen niet ondersteund** en blijft aircokoeling
geblokkeerd zolang de vloer niet betrouwbaar uit meldt.
De sensor **Opwarming met airco** signaleert langdurig onvoldoende opwarming;
hij schakelt geen extra gasverwarming in.
**Regenbeveiliging** staat standaard aan zodra een weerbron is gekoppeld. De
actuele toestand van de centrale `weather`-entiteit wordt gebruikt, ook die van
Pirate Weather. Bij regen/onweer/neerslag gaan ingeschakelde screens direct omhoog,
zonder de gewone bewegingswachttijden of handmatige pauze. Na **30 minuten droog**
mag de normale zonregeling weer sluiten, met de gewone sluitvertraging. Een
blokkeer-entiteit blijft alle bewegingen blokkeren. Onbekende of meer dan 60 minuten
niet gerapporteerde weerdata staan geen sluiting toe. Na eerder gemelde regen
blijven de screens dan omhoog. Instelbaar via centrale **Zonwering**; daar kun je
ook een aparte `weather`- of `binary_sensor`-regenbron kiezen. De screensensor
verklaart de regenstatus en droogtijd. Pirate Weather kan een lokale bui missen
of vertraagd melden; een fysieke regensensor reageert op de locatie zelf.

**Windbeveiliging** gebruikt eveneens de centrale weerentiteit. De actuele melding
`windy`/`windy-variant` haalt screens omhoog. Aparte fabrikantgrenzen voor
windsnelheid en windstoten kun je in **Zonwering** toevoegen in km/u; er worden
geen grenswaarden aangenomen. Zonder bekende grenzen werkt alleen de windmelding
van de weerprovider. Na een alarm wacht Zonentra **30 minuten zonder windmelding**;
met ingestelde grenzen moeten de beoordeelde waarden ook onder 80% van hun grens
blijven. Regen en wind moeten beide vrijgeven voordat de zonregeling weer sluit.
Onbruikbare of verouderde weerdata geven een actief alarm niet vrij. De screensensor
toont windstatus, werkwijze, actuele waarden en grenzen. Pirate Weather kan lokale
windstoten missen; deze aanvullende regeling geeft geen garantie voor de windbelasting
van een specifieke screen.

Laat geen andere regeling dezelfde apparaten tegelijk aansturen.

Koppel elke binnenunit aan één ruimte en aan de werkelijke buitenunitgroep.
Controleer minimale aan-/uittijden en omschakelvertragingen voor je apparatuur;
Home Assistant-states bevestigen geen fysieke compressorstand.

## Instellingen en bediening

Via **Configureren** bij de centrale configuratie stel je aanwezigheid,
ruimteregeling, zonwering, weersverwachting, afspraakvoorbereiding en groepen in.
Via **Configureren** bij een ruimte kies je een afzonderlijk onderwerp: Algemeen,
Sensoren en aanwezigheid, Airco’s en lamellen, Regelparameters, Vloerverwarming,
Zonwering of Agenda en afspraakvoorbereiding. **Toepassen en terug**
bewaart een onderwerp in het concept en opent weer het menu; **Opslaan** bewaart
alle wijzigingen in één keer. Sluiten verwerpt het concept.
Ruimtes nemen standaard centrale instellingen over en kunnen daarvan afwijken.
Lokale regelparameters, vloerverwarmingsgrenzen en voorbereidingswaarden worden
alleen getoond als de betreffende centrale instelling uitstaat. Eerdere lokale
waarden blijven bewaard. Na het uitschakelen van overnemen toont **Toepassen en
terug** eerst de lokale velden; pas die toe om terug te keren naar het menu.
De maximale meetleeftijd verschijnt alleen bij `update_based`; bij `state_based`
blijft een eerder ingestelde leeftijd bewaard zonder te worden toegepast.
Na het kiezen van `update_based` verschijnt eerst het veld voor de meetleeftijd.
Het interval voor setpoint- of ventilatorstandwijzigingen geldt per airco voor
beide regelingen. Handmatige bediening van een gekoppelde airco kan de automatische
aansturing tijdelijk pauzeren; dit staat los van tijdelijke temperatuurdoelen op
de Zonentra-thermostaat.
**Gebruik centrale instellingen voor afspraakvoorbereiding** staat standaard aan,
ook als er al lokale voorbereidingswaarden zijn opgeslagen. Alleen een expliciete
keuze voor uit schakelt over naar die lokale waarden.
Comfortgrenzen stel je in op het centrale apparaat of het ruimteapparaat.
Tijdens een tijdelijke handmatige temperatuurafwijking worden de uitstelmarges
van de weersverwachting niet toegepast. Na afloop gelden die weer; sensorvalidatie,
raamdetectie en schakeltijden blijven steeds gelden. De eigen automatische
ventilator gebruikt rond elke snelheidsgrens 0,1 °C hysterese (bij kleine stappen
maximaal een kwart van de stap), zodat kleine meetwisselingen geen standwissel geven.
Een geldige, uitsluitend verwarmende of koelende vraag wordt door de groep gevolgd,
ook bij een kleine of nul-score tijdens het afmaken van de kamerhysterese.
De minimale vraagscore en verschilgrens kiezen alleen tussen tegengestelde vragen.
Wachttijden, apparaatbeschikbaarheid en beveiligingen blijven gelden.

| Functie | Gebruik |
|---|---|
| Thermostaat | Per ruimte temperatuur en modus kiezen; zonder ventilatorsnelheidsbediening |
| Aanwezigheid | Gebouwaanwezigheid en ruimtebeweging kiezen comfort, eco of afwezig |
| Raamcontact | Een open raam stopt ruimteregeling na de ingestelde vertraging |
| Ventilator | Automatisch bij ingeschakelde ruimteregeling; per ruimte uit te zetten |
| Lamellen | Optionele entiteit met standen voor verwarmen en koelen |
| Zonwering | Koppel covers met positie en gevel; stel centraal zonvermogen, zonstand en eventueel een blokkering in |
| Weersverwachting | Koppel centraal een weerentiteit met uurverwachtingen |
| Openingstijden | Selecteer centraal onder **Aanwezigheid** een schedule-helper; gewone ruimtes bereiden zich automatisch voor vóór opening |
| Ruimteagenda | Koppel per ruimte een calendar-entiteit voor adaptieve comfortvoorbereiding |

Standaard is comfort 21–24 °C, eco 19–26 °C en afwezig 16–30 °C.
Handmatige bediening krijgt standaard 30 minuten voorrang. Automatische
ventilatorregeling volgt daarna weer; de schakelaar **Automatische ventilator**
laat de ventilatorkeuze volledig aan de gebruiker wanneer hij uit staat. Bekende
snelheidsnamen en numerieke standen worden van laag naar hoog geordend; onbekende
fabrikantstanden behouden hun volgorde. De thermostaat gebruikt gemelde activiteit
zoals `idle` waar beschikbaar; `activity_source` maakt terugval op de gekozen modus zichtbaar.

Onder **Aanwezigheid** kun je daarnaast een afzonderlijke **Werkdag-sensor voor
vandaag** kiezen (offset 0). Het schema bepaalt de tijden; de sensor bepaalt of
vandaag een werkdag is. Bij `off`, ontbrekende of onbetrouwbare sensorinformatie
vervallen de kantoorvoorbereiding en aankomstmarge. Werkelijke aanwezigheid en
agenda-afspraken blijven werken. Zonder werkdag-sensor verandert er niets.
Een opening op een volgende dag wordt met deze sensor pas vanaf lokale middernacht
beoordeeld: de waarde voor vandaag voorspelt geen feestdagen op andere datums.
De sensor **Klimaatvraag** toont `not_workday`, `workday_unknown` of
`waiting_for_workday` als reden voor het overslaan of uitstellen.

Het centrale openingstijdenschema geldt voor alle ruimtes zonder agenda; er is
geen instelling per ruimte voor het schema. De voorbereiding gebruikt temperatuurverschil,
buitentemperatuur en geleerde opwarm-/afkoeltempo's. Het schema meldt geen aanwezigheid.
Na voorbereiding geldt bij opening maximaal 15 minuten aankomstmarge zolang het
gebouw afwezig is. Daarna bepalen aanwezigheid en beweging weer de temperatuur;
ook buiten openingstijden kan werkelijke aanwezigheid comfort geven. Bij een
onbruikbaar schema wordt geen voorbereiding gevraagd. Eco/Afwezig en uitgeschakelde
regeling blijven leidend. De sensor **Klimaatvraag** toont status en starttijd.

Agenda's gebruiken de comfortband en geleerde opwarm-/afkoeltempo's per ruimte.
Stel marge en maximale voorbereiding centraal in via **Afspraakvoorbereiding**;
ruimtes kunnen eigen waarden gebruiken. Een agenda schakelt een uitgeschakelde
thermostaat, ruimteregeling of groepsstop niet in. Voorbereidende en lopende afspraken
krijgen bij tegengestelde groepsvragen een oplopende prioriteit van maximaal 2×.
Geleerde tempos vereisen minstens vier recente, voldoende consistente meetintervallen;
ouder dan 30 dagen of zonder tijdstempel gebruikt de planning de beginschatting.
Bij afspraakstart wordt gecontroleerd of de comfortband is bereikt. Een geldige
mislukking voegt 5 minuten marge toe, tot 30 minuten extra per buitenklasse en
verwarmings-/koelrichting; succes bouwt deze met 1 minuut af. Onderbroken voorbereiding
en oude metingen passen de marge niet aan. De maximale voorbereiding blijft gelden.

Zonwering bewaart de laatste beweging en eindtijd van handmatige bedieningspauzes
in lokale HA-opslag. Die blijven ook na een volledige HA-herstart gelden. Een nog
niet bevestigde stabiele doelstand begint na een herstart opnieuw te tellen.

## Bijwerken

Werk bij via HACS, herstart Home Assistant en herlaad de browser.

- **HomeKit:** Zonentra-thermostaten bieden geen ventilatorsnelheidsbediening meer
  aan. Na bijwerken en herstart kun je bestaande thermostaten eenmalig met
  `homekit.reset_accessory` resetten om een eerder aangemaakte ventilatorbediening
  te verwijderen. De automatische ventilatorregeling en de schakelaar daarvoor
  blijven beschikbaar in Home Assistant.
- **Vóór 0.7:** houd rekening met de standaard ingeschakelde actieve standregeling;
  zet bestaande automations voor de groepsmodus uit voordat Zonentra die overneemt.
- **Vóór 0.6:** een oud enkel configuratie-item blijft alleen simuleren. Noteer de
  instellingen, vervang het door een centrale configuratie met aparte ruimtes en
  pas verwijzingen naar gewijzigde entity-ID's aan.

## Builds en versies

Iedere push en pull request doorloopt **Build and release** in GitHub Actions:
versiecontrole, Ruff, tests en een downloadbaar `zonentra.zip` met buildinformatie.
Alleen een geslaagde build van de actuele `main` publiceert automatisch een nieuwe
patchrelease, bijvoorbeeld `0.13.6 → 0.13.7`. Ook wijzigingen aan alleen documentatie
of tests krijgen daarmee een patchrelease. Oudere builds worden overgeslagen als
`main` inmiddels verder is.

De releasejob verhoogt `manifest.json` en `pyproject.toml` samen, maakt een
releasecommit en een tag met exact hetzelfde nummer, en publiceert daarna een
GitHub-release met gegenereerde release notes, ZIP en commit-/buildinformatie.
HACS gebruikt de code van die releasetag; de ZIP is daarnaast als download beschikbaar.
Mislukte tests publiceren niets. Een opnieuw uitgevoerde job hergebruikt zijn eigen
versie als de releasecommit al was gepusht, zodat een mislukte upload geen extra
versienummer kost. De standaard `GITHUB_TOKEN` voorkomt een buildlus door de
releasecommit.

Voor een bewuste minor- of majorrelease kies je **Actions → Build and release →
Run workflow**, branch `main`, en `minor` of `major`. Een gewone push gebruikt altijd
`patch`. De workflow heeft voor de releasejob schrijfrechten op `main` en tags nodig;
bestaande branchbeveiliging wordt niet omzeild. Haal vóór je volgende push de
releasecommit op (`git pull --ff-only` als je lokaal nog geen nieuwe commits hebt).

Lokaal controleren en bouwen kan met `python scripts/release.py check` en
`python scripts/release.py build`. De artifactnaam bevat het GitHub-buildnummer;
`build-info.json` koppelt het pakket aan versie, commit en workflowrun. Alleen een
gepubliceerde release verandert de officiële HACS-versie.

## Diagnostiek en privacy

De sensoren tonen klimaatvraag, plannen, blokkades en commandobevestigingen.
Download diagnostiek via het integratiemenu; namen, identifiers en bronkoppelingen
worden geredigeerd. Zowel centraal als per ruimte zijn zonweringsbesluiten,
wachttijden en recente commando’s zichtbaar. Voor agendaruimtes worden ook planning,
leerkwaliteit, prioriteit en het laatste voorbereidingsresultaat opgenomen.
Controleer exports en logs altijd voordat je ze openbaar deelt.
Configuratie en geleerde gegevens blijven in je eigen Home Assistant-installatie.

Commit geen Home Assistant-configuratie, agenda-exports, diagnostiek, logs,
inloggegevens of lokale editor-/agentinstellingen. De `.gitignore` sluit gangbare
lokale bestanden uit; gebruik uitsluitend fictieve gegevens in voorbeelden en tests.

## Ontwikkelen

Gebruik Python **3.14.2** of nieuwer:

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

De repository bevat de integratie, regressietests en CI. Zie
[ALGORITHM.md](ALGORITHM.md) voor beslisregels en
[ARCHITECTURE.md](ARCHITECTURE.md) voor de technische opbouw.
Licentie: [MIT](LICENSE).
