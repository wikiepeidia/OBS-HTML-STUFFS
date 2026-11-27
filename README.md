# OBS HTML Stuffs

A collection of lightweight OBS Browser Source overlays (clocks, weather, and stream status screens).

## Files & Folders

### Clocks & Weather Overlays
- **Flip Clock** — [klock-clock.html](./klock-clock.html)
- **Simple Clock with Date** — [CLOCK2.html](./CLOCK2.html)
- **Dynamic Clock + Weather (GPT Clock)** — [dynamic clocks/](./dynamic%20clocks/)
  - [GPTCLOCK1.html](./dynamic%20clocks/GPTCLOCK1.html) — Standalone OBS overlay (auto day/night theme, or force via `?theme=day` / `?theme=night`). Works directly in OBS without Flask using keyless Open-Meteo weather (or `?owm_key=...`).
  - [overlay_proxy.py](./dynamic%20clocks/overlay_proxy.py) — Optional local proxy (`http://127.0.0.1:8787`) for live location + OpenWeatherMap updates using keys from `.env`.
  - [address_algorithm.py](./dynamic%20clocks/address_algorithm.py) — Nominatim + LocationIQ Vietnamese address (`Phường` / `Xã` / `Đặc khu`) test & verification script.
  - [app.py](./dynamic%20clocks/app.py) — Optional Flask `/weather` API service.

### Stream Images
- **Stream Status & Offline Screens** — [Stream images/](./Stream%20images/) *(rest / prepare / interval / offline / end screens)*

### Project Config
- [requirements.txt](./requirements.txt) · [.gitignore](./.gitignore) · [LICENSE](./LICENSE)

## Stream Examples

- **The GPT Clock** — [Twitch VOD](https://www.twitch.tv/videos/2781923912)
- **Flip Clock** — [YouTube Stream](https://www.youtube.com/watch?v=6cpdOb3L4gw&t=1281s)
- **Image Overlay / Offline Screen** — [Twitch Channel (wikiepeidia)](https://www.twitch.tv/wikiepeidia)

## Usage

1. **Lightweight OBS mode (no Python/Flask needed):** Add [GPTCLOCK1.html](./dynamic%20clocks/GPTCLOCK1.html), [klock-clock.html](./klock-clock.html), or [CLOCK2.html](./CLOCK2.html) directly as a local **Browser Source** in OBS.
2. **Dynamic location mode (optional):** Create a `.env` file with `LOCATIONIQ_KEY` and `OWM_KEY`, then run:
   ```bash
   python "dynamic clocks/overlay_proxy.py"
   ```
   [GPTCLOCK1.html](./dynamic%20clocks/GPTCLOCK1.html) will automatically detect the local server and switch to live location updates.
