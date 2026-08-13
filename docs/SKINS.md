# Viewer skins

A skin is one JSON object that restyles the match viewer — palette, type,
silhouette scale, motion — without touching anything the viewer *does*.
Nothing in the sim, the POV/fog rules, hit-testing, or the replay/live
pipeline reads the skin; a malformed skin falls back to the default, and the
default reproduces the stock look exactly.

## Applying a skin

In priority order:

1. `window.FLOTILLA_SKIN = {...}` — set before the viewer script runs
   (exported bundles: inject a `<script>` above the viewer's).
2. `player.html?skin=<name>` — a built-in name (`flotilla`, `daylight`) or
   any skin imported on the dashboard (fetched from the server).
3. `player.html?skinurl=<path>` — same-origin JSON only (a foreign URL is
   ignored, same rule as replay sources).
4. The 🎨 picker in the viewer's settings tab (persisted per browser) —
   built-ins plus every imported skin.

Works identically on live views and replays — the skin layer sits above the
renderer, not inside it.

## Tokens

Every field is optional; omitted fields keep the default. Colors: 6-digit
hex keeps the renderer's baked-in translucency nuances (any CSS color works,
but only hex gets those alphas).

| section | field | what it paints |
|---|---|---|
| `ui` | `bg` `card` `line` `ink` `dim` `accent` | the DOM chrome (panels, cards, buttons, text) via CSS variables |
| `ui` | `font` | font-family for the whole page ("" = the stock stack) |
| `fleet` | `[8 colors]` | the fleet palette — ships, trails, charts, names, everywhere a fleet is colored |
| `sea` | `top` `bottom` | the water gradient |
| `sea` | `gridMinor` `gridMajor` `label` | coordinate grid + on-map name labels |
| `land` | `fill` `halo` | islands |
| `nodes` | `fish` `wreck` `label` | resource shoals, wrecks, their labels |
| `ships` | `hullInk` | dark interior details (wheelhouses, outlines against bright water) |
| `ships` | `detail` | bright details (gun studs, armor rings, selection) |
| `ships` | `laden` | the cargo-aboard marker |
| `ships` | `ghost` | stale contact silhouettes |
| `ships` | `scale` | silhouette size multiplier (0.25–4; hit-testing is position-based, so purely visual) |
| `fx` | `fog` | the POV fog wash |
| `fx` | `tail` | wake/trail length in frames (0–60) |
| `minimap` | `bg` `frame` `cursor` | the overview map |

## The dashboard importer

The Server tab's **🎨 Viewer skins** section is the working surface for a
design pass — no file drops or code needed:

- **Import**: paste skin JSON (or load a `.json` file), name it, Import.
  The server stores it under the library's `skins/` directory and serves it
  at `/skins/<name>.json`; a typo'd section name comes back as a warning
  instead of silently painting nothing.
- **Preview**: a side-by-side canvas (stock vs yours) re-renders as you
  type — a sketch of the scene, for fast iteration. The fidelity check is
  the **▶ viewer** button, which opens the real renderer on the newest
  finished replay with your skin applied.
- **Share / prefer**: *copy link* gives `player.html?skin=<name>` for
  anyone on the server; *use in my viewer* makes it your own browser's
  default (the viewer's 🎨 picker lists every imported skin too, so each
  person keeps their own preference).
- Skin names are 1–24 letters/digits/`-`/`_`; `flotilla` and `daylight` are
  reserved for the built-ins. Deleting a skin falls anyone still pointing
  at it back to the stock look.

API, for scripting: `GET /api/skins` lists, `POST /api/skins`
`{"name","skin"}` saves (`{"name","delete":true}` removes),
`GET /skins/<name>.json` serves one.

## A brand pass, concretely

Start from the `daylight` entry in `viewer/index.html` (the `SKINS`
registry) — it exercises every token. For an external team: author the JSON
against the table above, iterate in the dashboard importer (or with
`player.html?replay=...&skinurl=your-skin.json` if you'd rather work from
files), and when it's final either leave it imported, promote it into the
registry, or ship it as a `window.FLOTILLA_SKIN` injection in an exported
bundle. Fleet colors need clear pairwise contrast against the sea and each
other — eight admirals can be on screen at once.
