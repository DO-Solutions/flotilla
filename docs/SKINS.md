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
| `ui` | `font` | font-family for the whole page ("" = the stock stack). A skin can only NAME a face the page already loads — see "Fonts" below |
| `ui` | `radius` | corner rounding on cards and panels. A bare number means px (clamped 0–64); a string may carry its own unit (`"6px"`, `"0.5rem"`, `"50%"`). Appearance only — no layout moves |
| `fleet` | `[8 colors]` | the fleet palette — ships, trails, charts, names, everywhere a fleet is colored |
| `sea` | `top` `bottom` | the water gradient |
| `sea` | `gridMinor` `gridMajor` `label` | coordinate grid + on-map name labels |
| `sea` | `texture` | a `data:image` URI tiled over the gradient (put the wash's alpha in the tile itself) |
| `sea` | `decor` | sprites scattered on open water (waves, gulls, whales) — same shape as `land.decor`, density capped at 0.1, seeded per replay |
| `land` | `fill` `halo` | islands |
| `land` | `texture` | a `data:image` URI tiling the island fill (the reef halo keeps its color) |
| `land` | `tileset` | blob16 autotile sheet — real coastlined islands from 16 tiles (see "Island dressing") |
| `land` | `decor` | sprites scattered on island interiors, seeded per replay (see "Island dressing") |
| `land` | `coast` | procedural smoothed coastline, zero assets (see "Island dressing") |
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

## Island dressing

Three ways to make islands read as islands, in precedence order —
`tileset` wins over `coast`, which wins over the plain `texture`/`fill`
cells. All of it renders once into a cached layer (islands are static), so
none of it costs anything per frame, and any malformed input falls back to
the plain cell fill. Ships path on the cell grid regardless — nothing here
changes where anything can sail.

**Auto-tiling** (`land.tileset`) — the standard game technique: the artist
draws one sheet of 16 tiles and the renderer picks per cell by which
neighbors are also land.

```json
{ "land": { "tileset": { "src": "data:image/png;base64,...", "layout": "blob16" } } }
```

The sheet is a **4×4 grid of equal square tiles**, indexed by the
land-neighbor bitmask **N=1 · E=2 · S=4 · W=8**, row-major: tile index =
mask, column = `index % 4`, row = `index ÷ 4`. So tile 0 (top-left) is a
lone island cell (water on all four sides), tile 15 (bottom-right) is pure
interior, tile 3 (N+E land) is a southwest shoreline corner, and so on.
Draw beaches on each tile's water-facing sides and the coastline assembles
itself. Any tile size works (32–64 px reads well); diagonals aren't
distinguished (that's the 47-tile variant — not supported yet).

**Scatter decoration** (`land.decor`) — up to 8 sprites sprinkled on
*interior* cells (single-cell islets stay bare):

```json
{ "land": { "decor": { "sprites": ["data:image/png;base64,..."], "density": 0.2 } } }
```

Placement is deterministic from the replay seed, so every viewer of the
same game sees the same palm in the same place. `density` is the fraction
of interior cells decorated (0–0.5).

**Diagonal smoothing** (always on for tiles and plain cells) — a water cell
whose two perpendicular land neighbors share a land diagonal gets a
quarter-round land fillet in that corner, so staircase coastlines bleed
gently into the sea instead of stepping. The fillet never exceeds half a
cell — the drawn shore stays honest about where ships can sail. (`coast`
mode achieves the same rounding through its curve smoothing.)

**Procedural coastline** (`land.coast`) — organic islands with **no assets
at all**: the blocky outline is traced and corner-smoothed, filled with
`land.texture`/`fill`, ringed with a sand edge over a shallow-water halo.

```json
{ "land": { "coast": { "sand": "#d9c489", "shallow": "#aacfe855", "smooth": 2 } } }
```

`coast: true` uses sensible defaults; `smooth` is 0–3 rounding passes (the
smoothed shore always stays within half a cell of the true grid, so the
picture never lies about where ships can sail).

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

## Fonts

`ui.font` is a plain CSS `font-family` value, so it can only name a face the
page **already loads**. Naming a font nobody shipped silently falls through to
the next entry in the stack — which is why every `ui.font` should end in a real
fallback (`… , 'Helvetica Neue', Arial, sans-serif`).

The pages ship the webfaces in `assets/fonts/`, declared with `@font-face` in
`viewer/index.html` and both `dash/*.html`, and served from `/fonts/…`
(`ROUTES_STATIC`). Today that is **Special Gothic** 400 + 500, which the
`kraken` skin names. To add another face: drop the `woff2` in `assets/fonts/`,
add its `ROUTES_STATIC` entries, add an `@font-face` block to those three
pages, and include the license text alongside the files if it needs one.

Off-origin — an exported bundle, a public showcase mirror — those URLs 404 and
`font-display: swap` simply keeps the fallback. A skin never *breaks* off
origin; it just renders in the next font down.

## Skinning the site chrome

The dashboard and tournament pages are not the viewer: no registry, no canvas,
no `?skin=`. They ask `GET /api/site-skin` for **one fully-resolved `ui`
block** and paint the same CSS variables. Set it in the Server tab (🎨 Viewer
skins → 🖌 site chrome), or:

```
curl -X POST -d '{"name":"kraken"}' http://<host>/api/site-skin   # "" = stock
```

Only the `ui` section is used — the sea, ships and land tokens have nothing to
paint outside the viewer. With no site skin set the pages keep their own
stylesheet untouched, so the stock look is unchanged.

Because the built-in `ui` blocks now exist on both sides of a language
boundary (`viewer/index.html`'s `SKINS` and `server.py`'s `SKIN_BUILTIN_UI`),
`tests/test_skins.py` reads the real registry out of the viewer and fails if
the two ever drift. Change both, or the suite will tell you.
