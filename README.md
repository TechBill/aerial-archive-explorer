# Aerial Archive Explorer

Search and download historic USGS aerial photographs by location.

Aerial Archive Explorer is an independent desktop catalog client. It is not an
official USGS product and is not endorsed by USGS. It searches only the USGS
**Aerial Photo Single Frames** collection. Historical scans are not necessarily
georeferenced or georectified, and a catalog match does not guarantee that the
entered point is visible in the photograph.

## Requirements

- Python 3.11 or newer with Tkinter
- A free USGS EROS account with approved M2M access
- A USGS M2M application token
- Pillow (bounded in `requirements.txt`)
- Certifi for a bundled, verified TLS certificate-authority store

Request M2M access and manage tokens from the
[EROS profile](https://ers.cr.usgs.gov/profile/access). The startup access dialog
stores the application token only in the operating-system credential store
(macOS Keychain, Windows Credential Locker, or the supported system vault), never
in a plaintext configuration file. Choose **Skip for Now** to open the interface
without USGS access. Use **Sign Out** to clear saved access and return to the
startup dialog.

## Run from source

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt
python aerial_archive_explorer.py
```

Enter a username, application token, latitude, longitude, and small catalog
search radius. The **Paste Coordinates** button accepts `latitude, longitude`,
a whitespace-separated decimal pair, or a single Google Earth KML
`<coordinates>longitude,latitude[,altitude]</coordinates>` tuple.

**Quick Preview** uses the lower-resolution browse supplied by USGS. **Open Best
Image in Viewer** checks current products, selects the highest-resolution
immediately downloadable scan, and caches it only for this app session. **Save
Image** in the viewer copies those same bytes and does not download the image a
second time. Paid or order-only products are never ordered automatically.

Use **View Logs** in the bottom status bar to inspect and copy current-session
network diagnostics. Application tokens, session keys, and signed download URLs
are redacted from this display.

## Test

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

Tests use synthetic, credential-free API responses and do not contact USGS.

## Packaging

The runnable production application remains the single file
`aerial_archive_explorer.py`. When packaging, reuse `assets/icon.ico` for Windows
and `assets/icon.icns` for macOS. Do not replace the supplied icons.

```bash
python -m pip install -r requirements-dev.txt
pyinstaller aerial_archive_explorer.spec
```

The platform-specific app or executable is written beneath `dist/`.

## Data and API references

- [USGS M2M API](https://m2m.cr.usgs.gov/)
- [Aerial Photo Single Frames overview](https://www.usgs.gov/centers/eros/science/usgs-eros-archive-aerial-photography-aerial-photo-single-frames)
- [Collection data dictionary](https://www.usgs.gov/centers/eros/science/aerial-photo-single-frames-data-dictionary)
- [EarthExplorer](https://earthexplorer.usgs.gov/)
