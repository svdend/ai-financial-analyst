# Tableau Workbook — Source

`PANW_Dashboard.twb` is the canonical, human-readable XML source of the
published Tableau Public dashboard. Edits to the dashboard layout,
calculated fields, filters, and captions should be made here (or in
Tableau Desktop / Tableau Public web authoring) and committed to git.

## Files

| File | Tracked? | Purpose |
|------|----------|---------|
| `PANW_Dashboard.twb` | yes | Workbook XML — diff-friendly, source of truth for code review |
| `PANW_Dashboard.twbx` | yes | Packaged workbook (zip of `.twb` + bundled `.hyper` extract). This is what gets uploaded to Tableau Public — `.twb` alone won't render without the data extract |

Both formats are tracked because the upload is performed by the Tableau
Public account holder, who needs the `.twbx` to publish. Reviewers can
diff the `.twb` for code review; the `.twbx` is the deliverable.

## Republishing to Tableau Public

After editing `PANW_Dashboard.twb`, regenerate the `.twbx`:

```bash
# From dashboard/tableau_workbook/
zip -r PANW_Dashboard.twbx "PANW Dashboard.twb" Data/
```

Then upload — two options:

1. **Tableau Desktop Public Edition (preferred — overwrites in place):**
   - Open `PANW_Dashboard.twbx` in Tableau Desktop Public Edition (free
     download)
   - `File → Save to Tableau Public` and sign in as `sid.den`
   - This overwrites the existing viz at the same URL, preserving views/
     favorites
2. **Tableau Public web upload (creates a new viz at a new URL):**
   - Sign in to Tableau Public as `sid.den`
   - Profile page → "Upload Workbook" (top right)
   - Pick `PANW_Dashboard.twbx`

## Day 1 changes (bead ai-financial-analyst-7gp)

- Removed GAAP Net Margin from the **Margins %** sheet (the calc field is
  preserved at the data-source level for future reuse). See
  `Tableau_Setup.md` §Sheet 2.
- Updated dashboard footer caption (main + phone layouts):
  `Data through FY2025 Q3` → `Data through FY2026 Q2 (10-Q filed 2026-02-18)`.
