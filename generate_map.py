#!/usr/bin/env python3
"""
Process HEIC/MOV files with GPS data → compressed web images + interactive OSM map.
Run from the folder containing the media files:  python3 generate_map.py
Outputs:  docs/index.html, docs/thumbs/*.jpg, docs/full/*.jpg
Re-running is safe: already-converted files are skipped.
"""

import os
import sys
import json
import math
import subprocess
import struct
from pathlib import Path

try:
    import pillow_heif
    from PIL import Image
    pillow_heif.register_heif_opener()
except ImportError:
    sys.exit("Missing dependency: pip install pillow-heif pillow")

BASE = Path(__file__).parent
WEB = BASE / "docs"
THUMBS = WEB / "thumbs"
FULL = WEB / "full"
VIDEOS = WEB / "videos"

THUMB_WIDTH = 300   # px, for map markers
FULL_WIDTH = 1400   # px, for lightbox
JPEG_QUALITY = 82
GPS_TAG = 34853

_overrides_path = BASE / "gps_overrides.json"
GPS_OVERRIDES: dict = {}
if _overrides_path.exists():
    raw = json.loads(_overrides_path.read_text())
    GPS_OVERRIDES = {k: v for k, v in raw.items() if not k.startswith("_")}


def dms_to_decimal(dms, ref):
    d, m, s = dms
    val = float(d) + float(m) / 60 + float(s) / 3600
    if ref in ("S", "W"):
        val = -val
    return round(val, 7)


def get_gps_heic(path: Path):
    img = Image.open(path)
    exif = img.getexif()
    gps = exif.get_ifd(GPS_TAG)
    if not gps or 2 not in gps or 4 not in gps:
        return None, None, None, img
    lat = dms_to_decimal(gps[2], gps.get(1, "N"))
    lon = dms_to_decimal(gps[4], gps.get(3, "E"))
    acc = round(float(gps[31]), 1) if 31 in gps else None
    return lat, lon, acc, img


def get_gps_mov(path: Path):
    """Extract GPS from MOV using ffprobe location metadata."""
    import re
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return None, None
    tags = json.loads(result.stdout).get("format", {}).get("tags", {})
    # Apple QuickTime uses ISO6709 format: +49.4724+010.9311+334.000/
    loc = (tags.get("com.apple.quicktime.location.ISO6709")
           or tags.get("location", ""))
    if loc:
        m = re.match(r'([+-]\d+\.\d+)([+-]\d+\.\d+)', loc)
        if m:
            return float(m.group(1)), float(m.group(2))
    return None, None


def extract_mov_frame(path: Path, out: Path):
    """Extract first usable frame from MOV as JPEG."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path), "-vf", f"scale={FULL_WIDTH}:-2",
         "-frames:v", "1", "-q:v", "3", str(out)],
        capture_output=True
    )


def convert_mov_to_mp4(path: Path, out: Path):
    """Re-encode MOV to web-optimised MP4 (H.264, 1280px wide, faststart)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(path),
         "-vf", "scale=1280:-2",
         "-c:v", "libx264", "-crf", "28", "-preset", "slow",
         "-c:a", "aac", "-b:a", "128k",
         "-movflags", "+faststart",
         str(out)],
        capture_output=True
    )


def make_thumb(img: Image.Image, out: Path):
    ratio = THUMB_WIDTH / img.width
    h = int(img.height * ratio)
    thumb = img.resize((THUMB_WIDTH, h), Image.LANCZOS)
    thumb.save(out, "JPEG", quality=75, optimize=True)


def make_full(img: Image.Image, out: Path):
    if img.width > FULL_WIDTH:
        ratio = FULL_WIDTH / img.width
        img = img.resize((FULL_WIDTH, int(img.height * ratio)), Image.LANCZOS)
    img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)


def process_heic(path: Path):
    stem = path.stem
    thumb_path = THUMBS / f"{stem}.jpg"
    full_path = FULL / f"{stem}.jpg"

    lat, lon, acc, img = get_gps_heic(path)
    if lat is None:
        print(f"  skip (no GPS): {path.name}")
        return None
    if path.name in GPS_OVERRIDES:
        ov = GPS_OVERRIDES[path.name]
        lat, lon = ov.get("lat", lat), ov.get("lon", lon)
        acc = ov.get("acc", acc)
        print(f"  [override applied]", end=" ")

    # Respect EXIF orientation
    try:
        from PIL import ImageOps
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass

    if not thumb_path.exists():
        make_thumb(img, thumb_path)
    if not full_path.exists():
        make_full(img, full_path)

    return {"name": path.name, "lat": lat, "lon": lon, "acc": acc,
            "thumb": f"thumbs/{stem}.jpg", "full": f"full/{stem}.jpg",
            "type": "image"}


def process_mov(path: Path):
    stem = path.stem
    thumb_path = THUMBS / f"{stem}.jpg"
    full_path = FULL / f"{stem}.jpg"
    video_path = VIDEOS / f"{stem}.mp4"

    lat, lon = get_gps_mov(path)
    if lat is None:
        print(f"  skip (no GPS): {path.name}")
        return None

    if not full_path.exists():
        extract_mov_frame(path, full_path)
    if full_path.exists() and not thumb_path.exists():
        img = Image.open(full_path)
        make_thumb(img, thumb_path)
    if not video_path.exists():
        print(f"\n    converting video...", end=" ", flush=True)
        convert_mov_to_mp4(path, video_path)

    return {"name": path.name, "lat": lat, "lon": lon,
            "thumb": f"thumbs/{stem}.jpg", "full": f"full/{stem}.jpg",
            "video": f"videos/{stem}.mp4", "type": "video"}


COLS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def compute_grid(markers):
    lats = [m["lat"] for m in markers]
    lons = [m["lon"] for m in markers]
    min_lat, max_lat = min(lats), max(lats)
    min_lon, max_lon = min(lons), max(lons)

    pad_lat = (max_lat - min_lat) * 0.15 or 0.002
    pad_lon = (max_lon - min_lon) * 0.15 or 0.002
    g_min_lat = min_lat - pad_lat
    g_max_lat = max_lat + pad_lat
    g_min_lon = min_lon - pad_lon
    g_max_lon = max_lon + pad_lon

    mid_lat = (g_min_lat + g_max_lat) / 2
    lat_m = (g_max_lat - g_min_lat) * 111320
    lon_m = (g_max_lon - g_min_lon) * 111320 * math.cos(math.radians(mid_lat))

    cell_m = 400
    n_cols = max(4, min(10, round(lon_m / cell_m)))
    n_rows = max(3, min(8, round(lat_m / cell_m)))

    for m in markers:
        c = int((m["lon"] - g_min_lon) / (g_max_lon - g_min_lon) * n_cols)
        r = int((g_max_lat - m["lat"]) / (g_max_lat - g_min_lat) * n_rows)
        m["grid"] = f"{COLS[max(0, min(n_cols - 1, c))]}{max(1, min(n_rows, r + 1))}"

    return {"minLat": g_min_lat, "maxLat": g_max_lat,
            "minLon": g_min_lon, "maxLon": g_max_lon,
            "cols": n_cols, "rows": n_rows}


def build_html(markers):
    if not markers:
        sys.exit("No markers with GPS found.")

    grid = compute_grid(markers)

    lats = [m["lat"] for m in markers]
    lons = [m["lon"] for m in markers]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    markers_js = json.dumps(markers, indent=2)
    grid_js = json.dumps(grid)

    return f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TWF Beschilderung</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css"/>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: sans-serif; background: #1a1a1a; }}
  #map {{ width: 100vw; height: 100vh; }}
  .thumb-icon img {{ width: 60px; height: 60px; object-fit: cover;
    border: 3px solid #fff; border-radius: 4px;
    box-shadow: 0 2px 6px rgba(0,0,0,.6); cursor: pointer; display: block; }}
  .thumb-inner {{ position: relative; display: inline-block; }}
  .acc-fair img, img.acc-fair, .thumb-cluster img.acc-fair  {{ border-color: #e09600; }}
  .acc-poor img, img.acc-poor, .thumb-cluster img.acc-poor  {{ border-color: #d42020; }}
  /* Stacked cluster icon */
  .thumb-cluster {{ position: relative; width: 72px; height: 72px; cursor: pointer; }}
  .thumb-cluster img {{
    position: absolute; width: 54px; height: 54px; object-fit: cover;
    border: 2px solid #fff; border-radius: 4px;
    box-shadow: 0 2px 8px rgba(0,0,0,.5);
  }}
  .thumb-cluster img:nth-child(1) {{ top:14px; left:0;   transform: rotate(-8deg); }}
  .thumb-cluster img:nth-child(2) {{ top:8px;  left:8px; transform: rotate(3deg);  }}
  .thumb-cluster img:nth-child(3) {{ top:2px;  left:14px;transform: rotate(-2deg); }}
  .thumb-cluster .cluster-count {{
    position: absolute; top: 0; right: 0;
    background: #e55; color: #fff;
    font-size: .72rem; font-weight: bold;
    min-width: 20px; height: 20px; line-height: 20px;
    padding: 0 4px; border-radius: 10px;
    text-align: center; box-shadow: 0 1px 4px rgba(0,0,0,.4);
    pointer-events: none;
  }}
  /* Lightbox */
  #lb {{ display:none; position:fixed; inset:0; background:rgba(0,0,0,.85);
    z-index:9999; align-items:center; justify-content:center; flex-direction:column; }}
  #lb.open {{ display:flex; }}
  #lb img, #lb video {{ max-width:92vw; max-height:82vh; border-radius:6px;
    box-shadow: 0 4px 24px rgba(0,0,0,.8); }}
  #lb video {{ background:#000; }}
  #lb-img {{ display:block; }}
  #lb-vid {{ display:none; }}
  #lb-spinner {{
    width:52px; height:52px; border-radius:50%;
    border:5px solid rgba(255,255,255,.2);
    border-top-color:#fff;
    animation: spin .7s linear infinite;
    display:none;
  }}
  @keyframes spin {{ to {{ transform:rotate(360deg); }} }}
  /* Grid overlay */
  .grid-label {{
    font-size:.95rem; font-weight:700; color:rgba(60,60,60,.65);
    text-shadow: 0 0 3px #fff, 0 0 3px #fff;
    pointer-events:none; white-space:nowrap;
  }}
  #lb-caption {{ color:#eee; margin-top:10px; font-size:.9rem; }}
  #lb-close {{
    position:absolute; top:0; right:0; color:#fff;
    font-size:2rem; cursor:pointer; line-height:1;
    padding:16px 20px; min-width:48px; min-height:48px;
    display:flex; align-items:center; justify-content:center;
    z-index:1;
  }}
  #lb-prev, #lb-next {{
    position:absolute; top:0; bottom:0; color:#fff;
    font-size:2.5rem; cursor:pointer; user-select:none;
    display:flex; align-items:center; padding:0 16px;
    min-width:56px;
  }}
  #lb-prev {{ left:0; }}
  #lb-next {{ right:0; }}
</style>
</head>
<body>
<div id="map"></div>

<!-- Lightbox -->
<div id="lb">
  <span id="lb-close">&times;</span>
  <span id="lb-prev">&#8249;</span>
  <div id="lb-spinner"></div>
  <img id="lb-img" src="" alt="">
  <video id="lb-vid" controls playsinline></video>
  <div id="lb-caption"></div>
  <span id="lb-next">&#8250;</span>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<script>
const markers = {markers_js};
const gridParams = {grid_js};

const map = L.map('map', {{maxZoom: 21}}).setView([{center_lat:.6f}, {center_lon:.6f}], 15);
// All providers below work from file:// (no Referer enforcement)
const esriTopo = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{{z}}/{{y}}/{{x}}',
  {{ attribution: 'Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, TomTom, Intermap, iPC, USGS, FAO, NPS, NRCAN, GeoBase, Kadaster NL, Ordnance Survey, Esri Japan, METI, Esri China (Hong Kong), and the GIS User Community',
     maxZoom: 21, maxNativeZoom: 19 }}
);
const cyclOsm = L.tileLayer(
  'https://{{s}}.tile-cyclosm.openstreetmap.fr/cyclosm/{{z}}/{{x}}/{{y}}.png',
  {{ attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> | <a href="https://www.cyclosm.org">CyclOSM</a>',
     subdomains: 'abc', maxZoom: 21, maxNativeZoom: 19 }}
);
const cartoLayer = L.tileLayer(
  'https://{{s}}.basemaps.cartocdn.com/rastertiles/voyager/{{z}}/{{x}}/{{y}}{{r}}.png',
  {{ attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
     subdomains: 'abcd', maxZoom: 21, maxNativeZoom: 20 }}
);
const topoLayer = L.tileLayer(
  'https://{{s}}.tile.opentopomap.org/{{z}}/{{x}}/{{y}}.png',
  {{ attribution: 'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | Style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
     subdomains: 'abc', maxZoom: 21, maxNativeZoom: 17 }}
);
const satellite = L.tileLayer(
  'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
  {{ attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
     maxZoom: 21, maxNativeZoom: 19 }}
);
const hikingOverlay = L.tileLayer(
  'https://tile.waymarkedtrails.org/hiking/{{z}}/{{x}}/{{y}}.png',
  {{ attribution: '&copy; <a href="https://waymarkedtrails.org">Waymarked Trails</a>', maxZoom: 21, opacity: 0.8 }}
);

// Grid overlay (letter-number cells, like a paper map)
function buildGridLayer() {{
  const {{minLat, maxLat, minLon, maxLon, cols, rows}} = gridParams;
  const latStep = (maxLat - minLat) / rows;
  const lonStep = (maxLon - minLon) / cols;
  const ALPHA = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
  const group = L.layerGroup();
  const lineStyle = {{color:'#555', weight:1, opacity:0.45, dashArray:'5 5', interactive:false}};

  for (let r = 0; r <= rows; r++)
    L.polyline([[minLat + r*latStep, minLon],[minLat + r*latStep, maxLon]], lineStyle).addTo(group);
  for (let c = 0; c <= cols; c++)
    L.polyline([[minLat, minLon + c*lonStep],[maxLat, minLon + c*lonStep]], lineStyle).addTo(group);

  for (let r = 0; r < rows; r++) {{
    for (let c = 0; c < cols; c++) {{
      const lat = maxLat - (r + 0.5) * latStep;
      const lon = minLon + (c + 0.5) * lonStep;
      L.marker([lat, lon], {{
        icon: L.divIcon({{className:'', html:`<div class="grid-label">${{ALPHA[c]}}${{r+1}}</div>`, iconSize:[40,20], iconAnchor:[20,10]}}),
        interactive: false, keyboard: false
      }}).addTo(group);
    }}
  }}
  return group;
}}
const gridLayer = buildGridLayer();
gridLayer.addTo(map);

cyclOsm.addTo(map);
L.control.layers(
  {{ 'CyclOSM (Wege)': cyclOsm, 'Satellit': satellite, 'Esri Topo': esriTopo,
     'CartoDB Voyager': cartoLayer, 'OpenTopoMap': topoLayer }},
  {{ 'Wanderwege-Overlay': hikingOverlay, 'Planquadrat-Raster': gridLayer }},
  {{ position: 'topright' }}
).addTo(map);

// Haversine distance in metres between two lat/lon points
function distM(a, b) {{
  const R = 6371000, toRad = d => d * Math.PI / 180;
  const dLat = toRad(b.lat - a.lat), dLon = toRad(b.lon - a.lon);
  const x = Math.sin(dLat/2)**2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(x), Math.sqrt(1-x));
}}

const RADIUS_M = 20;

// Lightbox state — group holds indices of markers in the current local set
let lbGroup = [];
let lbPos = 0;
const lb = document.getElementById('lb');
const lbImg = document.getElementById('lb-img');
const lbVid = document.getElementById('lb-vid');
const lbCap = document.getElementById('lb-caption');
const lbSpinner = document.getElementById('lb-spinner');

function showLbAt(pos) {{
  lbPos = (pos + lbGroup.length) % lbGroup.length;
  const m = markers[lbGroup[lbPos]];
  const isVideo = m.type === 'video';
  const accStr = m.acc ? `  ·  ±${{Math.round(m.acc)}}m` : '';
  lbCap.textContent = `${{m.name}}  ·  ${{m.grid}}${{accStr}}  (${{lbPos + 1}}/${{lbGroup.length}})`;

  if (isVideo) {{
    lbImg.style.display = 'none';
    lbSpinner.style.display = 'none';
    lbVid.style.display = 'block';
    lbVid.src = m.video;
    lbVid.load();
    lbVid.play();
  }} else {{
    lbVid.pause();
    lbVid.src = '';
    lbVid.style.display = 'none';
    // Show spinner, hide stale image until new one loads
    lbImg.style.display = 'none';
    lbSpinner.style.display = 'block';
    const nextSrc = m.full;
    lbImg.onload = null;
    lbImg.onload = () => {{
      if (lbImg.src.endsWith(nextSrc) || lbImg.src === nextSrc) {{
        lbSpinner.style.display = 'none';
        lbImg.style.display = 'block';
      }}
    }};
    lbImg.src = nextSrc;
    // Already cached — onload may not fire again
    if (lbImg.complete && lbImg.naturalWidth > 0) {{
      lbSpinner.style.display = 'none';
      lbImg.style.display = 'block';
    }}
  }}
}}

function openLb(i) {{
  // Start with all markers within RADIUS_M
  const origin = markers[i];
  const nearby = new Set(
    markers
      .map((m, idx) => ({{ idx, d: distM(origin, m) }}))
      .filter(x => x.d <= RADIUS_M)
      .map(x => x.idx)
  );
  // Also include any cluster siblings (same visual cluster as the clicked marker)
  cluster.eachLayer(lyr => {{
    if (lyr._idx === i) {{
      const parent = lyr.__parent;
      if (parent && parent.getAllChildMarkers) {{
        parent.getAllChildMarkers().forEach(m => nearby.add(m._idx));
      }}
    }}
  }});
  lbGroup = [...nearby].sort((a, b) => a - b);
  lbPos = lbGroup.indexOf(i);
  showLbAt(lbPos);
  lb.classList.add('open');
}}

document.getElementById('lb-close').onclick = () => {{ lbVid.pause(); lb.classList.remove('open'); }};
document.getElementById('lb-prev').onclick = () => showLbAt(lbPos - 1);
document.getElementById('lb-next').onclick = () => showLbAt(lbPos + 1);
lb.addEventListener('click', e => {{ if (e.target === lb) {{ lbVid.pause(); lb.classList.remove('open'); }} }});
document.addEventListener('keydown', e => {{
  if (!lb.classList.contains('open')) return;
  if (e.key === 'Escape') {{ lbVid.pause(); lb.classList.remove('open'); }}
  if (e.key === 'ArrowLeft') showLbAt(lbPos - 1);
  if (e.key === 'ArrowRight') showLbAt(lbPos + 1);
}});

// Swipe gestures for mobile
let touchX = null;
lb.addEventListener('touchstart', e => {{ touchX = e.touches[0].clientX; }}, {{passive: true}});
lb.addEventListener('touchend', e => {{
  if (touchX === null) return;
  const dx = e.changedTouches[0].clientX - touchX;
  touchX = null;
  if (Math.abs(dx) < 40) return;
  dx < 0 ? showLbAt(lbPos + 1) : showLbAt(lbPos - 1);
}}, {{passive: true}});

// Cluster group with stacked-thumbnail icons
const cluster = L.markerClusterGroup({{
  maxClusterRadius: 50,
  spiderfyOnMaxZoom: false,
  showCoverageOnHover: false,
  zoomToBoundsOnClick: false,
  iconCreateFunction: function(c) {{
    const children = c.getAllChildMarkers().slice(0, 3);
    const imgs = children.map(m => `<img src="${{m._thumb}}" alt="" class="${{m._accClass}}">`).join('');
    return L.divIcon({{
      html: `<div class="thumb-cluster">${{imgs}}</div>`,
      className: '',
      iconSize: [72, 72],
      iconAnchor: [36, 36]
    }});
  }}
}});

// Cluster click → open lightbox with all children (sorted by index = filename order)
cluster.on('clusterclick', e => {{
  lbGroup = e.layer.getAllChildMarkers()
    .map(m => m._idx)
    .sort((a, b) => a - b);
  showLbAt(0);
  lb.classList.add('open');
}});

function accClass(acc) {{
  if (!acc || acc < 30) return '';
  return acc >= 100 ? 'acc-poor' : 'acc-fair';
}}

markers.forEach((m, i) => {{
  const icon = L.divIcon({{
    className: 'thumb-icon',
    html: `<div class="thumb-inner ${{accClass(m.acc)}}"><img src="${{m.thumb}}" alt="${{m.name}}" title="${{m.name}}"></div>`,
    iconSize: [64, 64],
    iconAnchor: [32, 32]
  }});
  const marker = L.marker([m.lat, m.lon], {{icon}});
  marker._thumb = m.thumb;
  marker._accClass = accClass(m.acc);
  marker._idx = i;
  marker.on('click', () => openLb(i));
  cluster.addLayer(marker);
}});

map.addLayer(cluster);

// Fit map to all markers
const coords = markers.map(m => [m.lat, m.lon]);
if (coords.length > 1) map.fitBounds(coords, {{padding: [40, 40]}});
</script>
</body>
</html>
"""


def main():
    THUMBS.mkdir(parents=True, exist_ok=True)
    FULL.mkdir(parents=True, exist_ok=True)
    VIDEOS.mkdir(parents=True, exist_ok=True)
    FULL.mkdir(parents=True, exist_ok=True)

    files = sorted(BASE.glob("*.HEIC")) + sorted(BASE.glob("*.heic")) + \
            sorted(BASE.glob("*.MOV")) + sorted(BASE.glob("*.mov"))

    if not files:
        sys.exit("No HEIC or MOV files found in current directory.")

    print(f"Found {len(files)} media files.")
    markers = []

    for f in files:
        print(f"Processing {f.name} ...", end=" ", flush=True)
        try:
            if f.suffix.upper() == ".HEIC":
                m = process_heic(f)
            else:
                m = process_mov(f)
            if m:
                markers.append(m)
                print(f"({m['lat']:.5f}, {m['lon']:.5f})")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\nBuilding map with {len(markers)} markers...")
    html = build_html(markers)
    (WEB / "index.html").write_text(html, encoding="utf-8")
    print(f"Done → {WEB / 'index.html'}")


if __name__ == "__main__":
    main()
