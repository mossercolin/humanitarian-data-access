# Changelog

## v0.4.1

- Clarified the six existing CBPF, FEWS NET, COD-AB geographic, INFORM, UNHCR,
  and WHO Disease Outbreak News client implementations with source-level
  explanatory comments. The comments describe request scope, bounded data
  flow, output limits, provenance, and the limits of each client claim.
- Added a README source-registry glossary distinguishing source and interface
  identity, L1/L2 access status, credential metadata, and HDA retrieval time
  from publisher data times.
- No runtime, registry, source-interface, dependency, or behavior change.

## v0.4.0

- Added bounded native UNFPA maternal-mortality selectors for country, M49, year, and named geography; arbitrary SQL is not exposed.
- Kept attributes-only output by default and added explicit publisher-native geometry retrieval with preserved spatial-reference declarations.
- Documented the single `HDA_ROOT` installation contract for the canonical Skill, clients, and registry.
- Applied a UNFPA-only TLS strict-mode compatibility adjustment while retaining certificate-chain and hostname verification.

## v0.3.2

### Readability, correctness, and bounded I/O

- Improved existing local-query, HDX-discovery, and HDX-acquisition/inspection
  behavior; these corrections do not establish additional source-wide coverage.
- Retained the v0.3.2 release scope and its bounded-I/O behavior.

## v0.3.1 — 2026-09-22

### Skill consumer-contract correction

- Require consuming agents to answer the humanitarian question directly with
  supported retrieved evidence, leading with the requested facts.
- Let material scope, time, and provenance caveats qualify, not replace, the
  supported answer. Keep internal HDA status and metadata internal unless
  requested or needed to explain a material limitation.
- No runtime, schema, source-interface, or registry changes. The public
  distribution remains the same 24-file allowlist.

## v0.3.0 — 2026-09-22

### Capabilities and distribution

- Added bounded public clients for FEWS NET market prices, IFRC GO Events,
  CERF Projects by Year, CBPF PF Project Summary v4, and INFORM country/model
  scores. Each remains limited to its documented route and proof scope.
- The registry now marks 15 interfaces as access-ready; these are dated,
  scoped access proofs, not guarantees of universal coverage or availability.
- Expanded the clean public allowlist from 19 to 24 files to ship those five
  existing clients. Private history, tests, research, evidence, artifacts,
  presentation-specific code, and local deployment state remain excluded.

### Output contracts and corrections

- Standardized bounded stdout handling across the affected access and local
  query commands, with explicit successful omission envelopes for oversized
  results and preserved source/request provenance.
- Included corrections to HDX discovery/acquisition, local tabular queries,
  HAPI, HPC, UNHCR, GDACS, and geographic access.
- Retained source-native identity, geographic and temporal semantics, scoped
  negatives, no-clobber durable outputs, and the external-content trust boundary.
- No new dependencies or installation requirements. Timeout remains a socket
  connect/read inactivity limit, not a total operation deadline.

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
