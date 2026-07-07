# Free Tide Prediction APIs for the Mediterranean

## Summary
Several tide-data services exist online, but most free options with true API access are built around U.S. NOAA data and explicitly do not cover the Mediterranean Sea. Among the sources reviewed, WorldTides, Openwaters.io, and Stormglass.io are the ones that claim global or Mediterranean-relevant coverage with free or free-to-start API access, though the level of detail confirming actual Mediterranean station data varies. TideTime.org offers Mediterranean-region tide info (e.g., Venice) but only as a website, not as an API.

## NOAA-Based Options: Not Mediterranean-Compatible
NOAA's Tides and Currents service and its CO-OPS Data Retrieval API are free and well-documented, offering high/low tide predictions, water levels, currents, and meteorological data for U.S. coastal areas [2][4]. These APIs are part of the PORTS® system and pull from over 200 active U.S. stations [3][4]. However, the notes explicitly state that NOAA's tide APIs do not cover the Mediterranean Sea [2][4]. A related open-source project, the WatermanAPI (GitHub: mkozub/noaa), wraps NOAA's NDFD and CO-OPS data into simple, key-free REST endpoints (`/current`, `/24hr`, `/extended`) for real-time and forecast marine conditions, but it too is limited to U.S. coastal waters and does not provide Mediterranean predictions [3].

## Services Claiming Mediterranean Coverage

### WorldTides.info
WorldTides.info states it provides free tide prediction APIs covering the Mediterranean [5]. Its API returns tide heights, extremes, datums, time zones, and forecast graphics in a single call, and aggregates data from multiple authoritative sources, including the Center for Operational Oceanographic Products and Services, British Oceanographic Data Centre, Australian Bureau of Meteorology, University of Hawaii, Puertos del Estado, Land Information New Zealand, AVISO, and the Canadian Hydrographic Service [5]. It uses harmonic analysis and satellite data to improve accuracy and is described as production-ready for mapping, routing, and planning applications [5]. The notes do not specify exact Mediterranean station counts or list specific Mediterranean locations.

### Openwaters.io
Openwaters.io also claims free tide prediction API coverage for the Mediterranean, powered by the open-source Neaps prediction engine and open data [6]. It provides REST access to tide predictions derived from harmonic constituents and station metadata, with documentation available for developers [6]. However, the notes explicitly caution that there is no specific mention of Mediterranean stations or data availability confirmed in the source text [6], so Mediterranean coverage here is asserted but not independently verified in the notes.

### Stormglass.io
Stormglass.io offers a free-to-sign-up Global Tide API with thousands of tide stations worldwide, automatically selecting the nearest station to given coordinates [7]. It distinguishes astronomical tide (predictable) from meteorological tide (less predictable) and provides two endpoints: `sea-level` (hourly predictions up to 10 days) and `extremes` [7]. Data is sourced partly from NOAA and other providers, uses UTC time and configurable datums (MSL or MLLW), and includes station metadata such as distance and operator [7]. The notes do not explicitly confirm Mediterranean station density, but the "thousands of tide stations worldwide" and multi-source model imply broader-than-U.S. coverage [7].

## Non-API Reference Option
TideTime.org is a free website (not an API) covering 7,000+ locations globally, including Europe and the Mediterranean (e.g., Venice) [1]. It provides high/low tide times, current tidal conditions, and moon phases, updated daily, for planning coastal activities such as fishing and beach visits [1]. The site explicitly states its predictions are not suitable for navigation [1], and there is no indication in the notes that it offers a programmatic API.

## Open Questions
- The notes do not confirm precise Mediterranean station counts or specific Mediterranean cities/ports covered by WorldTides, Openwaters.io, or Stormglass.io.
- It is unclear whether Openwaters.io's Neaps-based database includes verified Mediterranean harmonic constituents, since the source notes explicitly flag the absence of such confirmation [6].
- Pricing tiers, rate limits, or free-usage caps for WorldTides.info and Stormglass.io beyond "free to sign up" are not detailed in the notes.

## Sources
[1] https://www.tidetime.org/
[2] https://tidesandcurrents.noaa.gov/
[3] https://github.com/mkozub/noaa
[4] https://api.tidesandcurrents.noaa.gov/api/prod/
[5] https://www.worldtides.info/
[6] https://openwaters.io/tides/
[7] https://stormglass.io/our-tide-api-better-than-ever/