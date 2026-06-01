# TWF Beschilderung – Signpost Photo Map

Interactive OpenStreetMap showing geotagged photos and videos of trail signposts,
used by the club to discuss and plan signpost maintenance.

## Quick Start

```bash
# Add new HEIC/MOV files to this folder, then:
python3 generate_map.py

# Open the result:
xdg-open docs/index.html   # Linux
open docs/index.html        # macOS
```

Re-running is safe — already-processed images are skipped, only new ones are converted.
The map HTML is always fully regenerated.

---

## Deployment

**Live URL:** https://malsieb.github.io/twf-beschilderung/

Hosted on GitHub Pages (account: `malsieb`, repo: `twf-beschilderung`, public).
The `docs/` folder is its own git repo, deployed from the `main` branch root.

### Publishing updates

```bash
cd "/home/malte/Dropbox/TWF Beschilderung"
python3 generate_map.py
git add -A && git commit -m "Update map" && git push
```

GitHub Pages rebuilds automatically on push — changes are live within ~1 minute.

### Re-creating Pages from scratch (if needed)

```bash
gh api repos/malsieb/twf-beschilderung/pages \
  --method POST \
  --field "source[branch]=main" \
  --field "source[path]=/"
```

Equivalent to: GitHub repo → Settings → Pages → Deploy from branch → main / (root).

---

## Folder Structure

```
TWF Beschilderung/
├── IMG_XXXX.HEIC          # Source photos (iPhone, geotagged)
├── IMG_XXXX.MOV           # Source videos (iPhone, geotagged)
├── generate_map.py        # Main script – processes media and builds the map
├── gps_overrides.json     # Manual GPS corrections (applied at generation time)
├── README.md              # This file
└── docs/
    ├── index.html           # The interactive map (open this in a browser)
    ├── thumbs/            # 300px-wide JPEG thumbnails (for map markers)
    └── full/              # 1400px-wide JPEG images (for lightbox)
```

## Dependencies

```bash
pip install pillow-heif pillow piexif
# ffmpeg + ffprobe must be installed system-wide (for MOV frame extraction)
```

- **pillow-heif** – opens HEIC files with PIL
- **piexif** – reads and writes raw EXIF bytes (used for GPS patching)
- **ffmpeg / ffprobe** – extracts first frame and GPS from MOV files

---

## Map Features

### Tile Layers (layer control, top-right)
| Layer | Notes |
|---|---|
| CyclOSM *(default)* | Best for forest paths and trails |
| Satellit | Esri World Imagery – good for ground-truth |
| Esri Topo | Terrain + roads |
| CartoDB Voyager | Clean general-purpose basemap |
| OpenTopoMap | Topographic, hiking-focused |

All tile providers work from `file://` (no Referer restrictions).

### Overlays
- **Planquadrat-Raster** *(on by default)* – letter+number grid (A1, B2 …) matching the
  photo extent. Useful for discussing locations: "the broken sign is in C3".
- **Wanderwege-Overlay** – Waymarked Trails hiking routes.

### Markers
- Each photo/video is a clickable thumbnail marker on the map.
- **Border colour** indicates GPS accuracy:
  - White – accuracy < 30 m (reliable)
  - Orange – accuracy 30–99 m (use with caution)
  - Red – accuracy ≥ 100 m (position likely wrong)
- Nearby markers are grouped into **stacked cluster icons** (up to 3 thumbnails fanned
  out). Clicking a cluster opens the lightbox for all its members directly — no zoom.

### Lightbox
- Click any marker or cluster to open a lightbox.
- Shows only photos taken **within 20 m** of the clicked one (or all cluster siblings,
  whichever set is larger).
- Caption format: `IMG_1234.HEIC  ·  C3  ·  ±12m  (2/4)`
- Navigation: arrow buttons, keyboard ←/→, or swipe (mobile).
- Videos play inline; pauses automatically when closed or navigated away.
- Spinner shown while a new image loads (no stale-image flash).

### Grid Reference System
- Fixed letter+number grid over the photo extent (~400 m cells).
- Cell reference for each photo is computed at generation time and shown in the
  lightbox caption and in `markers[i].grid` in the JS data.
- Grid is based on the bounding box of all markers with 15 % padding.

---

## GPS Corrections Workflow

### Option A – Override without touching source files
Edit `gps_overrides.json`:
```json
{
  "IMG_1234.HEIC": {"lat": 49.12345, "lon": 10.12345, "acc": 10.0}
}
```
Then re-run `generate_map.py`. Overrides are applied after EXIF is read,
so the source file is untouched.

### Option B – Patch the HEIC file directly (permanent fix)
Use the helper below. It tries an **in-place byte patch** first (no re-encoding);
falls back to re-encoding at quality≈65 only if the EXIF size changes.

```python
import piexif, pillow_heif
from PIL import Image
from pathlib import Path
pillow_heif.register_heif_opener()

def decimal_to_dms(val):
    d = int(val)
    m = int((val - d) * 60)
    s = round((val - d - m/60) * 3600 * 10000)
    return ((d, 1), (m, 1), (s, 10000))

def patch_heic_gps(path, lat, lon, acc):
    img = Image.open(path)
    orig_exif = img.info['exif']
    ed = piexif.load(orig_exif)
    ed['GPS'][1] = b'N' if lat >= 0 else b'S'
    ed['GPS'][2] = decimal_to_dms(abs(lat))
    ed['GPS'][3] = b'E' if lon >= 0 else b'W'
    ed['GPS'][4] = decimal_to_dms(abs(lon))
    ed['GPS'][31] = (round(acc * 100), 100)  # GPSHPositioningError in metres
    new_exif = piexif.dump(ed)
    raw = Path(path).read_bytes()
    if orig_exif in raw and len(new_exif) == len(orig_exif):
        Path(path).write_bytes(raw.replace(orig_exif, new_exif, 1))
    else:
        img.save(path, format='HEIF', exif=new_exif, quality=65)

patch_heic_gps(Path('IMG_1234.HEIC'), lat=49.12345, lon=10.12345, acc=10.0)
```

After patching, delete the cached `docs/thumbs/IMG_XXXX.jpg` and
`docs/full/IMG_XXXX.jpg` only if the image content changed (re-encoded).
GPS-only fixes don't require regenerating the JPEG cache.

### Accuracy threshold for white border
`acc < 30 m` → white border. Set `acc=10.0` or `acc=12.0` when manually
correcting a position you are confident about.

### Interpolating bad GPS from neighbours
For photos with GPS errors caused by poor satellite reception (dense forest cover),
use timestamps and coordinates of the surrounding photos to interpolate:

```python
# Use GPS UTC timestamps (tag 7+29 in GPS IFD) for HEIC,
# and ffprobe's creation_time (UTC) for MOV.
# frac = (t_bad - t_prev) / (t_next - t_prev)
# lat = lat_prev + frac * (lat_next - lat_prev)  — same for lon
```

See session history: this approach was used to fix IMG_1297, IMG_1302, IMG_1310.

### Offset by metres
```python
import math
def offset(lat, lon, north_m, east_m):
    dlat = north_m / 111320
    dlon = east_m / (111320 * math.cos(math.radians(lat)))
    return round(lat + dlat, 7), round(lon + dlon, 7)
```

---

## Current Status (as of 2026-06-01)

- **93 markers** total: 88 HEIC photos + 5 MOV videos
- **0 orange, 0 red** accuracy markers – all positions verified or corrected
- GPS corrections applied (permanently patched into source HEIC files):
  - IMG_1230, IMG_1231 → relocated to IMG_1270's position
  - IMG_1297 → interpolated from IMG_1296 / IMG_1298
  - IMG_1299 → relocated to IMG_1300's position
  - IMG_1301 → relocated to IMG_1313's position
  - IMG_1302 → interpolated from IMG_1301 / IMG_1303
  - IMG_1303 → shifted 20 m west, 5 m south
  - IMG_1304 → relocated to IMG_1303's position
  - IMG_1305 → accuracy corrected (position was fine)
  - IMG_1308 → relocated to IMG_1309's position
  - IMG_1310 → interpolated from IMG_1309 / IMG_1311
  - IMG_1311 → shifted 5 m south
  - IMG_1312 → relocated to IMG_1313's position
  - IMG_1320 → relocated to IMG_1314's position

---

## Notes for Future Agents

- **Do not re-run `generate_map.py` to "check" the map** – open `docs/index.html` directly.
- The `docs/thumbs/` and `docs/full/` caches persist between runs. Delete a specific
  `IMG_XXXX.jpg` pair if you need to force re-processing of that image.
- MOV files: GPS is extracted from `com.apple.quicktime.location.ISO6709` ffprobe tag.
  The video frame shown in the lightbox is the first frame extracted by ffmpeg.
  MOV files reference `../IMG_XXXX.MOV` (parent directory) for playback.
- The grid (Planquadrat-Raster) recomputes automatically from all marker positions
  each time the map is generated. Cell labels will shift if the photo extent changes
  significantly — regenerate a reference printout after adding many new photos.
- `exiftool` is not installed on this machine. All EXIF manipulation uses `piexif`.
