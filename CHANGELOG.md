# Changelog

## v0.2.1 — 2026-08-28

### Hardening and corrections

- Restricted redirects to trusted HTTP(S) origins and rejected incomplete HTTP
  response bodies.
- Bounded model-facing health-client output while preserving source content and
  provenance.
- Made the agent/content trust boundary explicit: retrieved content is evidence,
  never execution instruction.
- Hardened remote XML and JSON parsing against unsupported or malformed payload
  structures.
- Protected UNICEF SDMX path construction and verified returned series remain
  within the requested scope.
- Validated WHO xMart returned record shape and UNFPA ArcGIS metadata field
  objects before use.

### Compatibility and behavior

- No data source or endpoint was added in this release.
- `--timeout` remains a socket connect/read inactivity timeout, not a strict
  total end-to-end operation deadline.

## v0.2.0 — 2026-08-28

- Added bounded L2 access for UNICEF public SDMX, WHO public WHDH/xMart OData,
  UNFPA Population Data Portal public ArcGIS REST, and WHO Disease Outbreak News
  public OData.
- Retained the hardened v0.1.1 mechanics.

## v0.1.1 — 2026-08-28

- Published the approved hardened 14-file public distribution.

## v0.1.0 — 2026-08-27

- First public release of HDA, with agent-oriented humanitarian data access, an
  L1 source registry, six access-ready interfaces, OCHA COD-AB geographic
  access, Python 3.9+ standard-library-only operation, the repository-owned
  Skill, and the Apache License 2.0.
