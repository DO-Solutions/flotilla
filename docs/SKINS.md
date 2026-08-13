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
2. `player.html?skin=<name>` — a registry name (`flotilla`, `daylight`).
3. `player.html?skinurl=<path>` — same-origin JSON only (a foreign URL is
   ignored, same rule as replay sources).
4. The 🎨 picker in the viewer's settings tab (persisted per browser).

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

## A brand pass, concretely

Start from the `daylight` entry in `viewer/index.html` (the `SKINS`
registry) — it exercises every token. For an external team: author the JSON
against this table, drop it next to the page, open
`player.html?replay=...&skinurl=your-skin.json`, iterate. When it's final it
can join the registry or ship as a `window.FLOTILLA_SKIN` injection in an
exported bundle. Fleet colors need clear pairwise contrast against the sea
and each other — eight admirals can be on screen at once.
