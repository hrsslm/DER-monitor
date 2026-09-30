# Release checklist — Ontario DER Monitor

Nothing here may look like a finished public product until **every** gate
below is satisfied. The owner flips `PUBLIC_RELEASE_APPROVED` in
`src/config.py` from `False` to `True` only after that. While the flag is
false, every page carries a "DRAFT: not for public release. Data licences
pending." banner and `<meta name="robots" content="noindex">`.

## Gates

- [ ] **LDC polygon licence confirmed.** The service-area polygons
      (KMZ from the OEB capacity map and all derived GeoJSON) have a
      confirmed licence permitting public redistribution, recorded with
      source and licence text.
- [ ] **Node-coordinate file provenance and licence confirmed.** The
      user-supplied `ieso_node_locations.csv` has confirmed provenance
      and a licence permitting public redistribution.
- [ ] **Page 4 rows reviewed.** Every row of
      `data/processed/crosswalk_review_top20.csv` has an `owner_status`
      of `approved`, `pending_owner_review`, or `rejected`; no row is
      still blank.
- [ ] **Commentary written.** `content/commentary_page1.md` through
      `content/commentary_page4.md` contain the author's analysis (no
      remaining `[ANALYST COMMENTARY - TO BE WRITTEN BY AUTHOR]`
      placeholders).
- [ ] **README attribution complete.** All data sources are attributed
      with licences in `README.md` and `docs/DATA_SOURCES.md`.
- [ ] **Repo visibility decision made.** The owner has decided whether
      the repository is public or private, and GitHub Pages sharing
      settings match that decision.

## After the gates

1. Set `PUBLIC_RELEASE_APPROVED = True` in `src/config.py`.
2. Re-run the build workflow (banners and noindex tags are removed).
3. Enable GitHub Pages ("Deploy from a branch: main, /docs") only now.
