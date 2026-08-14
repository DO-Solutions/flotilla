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
| `sea` | `texture` | a `data:image` URI tiled over the gradient (put the wash's alpha in the tile itself) |
| `land` | `fill` `halo` | islands |
| `land` | `texture` | a `data:image` URI tiling the island fill (the reef halo keeps its color) |
| `nodes` | `fish` `wreck` `label` | resource shoals, wrecks, their labels |
| `ships` | `hullInk` | dark interior details (wheelhouses, outlines against bright water) |
| `ships` | `detail` | bright details (gun studs, armor rings, selection) |
| `ships` | `laden` | the cargo-aboard marker |
| `ships` | `ghost` | stale contact silhouettes |
| `ships` | `scale` | silhouette size multiplier (0.25–4; hit-testing is position-based, so purely visual) |
| `ships` | `shapes` | custom silhouettes per role — see "Shapes & textures" below |
| `ships` | `sprites` | actual images per role (+ `flagship`) — see "Shapes & textures" below |
| `ships` | `spriteKey` | the team-color key (default `#ff00ff`): sprite pixels in shades of this color render in the fleet color; `""` disables |
| `fx` | `fog` | the POV fog wash |
| `fx` | `tail` | wake/trail length in frames (0–60) |
| `minimap` | `bg` `frame` `cursor` | the overview map |

## Shapes & textures

**Ship shapes.** `ships.shapes` maps a role to a polygon that replaces its
stock silhouette:

```json
{ "ships": { "shapes": {
    "trawler": [[9, 0], [-7, 5], [-4, 0], [-7, -5]],
    "frigate": [[10, 0], [2, 4], [-7, 3], [-7, -3], [2, -4]]
} } }
```

- Author with the **front at +x** — the renderer rotates the shape to the
  ship's heading, so a sideways-authored shape visibly sails sideways.
- Units are the built-ins' canvas pixels: a stock hull spans roughly ±8–10.
  3–64 points per shape, coordinates clamped to ±40; `ships.scale` still
  multiplies on top.
- Valid role keys: `trawler`, `frigate`, `raider`, `scout`, `cutter`, plus
  `custom` for any operator-designed class (or that class's own name). A
  malformed entry is dropped alone; the role keeps its stock look.
- A custom shape is a plain color-fill silhouette — the stock roles' detail
  overlays (net booms, gun studs, masts) are drawn only on the shapes they
  were designed for. Laden/selection markers still apply.

**Ship sprites.** `ships.sprites` maps a role to an actual **image** drawn
as the ship — a photo crop, pixel art, a logo boat:

```json
{ "ships": { "sprites": {
    "trawler": "data:image/png;base64,....",
    "flagship": "data:image/png;base64,...."
} } }
```

- Same role keys as `shapes`, plus **`flagship`** (replaces the anchored
  command square; drawn unrotated) and `custom` for operator classes. A
  sprite wins over a shape for the same role.
- Author the image **pointing right** (front at +x) with a transparent
  background; it's aspect-fit to the stock hull footprint (~20 px long,
  height capped) and `ships.scale` multiplies on top. Roughly 2:1
  landscape crops look best; the source can be any size.
- Same self-contained rule as textures: `data:image` URIs only, ≤200 KB
  each. Until an image decodes — or if it's malformed — the role keeps its
  shape/stock silhouette, so ships are never invisible.
- **Team color via `spriteKey`** — one PNG serves all eight fleets. Paint
  the parts that should carry the team color in shades of the key color
  (default magenta `#ff00ff`): every pixel whose color *proportions* match
  the key is re-rendered in the fleet color at that pixel's own brightness,
  so light magenta becomes light team color and dark magenta a dark shade —
  shading survives the swap. Matching is proportion-based rather than
  exact, which keeps anti-aliased edges from leaving magenta fringes;
  pixels that drifted too far off the key stay untouched. If the artwork
  legitimately uses magenta, point `spriteKey` at another 6-digit hex or
  set it `""` to disable. The flagship sprite is keyed with its fleet's
  color too. (Wakes, charts, and labels stay fleet-colored regardless.)

**Textures.** `sea.texture` and `land.texture` take a **`data:image` URI**
(png/jpeg/webp/gif, base64) that tiles as a repeating pattern — the sea one
over the gradient, the land one as the island fill. Self-contained by rule:
a remote URL is ignored, same as `?skinurl=` (nothing in a skin ever
fetches off-origin). Keep tiles small (a 32–64 px tile reads well; ≤200 KB
per texture, 600 KB per skin). For the sea, bake the overlay's transparency
into the tile — it's drawn on top of the gradient.

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
