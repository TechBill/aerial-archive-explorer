# Aerial Archive Explorer 2.1.0

Search and download historic USGS aerial photographs by location.

Aerial Archive Explorer is an independent desktop catalog client. It is not an
official USGS product and is not endorsed by USGS. It searches only the USGS
**Aerial Photo Single Frames** collection, and only shows frames whose USGS
four-corner footprint actually covers the coordinate you entered. Historical
scans are not necessarily georeferenced or georectified, and a footprint match
is not a pixel-level guarantee that the point is visible in the photograph.

## Requirements

- Python 3.11 or newer with Tkinter
- A free USGS EROS account with approved M2M access
- A USGS M2M application token
- Pillow (bounded in `requirements.txt`)
- Certifi for a bundled, verified TLS certificate-authority store

By default, the startup access dialog stores the application token only in
the operating-system credential store (macOS Keychain, Windows Credential
Locker, or the supported system vault), never in a plaintext file. On the same
dialog you can opt in to a lower-security alternative — a local file on this
machine — useful mainly on a personal, non-shared computer when the OS
keychain's re-authorization prompt becomes disruptive (for example, an
unsigned development build re-prompts on every rebuild). Choose **Skip for
Now** to open the interface without USGS access. Use **Sign Out** to clear
saved access, from whichever store holds it, and return to the startup dialog.

## Getting a USGS M2M application token

To get a Machine-to-Machine (M2M) application token for USGS EROS services,
you must register an account on the USGS EROS Registration System (ERS),
request M2M API access in your profile settings, and then generate your
application token for programmatic login.

### Account creation & setup

1. Register a free account at [ERS User Registration](https://ers.cr.usgs.gov/register/) if you do not already have one.
2. Check your email to confirm and activate the new account.
3. Log in and go to your [ERS profile access page](https://ers.cr.usgs.gov/profile/access) to request **Machine to Machine (M2M)** access by filling out the data-use questionnaire.
4. Wait for approval. USGS access review can take a day or two.

### Generating an application token

1. Once access is approved, return to your [ERS profile access page](https://ers.cr.usgs.gov/profile/access) (also reachable from the app's **M2M Access / Token Help** button).
2. Generate a new application token. USGS allows more than one active token per account, so you can revoke and regenerate one at any time without losing access.
3. Enter that token string — not your ERS account password — into this app's access dialog. The app uses it with the M2M `login-token` endpoint and never asks for your password.

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
`<coordinates>longitude,latitude[,altitude]</coordinates>` tuple. The radius
only bounds the initial catalog search; every frame actually listed has been
confirmed to cover your exact coordinate.

Select a frame and use **View Aerial** to fetch and open the highest-resolution
immediately downloadable scan in the interactive viewer, or **Download** to
save a product to a destination of your choice. Both are offered as
`<entity ID>.tif` and include a plain-text USGS identity/footprint block
embedded in the saved TIFF's metadata (entity ID, acquisition date,
project/roll/frame/scale, all four corners and center as decimal-degree
coordinates) for later reference or use in another tool. Paid or order-only
products are never ordered automatically — use **Open in EarthExplorer** for
those. In the viewer, **Rotate Left 90° / Rotate 180° / Rotate Right 90°**
change only the displayed orientation; **Save Image** always saves the
original scan.

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

---

## Author

Bill Fleming (TechBill)

Donations (optional)
PayPal: <https://www.paypal.com/paypalme/techbill>
Buy Me a Coffee: <https://buymeacoffee.com/techbill>
