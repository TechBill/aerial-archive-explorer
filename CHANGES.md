# Planned Changes

## UI changes

- Remove the **Quick Preview** button.
- Remove the entire lower-resolution, unrectified **Quick Preview** browse panel.
- Replace the bottom action controls with three buttons:
  - **View Aerial**
  - **Download**
  - **Open in EarthExplorer**
- Continue displaying the **Best available** product name and file size for the selected frame.
- Update result labels and supporting text to state that every displayed frame covers the searched coordinate.

## Search and filter changes

- Keep the USGS radius search as the initial catalog candidate search for **Aerial Photo Single Frame** records.
- After receiving candidates, build a footprint polygon from each record's four corner coordinates.
- Test the entered coordinate against each footprint using a point-in-polygon check.
- Retain only records whose footprint contains the searched point. Treat a point on the polygon boundary as covered.
- Discard neighboring frames that intersect the search radius but do not contain the exact coordinate.
- Do not present incomplete or invalid corner geometry as confirmed coverage; omit it from the filtered results or flag it separately for diagnostics.
- Preserve the original record metadata and product options for every retained frame.

## Acceptance criteria

- No Quick Preview button or Quick Preview browse panel is visible.
- The bottom action area contains only **View Aerial**, **Download**, and **Open in EarthExplorer**.
- The selected frame shows its best available product and file size when those values are available.
- The USGS radius query supplies candidates, but does not by itself determine displayed results.
- Every displayed result has a valid four-corner footprint that contains the searched coordinate, including boundary points.
- Frames that are merely nearby or overlap the radius without covering the coordinate are excluded.
- UI wording clearly communicates that displayed results cover the exact searched coordinate.
- Point-in-polygon behavior is covered by tests for inside, outside, boundary, invalid-footprint, and overlapping-frame cases.
