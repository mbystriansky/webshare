# Webshare.cz — Kodi addon

Kodi video plugin (`plugin.video.webshare`), ktorý spája katalóg
[TMDB](https://www.themoviedb.org/) s prehrávaním súborov z
[Webshare.cz](https://webshare.cz/). Trending, novinky v kinách, žánre,
roky, vyhľadávanie filmov aj seriálov po epizódach, s metadátami, obsadením
a triedením nájdených súborov podľa kvality (rozlíšenie, HDR, CZ/SK dabing).

## Požiadavky

- **Kodi 20 (Nexus)** alebo novšie
- **Webshare.cz účet** — na prehrávanie je prakticky nutný VIP
- **TMDB API kľúč** — zadarmo po registrácii na
  [themoviedb.org](https://www.themoviedb.org/settings/api)

## Inštalácia

### Z repozitára (odporúčané — automatické aktualizácie)

1. V Kodi: *Add-ons → Install from zip file* a zadajte URL
   `https://mbystriansky.github.io/webshare/repository.mbystriansky/repository.mbystriansky-1.0.1.zip`
2. Potom *Install from repository → Webshare repozitár → Video add-ons →
   Webshare.cz*

### Manuálne

Stiahnite ZIP z [releases](https://github.com/mbystriansky/webshare/releases)
alebo naklonujte repo do `addons/plugin.video.webshare` v profile Kodi.

## Konfigurácia

V nastaveniach doplnku:

| Nastavenie | Popis |
|---|---|
| Používateľské meno / Heslo | Webshare.cz prihlasovacie údaje |
| TMDB API kľúč | v3 API kľúč z themoviedb.org (dá sa uložiť aj do súboru `tmdb_api_key.txt` v profile doplnku alebo `/sdcard/tmdb_api_key.txt` na Androide) |
| Jazyk metadát | sk-SK / cs-CZ / en-US, s automatickým dopĺňaním chýbajúcich popisov z náhradných jazykov |
| Typ streamu | Stream (rýchly štart) alebo originálny súbor (plná kvalita) |
| Región | kód krajiny pre „Novinky v kinách" |

## Vývoj

Bez build systému — repo sa priamo nasadí ako addon (symlink do `addons/`
stačí). Architektúra je popísaná v `CLAUDE.md`; release proces beží cez
GitHub Actions (`.github/workflows/publish.yml`) na GitHub Pages.

## Licencia

[MIT](LICENSE). Doplnok nie je pridružený k Webshare.cz ani TMDB.
