# AGENTS.md

## Product identity

- Application name: **Aerial Archive Explorer**
- Use this name consistently in the window title, dialogs, documentation, packaging metadata, and user-visible messages.
- Suggested descriptive subtitle: **Search and download historic USGS aerial photographs by location.**
- The name describes an independent application; do not imply that it is an official USGS product or endorsed by USGS.
- Git repository/project folder: `aerial-archive-explorer/`.
- Platform app icons are stored relative to the repository root as:
  - `assets/icon.ico` for Windows packaging and executable metadata;
  - `assets/icon.icns` for macOS application packaging.
- Reuse these supplied icon files. Do not regenerate, rename, relocate, or overwrite them unless the user explicitly requests an icon change.

## Project mission

Build **Aerial Archive Explorer**, a small, dependable desktop application that helps a nontechnical user find and obtain historical **USGS Aerial Photo Single Frames** for a latitude/longitude.

The shipped application must be a **single Python source file** using Tkinter. It should:

1. accept decimal-degree coordinates,
2. search only the USGS Aerial Photo Single Frames collection,
3. show matching frames in a useful, sortable list,
4. show acquisition year/date and key metadata,
5. let the user inspect a quick browse preview,
6. let the user download the best immediately available scan into temporary storage and open that full product in an interactive zoom/pan viewer, and
7. let the user save the already-fetched viewer image permanently without downloading it again.

The app is an index and downloader, not a GIS. Historical scans are not necessarily georeferenced or georectified.

## Scope boundaries

### In scope

- One primary Tkinter window plus an owned interactive viewer window.
- Decimal latitude/longitude input in WGS 84 (`EPSG:4326`).
- Optional small search-radius control, expressed in miles or kilometers and converted to a bounding box.
- USGS EROS Machine-to-Machine (M2M) API authentication with a USGS username and application token.
- Dynamic discovery of the Aerial Photo Single Frames dataset alias.
- Coordinate-based scene search, pagination, metadata normalization, and oldest-to-newest sorting.
- Result filtering and sorting in the UI without repeating the network search.
- Fast browse-image preview where USGS supplies one; browse imagery is for identification and is not the preferred viewer source.
- A dedicated interactive aerial-image viewer with zoom and pan behavior modeled after `/Users/techbill/Documents/GitHub/lidar-hillshade-explorer/src/hillshade_viewer.py`.
- Temporary acquisition of the best immediately available scan for full-resolution viewing, with an option to save that same local file permanently.
- Download-product discovery, destination selection, download progress, cancellation, and clear completion status.
- Links to the relevant EarthExplorer record or site when the API cannot provide an immediate download.
- Cross-platform behavior on current supported Python releases where Tkinter is available.

### Explicitly out of scope for the initial app

- Georeferencing, georectification, orthorectification, image warping, reprojection, mosaicking, or control-point selection.
- Geofencing, polygon drawing, parcel lookup, address geocoding, or an interactive GIS map.
- Automated image interpretation or computer vision.
- Searching unrelated EarthExplorer datasets.
- Bulk unattended harvesting, download queues that persist across launches, user accounts managed by this app, or cloud storage integration.
- Pretending that a frame's catalog point or footprint guarantees that the entered point is visibly present in the scanned image.

Do not quietly add an out-of-scope feature. Keep extension seams clean, document the idea, and defer it.

## Authoritative external references

Consult current official USGS documentation before implementing or changing API payloads:

- M2M landing page: <https://m2m.cr.usgs.gov/>
- M2M JSON API documentation: <https://m2m.cr.usgs.gov/api/docs/json/>
- M2M test page: <https://m2m.cr.usgs.gov/api/test/json/>
- Stable JSON API base: `https://m2m.cr.usgs.gov/api/api/json/stable/`
- Aerial Photo Single Frames overview: <https://www.usgs.gov/centers/eros/science/usgs-eros-archive-aerial-photography-aerial-photo-single-frames>
- Collection data dictionary: <https://www.usgs.gov/centers/eros/science/aerial-photo-single-frames-data-dictionary>
- EarthExplorer: <https://earthexplorer.usgs.gov/>

Treat endpoint request/response examples in this file as a design outline, not a substitute for the current API schema. USGS can change aliases, product identifiers, metadata field labels, availability, and authentication requirements.

## User and credential assumptions

- The user needs a free USGS EROS/ERS account, approved M2M access, and an application token.
- Use the current token flow (`login-token`) with the username and application token. Do not implement the retired username/password `login` flow unless current official USGS documentation explicitly restores it.
- Never log, print, persist in source code, include in exception messages, or put on the clipboard the application token or returned API key.
- By default, keep credentials only in memory for the current run. A future “remember token” option must use an operating-system credential store; never use a plaintext config file.
- Mask the token entry. Explain where to obtain M2M access/token in concise UI help.
- Send the session API key only through the API's required authentication header, normally `X-Auth-Token`.
- Attempt `logout` on normal shutdown when a session exists; failure to log out must not block application exit.

## Deliverable and dependency policy

- Treat `aerial-archive-explorer/` as the repository root. Resolve all project-relative paths from that directory.
- The production application is one file named `aerial_archive_explorer.py`.
- Keep UI, models, API client, download logic, and entry point in that file, separated into well-marked sections and classes.
- Tests, fixtures, documentation, packaging files, and dependency declarations may be separate. “Single-file” applies to the runnable application source, not the entire repository.
- Prefer the standard library. Expected runtime libraries are:
  - Tkinter/ttk from the Python installation;
  - `urllib.request` or one deliberately chosen HTTP library;
  - Pillow only if needed for reliable JPEG browse display and thumbnail scaling.
- Do not introduce GIS libraries such as GDAL, Rasterio, GeoPandas, Shapely, or PyProj in this phase.
- Pin or bound third-party dependencies in the dependency file and explain how to install them.

## Architecture inside the single file

Keep these responsibilities separate even though they live in one module:

### Configuration and constants

- API base URL, request timeout, user agent, search limit/page size, default radius, retry limits, and UI column definitions.
- No credentials, dataset alias, product ID, download URL, or machine-specific path in constants.

### Data models

Use small `@dataclass` value objects where useful:

- `SearchQuery`: latitude, longitude, radius, optional date limits.
- `AerialFrame`: normalized entity/display ID, acquisition date, year, agency, project, roll, frame, scale, image type, quality, coordinates/footprint summary, browse URL, and raw metadata needed for details.
- `DownloadProduct`: product ID, product name/resolution, file size when known, availability/order state, and entity ID.
- `ApiError`: a typed exception carrying a safe user-facing category and optional technical detail without secrets.

Do not let Tkinter widgets depend directly on arbitrary raw USGS JSON. Normalize external data once at the API boundary. Preserve unknown or extra metadata in a raw/details mapping so useful fields are not discarded.

### `UsgsM2MClient`

Own all HTTP and M2M behavior:

- JSON request encoding and response-envelope validation.
- Token login, session API key, logout.
- Dataset discovery and in-memory alias caching.
- Scene search and pagination.
- Metadata extraction tolerant of missing fields and modest schema variation.
- Download-option lookup, download request, and retrieval polling.
- Streaming file download.
- Timeouts, bounded retries, cancellation checks, and redacted diagnostics.

The client must not import or call Tkinter. Make it independently testable with a fake transport.

### Controller/application class

- Own widgets and user-visible state.
- Validate inputs before starting work.
- Submit network and disk-heavy operations to background worker threads.
- Pass completed results/errors back to the Tk main thread through a `queue.Queue` polled by `after(...)` or an equivalent safe mechanism.
- Only the Tk main thread may create, read, or mutate widgets or `PhotoImage` objects.
- Ignore stale worker results using a request/generation ID when a newer search supersedes an older one.
- Coordinate cancellation and orderly shutdown.

### Preview helper

- Fetch browse bytes off the UI thread with a size limit and timeout.
- Decode and resize while preserving aspect ratio.
- Create/assign the Tk-compatible image on the UI thread and retain a strong reference so it is not garbage-collected.
- Never stretch a browse image to imply exact geographic alignment.

### Interactive viewer

- Implement the viewer inside `aerial_archive_explorer.py` to preserve the single-file application requirement, even though the reference LiDAR app keeps its viewer in a separate module.
- Open it as a dedicated Tkinter `Toplevel` window owned by the main application. Do not start a second Tk root or event loop.
- Use a dark canvas and Pillow-backed rendering, following the interaction pattern in `/Users/techbill/Documents/GitHub/lidar-hillshade-explorer/src/hillshade_viewer.py` without importing from or depending on that separate project.
- The normal viewer source is a locally cached downloadable scan, not the low-resolution catalog browse image. Use a browse image in the viewer only through an explicitly labeled fallback when no downloadable scan is available.
- Keep viewer state isolated from the main results table: source image, current scale, minimum/maximum scale, X/Y offset, active pan state, and pending redraw callback.
- Render only the visible source-image region when practical so large TIFF scans remain responsive and do not require constructing enormous scaled bitmaps.
- Coalesce rapid wheel and drag events into throttled redraws with `after(...)` rather than rerendering excessively.
- Closing the viewer must release image references without closing the main application or losing its search results. Session cache cleanup follows the cache-lifecycle rules below, not an unconditional delete on viewer close.

### Download helper

- Stream to a temporary sibling file such as `name.part` rather than holding the file in memory.
- Update progress at a throttled rate; total size may be unknown.
- On success, flush/close and atomically rename to the final filename.
- On failure or cancellation, close handles and offer to remove the partial file. Never overwrite an existing file without explicit user confirmation.
- Sanitize server-provided filenames and prevent path traversal.

### Viewer image cache

- Use a per-application-session temporary directory created with `tempfile.TemporaryDirectory` or an equivalently safe mechanism.
- The **Open Best Image in Viewer** workflow streams the chosen USGS product to a `.part` file in this directory, then atomically renames it after a complete transfer.
- Key completed cache entries by dataset, entity ID, and product ID. Reopening the same product or subsequently choosing **Save Image** must reuse the completed local file instead of repeating the network download.
- If the user already downloaded the identical product to a known existing path during this session, prefer opening that file rather than making a temporary copy.
- Show required/available disk-space information when the API reports product size. Detect disk-full and permission failures cleanly.
- Remove incomplete `.part` files after cancellation or failure when handles are closed. Clean completed temporary viewer files at normal app shutdown, but never delete a user-saved destination.
- Do not treat the cache as permanent or promise it will survive a crash or relaunch.

## M2M API workflow

Implement the workflow as a stateful client. Verify exact fields against current official docs and real redacted responses.

### 1. Authenticate

POST to `login-token` with the current required username/token payload. Store the returned session API key only in memory and supply it through `X-Auth-Token` on authenticated calls.

Handle distinctly:

- invalid username/token,
- account lacking M2M access,
- expired/revoked token,
- throttling/service outage, and
- malformed or changed API responses.

Never fall back to collecting an account password.

### 2. Discover the dataset

Call `dataset-search` and identify the dataset whose current official title/collection identity is **Aerial Photo Single Frames**. Use the returned `datasetAlias`/dataset name required by subsequent endpoints.

- Do not permanently hardcode an assumed alias.
- It is acceptable to keep a documented expected alias only as a matching hint, never as the sole lookup method.
- If discovery returns zero or multiple plausible datasets, show a useful diagnostic instead of silently choosing the wrong collection.
- Cache the resolved alias for the current process/session.

### 3. Build a spatial search

Validate latitude in `[-90, 90]` and longitude in `[-180, 180]`. Reject blank, nonnumeric, NaN, and infinity values.

Use an MBR spatial filter in WGS 84. Do not send a zero-area rectangle with identical lower-left and upper-right corners because server behavior may vary. Convert the selected radius to a small bounding box:

- latitude delta is approximately `radius_km / 111.32`;
- longitude delta is approximately `radius_km / (111.32 * cos(latitude))`;
- clamp bounds to valid latitude/longitude ranges;
- handle poles and antimeridian crossing explicitly, splitting into two searches if required.

Explain in the UI that the radius is a catalog-search tolerance, not geofencing and not a claim about pixel-level coverage.

The conceptual `scene-search` payload is:

```json
{
  "datasetName": "<discovered alias>",
  "maxResults": 100,
  "startingNumber": 1,
  "sceneFilter": {
    "spatialFilter": {
      "filterType": "mbr",
      "lowerLeft": {"latitude": 0.0, "longitude": 0.0},
      "upperRight": {"latitude": 0.0, "longitude": 0.0}
    }
  }
}
```

Populate the coordinates with computed nonzero bounds. Add acquisition date filters only when the UI exposes them and the current API schema supports them.

### 4. Page and normalize results

- Respect `totalHits`, `recordsReturned`, and the API's current maximum page size.
- Fetch additional pages deliberately, with a configured total cap to prevent a seemingly frozen app.
- If results exceed the cap, show that the list is partial and offer a clear “Load more” action or narrower-search advice.
- Deduplicate by stable entity ID when combining split/continued searches.
- Parse acquisition dates defensively; keep unknown dates and sort them last.
- Default sort is acquisition date oldest to newest, then display/entity ID for deterministic ordering.
- Treat metadata labels as external data. Build a small alias map for known label variants and retain the unmapped fields in the details view.

Prefer these displayed fields when present:

- acquisition year and full date,
- agency/source,
- project,
- roll and frame number,
- photo/display/entity ID,
- nominal scale,
- image/film type,
- quality,
- browse availability,
- high-resolution download availability,
- center coordinates or footprint summary.

Missing metadata must render as an em dash or “Unknown,” never crash the search.

### 5. Quick preview

Use a browse URL explicitly supplied by the API response or current documented browse mechanism. Do not guess undocumented image URLs.

- Preview is disabled when no browse is available.
- Show a placeholder and a short message for missing, unauthorized, corrupt, or unsupported browse content.
- A preview failure must not remove the result or prevent download.
- Label the preview as an unrectified scan/browse image.

The browse preview is intentionally separate from the full viewer workflow:

- Label it **Quick Preview** and explain that it is lower resolution and intended to confirm the correct frame.
- A missing browse image must not disable **Open Best Image in Viewer** when a downloadable product is available.
- Double-clicking a result may open Quick Preview but must not silently begin a full-product download.

### 6. Discover products, open the best image, and download

For the selected entity:

1. call `download-options` using the current documented entity/list request form;
2. display all relevant available products with product name/resolution and size when known;
3. distinguish immediate downloads, products requiring preparation, and unavailable/on-demand/order-only products;
4. let the user explicitly choose a product when more than one is available;
5. submit the selected entity/product pair to `download-request` with a unique request label;
6. consume immediate URLs from the response;
7. when preparation is required, poll `download-retrieve` at a bounded interval until ready, failed, cancelled, or timed out; and
8. stream the returned short-lived URL to the chosen local path.

Support two destinations through the same product-resolution and transfer pipeline:

- **Open Best Image in Viewer:** rank products, explain the selected product and its size/state, download it to the session cache with visible progress and cancellation, then open the completed local file in the viewer.
- **Download / Save As:** let the user select a product and permanent destination. If the identical completed product is already in the viewer cache, atomically copy it to the chosen destination instead of requesting/downloading it again.

“Best” means the highest-resolution product that USGS reports as immediately downloadable without a new paid scan/order. Prefer high-resolution (commonly 1,000 dpi) over medium-resolution (commonly 400 dpi) only when it is actually available for immediate download. Determine ranking from current product metadata and known normalized product labels; do not guess from file extensions or assume a fixed product ID. Show the chosen resolution and file size before a large transfer and allow cancellation.

If the highest-resolution product requires preparation but is otherwise an ordinary available download, show **Preparing image**, poll normally, and open it when ready. If it is order-only, requires payment, licensing, or any other external commitment, never place the order automatically. Explain the situation and offer:

1. the best lower-resolution immediately downloadable product, normally the free 400 dpi scan when available;
2. an explicitly labeled **View Browse Image Instead** fallback; or
3. **Open in EarthExplorer** for the user to review and authorize the higher-resolution order.

Never assume “high resolution available” means an immediate free API URL. Surface the state returned by USGS and, when needed, provide a button to open the appropriate EarthExplorer page in the user's browser.

Download URLs may expire. Request them just before transfer and, where safe, reacquire once after an authorization/expiration failure. Do not cache them across launches.

## UI behavior

Use native-looking `ttk` widgets and keyboard-accessible controls. A practical layout is:

### Top: access and search

- Username field.
- Masked application-token field.
- Latitude and longitude fields.
- A centered **Paste Coordinates** button directly beneath the latitude and longitude input boxes, matching the placement and interaction in `/Users/techbill/Documents/GitHub/lidar-hillshade-explorer/src/main_gui.py`.
- Small search-radius field/control with unit label and conservative default.
- Search button; Enter triggers search when focus is in a search input.
- Concise link/help for USGS account and M2M token setup.

### Paste Coordinates behavior

- Clicking **Paste Coordinates** reads plain text from the operating-system clipboard and fills both coordinate input boxes in one action.
- Support the common Google Maps decimal-degree format `latitude, longitude`, including optional surrounding whitespace, for example `37.123456, -93.654321`.
- Also accept a reasonable whitespace-separated decimal pair such as `37.123456 -93.654321`, but do not guess when the order or format is ambiguous.
- Interpret the first number as latitude and the second as longitude. Preserve negative signs and sufficient decimal precision; do not round pasted values unnecessarily.
- For compatibility with LiDAR Hillshade Explorer, also accept a Google Earth KML/XML `<coordinates>` value containing one unambiguous tuple in `longitude,latitude[,altitude]` order. Reverse the first two KML values when filling the latitude and longitude fields, and ignore an optional altitude.
- Strip harmless surrounding text only when one coordinate pair can be identified unambiguously. Reject URLs, multiple coordinate pairs/tuples, degrees/minutes/seconds, or malformed clipboard content in version 1 rather than silently inserting the wrong location.
- Validate the parsed values using the same finite-number and range checks as manually entered coordinates: latitude `[-90, 90]`, longitude `[-180, 180]`.
- Update both boxes only after the entire clipboard value parses and validates successfully. Invalid clipboard data must leave both existing entries unchanged.
- On success, replace both coordinate values, clear any prior coordinate validation message, focus the Search button or next logical control, and show a brief nonmodal status such as “Coordinates pasted.” Do not automatically start a search.
- On failure, show a concise message with the expected example format and keep the user's current coordinate values intact.
- Handle an empty or unavailable clipboard without crashing. Clipboard access and all Tkinter field updates must occur on the Tk main thread.
- Provide a keyboard mnemonic/focus access for the button. Standard direct paste into either individual field must continue to work.

### Center: results

- `ttk.Treeview` with vertical and horizontal scrolling.
- Initial columns: Year, Date, Agency, Project, Roll, Frame, Scale, Type, Preview, Download, ID.
- Oldest-to-newest ordering by default.
- Clickable headings for sort; stable toggling between ascending and descending.
- Single selection by default.
- A clear count such as “142 matches; showing first 100.”
- Preserve selection when a local sort is applied, when possible.

### Side or bottom: selected-frame details and preview

- Read-only details panel for normalized and extra metadata.
- **Quick Preview** pane with aspect-preserving fit and loading state.
- An **Open Best Image in Viewer** button driven by downloadable-product availability, not browse availability.
- Product/resolution summary plus **Download / Save As** button.
- Open in EarthExplorer button/link.

### Viewer window behavior

- Title the window `Aerial Archive Explorer — Viewer` and include the selected frame's year and display/entity ID when available.
- Identify the opened product in the viewer, including medium/high resolution or DPI and file size when known. If the source is only a browse fallback, display a prominent **Browse quality** label.
- Initially fit and center the complete image within the canvas without upscaling beyond a sensible default unless needed for usability.
- Provide visible **Zoom +**, **Zoom −**, **Fit to Window**, **Save Image**, and **Close** controls.
- **Save Image** performs a safe Save As from the completed cached product. It must not trigger another API download. Disable it for a browse-only fallback unless saving browse imagery is deliberately supported and labeled.
- Mouse wheel/trackpad scrolling over the canvas zooms in or out. Support Windows/macOS `<MouseWheel>` event deltas and Linux `<Button-4>`/`<Button-5>` events.
- Wheel zoom must stay anchored to the image point under the mouse cursor; that point should remain visually stationary as scale changes.
- **Zoom +** and **Zoom −** zoom around the canvas center using consistent increments, approximately `1.2×` per step.
- Clamp zoom to safe minimum and maximum values; use a maximum around `10×` unless real-image testing justifies another documented limit.
- Pressing and holding the left mouse button on the image, then dragging, pans the image in direct correspondence with cursor movement.
- Change the cursor to a move/pan cursor while dragging and restore it on release, cancellation, focus loss, or viewer close.
- **Fit to Window** recomputes scale and offsets for the current canvas dimensions and recenters the full image.
- Resizing the viewer preserves the current view during ordinary resize; Fit to Window remains an explicit reset. The initial delayed layout must still produce a correct fit.
- Show the current zoom percentage and concise help such as “Drag to pan. Wheel to zoom.”
- Keep image edges and empty canvas space visually distinct. Do not tile, stretch, georeference, or overlay the image on a map.
- Keyboard access: `+`/`=` zooms in, `-` zooms out, `0` fits to window, and Escape closes the viewer when it is safe to do so.
- Bind wheel and keyboard events to the viewer/canvas scope and unbind them on close; do not use lingering global bindings that interfere with the main window.
- The viewer must remain responsive with large images and rapid wheel input. Retain a strong reference to the active Tk image to prevent blank-canvas garbage collection.

### Status area

- Human-readable state: Ready, Signing in, Searching, Loading preview, Preparing download, Downloading, Complete, Cancelled, or Error.
- Determinate progress when byte total is known; indeterminate otherwise.
- Cancel button for active search/preview/download where cancellation is safe.

### Interaction rules

- Keep the window responsive during every network request and download.
- Disable only actions that conflict with current work; do not freeze the entire form unnecessarily.
- Double-clicking a result may show Quick Preview but must never start a full-product download without confirmation.
- Search results remain visible if a preview or download fails.
- A new search clears stale preview/product state only after input validation succeeds.
- Do not show raw JSON or a traceback in ordinary dialogs. Provide a collapsible/copyable diagnostics area if implemented, with secrets and signed URLs redacted.
- Use sensible minimum window dimensions, resizing weights, high-DPI-friendly spacing, and no color-only status cues.

## Networking and responsiveness rules

- Set both connection and read timeouts; no request may wait forever.
- Use HTTPS only and normal certificate verification. Do not add a “disable TLS verification” workaround.
- Include an honest application `User-Agent`.
- Retry only transient failures such as selected timeouts, HTTP 429, and 5xx responses.
- Use bounded exponential backoff with jitter and honor `Retry-After` when present.
- Do not blindly retry authentication failures, invalid requests, or permanent 4xx responses.
- Limit concurrent API/browse activity. One active search and one active download is sufficient for version 1.
- Worker threads should be daemonized or shut down cleanly. Closing the app must not leave it hung waiting on a worker.
- Check a cancellation event between pages, poll attempts, and streamed chunks.

## Error handling and user messages

Every failure should answer: what happened, whether existing work is safe, and what the user can do next.

Map errors into stable categories:

- **Input:** invalid coordinate/radius/date. Focus the offending field and explain its valid range.
- **Authentication/access:** rejected token, missing M2M permission, or expired session. Clear the in-memory session key and invite reauthentication; do not clear the user's search inputs.
- **No matches:** normal empty state, not an error. Suggest a slightly larger radius while noting coverage is incomplete.
- **Network:** offline, DNS, TLS, timeout, throttling, or USGS outage. Preserve inputs/results and provide Retry.
- **API/schema:** non-JSON response, error envelope, missing expected data, or ambiguous dataset. Show a concise message and safe diagnostics.
- **Preview:** unavailable/unsupported/corrupt. Keep metadata and download controls usable.
- **Download preparation:** failed, delayed beyond timeout, or order required. Explain the state and provide EarthExplorer access when possible.
- **Filesystem:** permission denied, disk full, invalid filename, existing destination, or interrupted transfer. Preserve or remove `.part` files according to the user's choice.

Catch exceptions at worker boundaries, translate them once, and always restore the UI from its busy state in a `finally` path. Never use a bare `except`, silently ignore an error, or terminate the process for a recoverable problem.

## Coding conventions

- Target Python 3.11+ unless packaging constraints require another documented baseline.
- Follow PEP 8, use four spaces, meaningful names, type hints, `pathlib.Path`, f-strings, context managers, and narrow exception handling.
- Organize the application file in this order: module docstring; imports; constants; models/exceptions; pure helpers; API client; preview/download helpers; Tkinter application; `main()`; guarded entry point.
- Use `if __name__ == "__main__": main()`.
- Keep functions focused. Extract pure parsing, bounding-box, sorting, filename, and response-normalization logic from widget callbacks.
- Prefer explicit state transitions over scattered boolean flags.
- Use `logging` for redacted diagnostic events. Never log tokens, session keys, full signed download URLs, or sensitive headers.
- Comments explain constraints and intent, not obvious syntax.
- User-visible text should be plain language, consistent, and centralized where practical.
- Avoid module-level mutable state and direct network calls from callbacks.
- Do not mutate raw API dictionaries throughout the UI.
- Do not use `update()` loops, blocking `join()` calls on the Tk thread, or widget access from workers.

## Testing strategy

Tests must not depend on live USGS availability, a real token, or a graphical display unless explicitly marked as manual/integration tests.

### Unit tests

Cover at minimum:

- valid and invalid coordinate/radius parsing, including NaN/infinity;
- clipboard coordinate parsing for Google Maps comma-separated values, whitespace-separated values, Google Earth KML `longitude,latitude[,altitude]`, extra whitespace, negative coordinates, empty text, malformed text, ambiguous/multiple pairs, and out-of-range values;
- atomic Paste Coordinates behavior: both fields change on success and neither changes on failure;
- bounding-box conversion at the equator, high latitudes, poles, and antimeridian;
- dataset matching for zero, one, and ambiguous candidates;
- response-envelope success and error parsing;
- metadata normalization with complete, sparse, reordered, and unfamiliar fields;
- date parsing and deterministic oldest-first ordering with unknown dates;
- pagination, total cap, deduplication, and cancellation;
- download-option classification;
- best-product ranking: immediately available high resolution over medium resolution, available medium resolution over browse fallback, and exclusion of paid/order-only products from automatic selection;
- viewer-cache identity, cache-hit reuse, no duplicate transfer, Save Image copying from cache, and separation of cached versus user-saved paths;
- filename sanitization/path traversal prevention;
- redaction of tokens, API keys, and signed URLs;
- atomic `.part` completion and cleanup behavior.

Inject the HTTP transport, clock/sleep function, and filesystem destination decisions where necessary. Use recorded synthetic fixtures scrubbed of credentials and expiring URLs.

### API contract tests

- Maintain small fixture responses representative of `login-token`, `dataset-search`, `scene-search`, `download-options`, `download-request`, and `download-retrieve`.
- Verify request shapes without asserting volatile IDs or URLs.
- Optional live tests require environment-provided credentials, are skipped by default, make a tiny search, and never download a large product.
- Never commit live tokens, session keys, or private response data.

### UI tests and manual checks

At minimum manually verify:

- clean launch and clear setup guidance;
- invalid-input focus/message behavior;
- centered Paste Coordinates placement beneath the inputs, successful Google Maps and Google Earth KML coordinate paste, correct KML longitude/latitude reversal, unchanged fields after invalid clipboard data, and no automatic search;
- responsive window during slow simulated requests;
- no-results and many-results states;
- sortable columns and stable selection;
- missing and successful Quick Preview states, including a downloadable product with no browse image;
- Open Best Image confirmation/product summary, transfer progress, cancellation, cache reuse on reopen, and Save Image without a second network request;
- paid/order-only high resolution falling back to the best immediately downloadable scan and offering EarthExplorer without placing an order;
- viewer initial fit, Zoom +/− controls, cursor-anchored wheel zoom on macOS/Windows/Linux event forms, zoom limits, left-drag pan offsets/cursor restoration, Fit to Window, resize behavior, keyboard shortcuts, and clean close/reopen;
- product selection and download destination confirmation;
- progress, cancellation, retry, existing-file handling, and app close during work;
- display at common scaling settings on each supported OS.

Use a fake API client to exercise UI states deterministically. Keep the actual Tk root creation out of import-time code.

## Phased implementation

Each phase should leave the app runnable and should add tests for its new pure or network-facing logic.

### Phase 1: shell and search model

- Create the one-file app skeleton and responsive layout.
- Implement manual input validation, Paste Coordinates clipboard parsing and field population, radius-to-MBR conversion, state/status handling, and fake results.
- Add pure-function tests.
- Acceptance: invalid input is clear, table sorting works, and the UI remains responsive with a deliberately slow fake search.

### Phase 2: authentication and live catalog search

- Implement safe JSON transport, `login-token`, logout, dataset discovery, scene search, pagination, normalization, and redacted logging.
- Initially show text metadata only.
- Acceptance: a real approved account can search known coordinates; no-match and API-error cases are understandable; secrets never appear in logs.

### Phase 3: preview and details

- Add selection-driven details, browse discovery/fetch/decode, placeholder states, cancellation, and stale-response suppression.
- Add the dedicated interactive viewer using a bundled test fixture/local image first: initial fit, button and cursor-centered wheel zoom, left-drag pan, keyboard controls, throttled visible-region rendering, Save Image plumbing, and clean resource release.
- Acceptance: switching selections quickly cannot display the wrong Quick Preview, a bad browse does not affect results, and the viewer stays responsive while zooming and panning a large local test image.

### Phase 4: product selection and download

- Add download options, deterministic best-product ranking, immediate and prepared-download workflows, bounded polling, session cache, save dialog, streamed `.part` file, progress, cancellation, and atomic completion.
- Connect **Open Best Image in Viewer** to the cached full-product workflow and **Save Image** to reuse the same completed bytes.
- Acceptance: the best immediately available product downloads once and opens locally; reopening and saving it cause no second network transfer; paid/order-only products require explicit external action; cancellation and disk/network failures leave a clear recoverable state; existing files are never silently overwritten.

### Phase 5: hardening and distribution

- Complete automated tests and manual cross-platform checks.
- Improve accessibility, DPI/layout behavior, retry messaging, diagnostics, and shutdown.
- Add concise setup/run/package documentation. Optionally package the same single source file into an OS executable without changing the architecture.
- Acceptance: a new user can obtain M2M access guidance, launch the app, find frames, use Quick Preview, open the best available scan in the viewer, and save it without reading source code.

## Definition of done

The initial release is done only when:

- the runnable production logic is contained in one Python file;
- entered coordinates are validated and searched against the dynamically identified Aerial Photo Single Frames collection;
- results include date/year and useful available metadata, sorted oldest first by default;
- Quick Preview and product/download availability are represented honestly;
- Open Best Image in Viewer uses the highest-resolution immediately downloadable scan, never silently initiates a paid/order-only request, and clearly labels any browse-quality fallback;
- a completed viewer download is reused by reopen and Save Image rather than downloaded again;
- immediate and prepared downloads are handled without blocking Tkinter;
- credentials and signed URLs are protected;
- cancellation, timeouts, partial files, API errors, and missing metadata are safe;
- core pure logic and API workflows have deterministic tests; and
- no georeferencing, geofencing, GIS, or unrelated imagery-search scope has slipped into version 1.

When API behavior conflicts with this plan, preserve the user-facing goal and security rules, consult current official USGS documentation, add a redacted fixture/test for the observed response, and document the compatibility change.
