# HDA — Humanitarian Data Access

HDA is agent-oriented, scoped, traceable access to public/open humanitarian and
relevant contextual data. It is structured-first, not structured-only: it
prefers validated structured interfaces while retaining bounded discovery and
acquisition paths for authoritative source material.

The agent workflow is:

`question → HDA Skill → source registry → access-ready runtime → bounded authoritative evidence → model reasoning`

In the registry, L1 means catalogued and validated interface knowledge. L2,
recorded as `access_ready`, means that the interface has been operationally
proven. Agents should route only through registry-confirmed access-ready
capabilities. The current set is HDX CKAN, OCHA COD-AB through HDX, HDX HAPI,
OCHA HPC public, UNHCR Refugee Statistics, GDACS REST, UNICEF public SDMX,
WHO public WHDH/xMart OData, UNFPA Population Data Portal public ArcGIS REST,
and WHO Disease Outbreak News public OData.

HDA is licensed under the Apache License 2.0; see `LICENSE`.

## Development provenance

HDA is 100% vibe-coded and human-guided by a humanitarian practitioner with
field and global humanitarian experience.

## Requirements

- Python 3.9 or newer; HDA uses only the Python standard library.
- Network access to the selected public source for discovery or live queries.
- No pip installation is required.
- A skill-capable agent environment is optional and required only for
  Skill-driven routing.

The product directory may be placed anywhere. Run commands directly from its
root; no fixed installation path is required.

## Commands

- `hdx_discover.py` — discover HDX datasets and resources.
- `hdx_acquire_inspect.py` — inspect or explicitly acquire HDX resources.
- `hda_query.py` — query locally acquired tabular data.
- `hda_hapi.py` — query HDX HAPI.
- `hda_hpc.py` — query OCHA HPC public data.
- `hda_unhcr.py` — query UNHCR Refugee Statistics.
- `hda_gdacs.py` — query GDACS.
- `hda_geo.py` — resolve COD country identity and obtain real administrative
  boundary GeoJSON with source lineage.
- `hda_unicef_sdmx.py` — bounded native public SDMX access, proved for the
  representative flow `UNICEF:IMMUNISATION(1.0)`; not all UNICEF datasets have
  been tested.
- `hda_who_xmart.py` — bounded WHO public xMart OData access for the proved
  representative route `FLUMART/VIW_FNT`; this is not a universal WHO health
  or outbreak client.
- `hda_unfpa_arcgis.py` — bounded UNFPA Population Data Portal public ArcGIS
  REST access, with maternal mortality ratio as the representative proof; not
  all UNFPA indicators have been tested.
- `hda_who_don.py` — bounded structured collection of WHO Disease Outbreak News
  publications. DON is selective and non-exhaustive, and its epidemiological
  narrative remains authored content; HDA does not convert that prose into a
  normalized case/death database.

`source_registry.json` records source availability, interface status, access
classes, and known constraints. It is the authoritative source-availability
surface.

HDA-native structured sources may not fully establish some current
epidemiological questions. Authoritative document or external fallback may
still be required; HDA makes no claim of comprehensive epidemiological
surveillance.

Routine queries return bounded JSON to stdout and do not silently save query
results. Durable output remains available through explicit `--output` options,
or `--output-dir` for `hda_query.py`. HDX acquisition requires an explicit
output path because preserving selected source bytes is its purpose. Durable
outputs never replace an existing destination by default. Use `--force` only
when deliberate replacement is intended; HDX acquisition applies the decision
to both the acquired bytes and their `.acquisition.json` sidecar.

Remote inputs and redirects are restricted to HTTP(S). API response bodies are
bounded at 16 MiB. HDX acquisition defaults to 64 MiB; disposable inspection to
1,000,000 bytes; local inspection and local query inputs to 64 MiB; COD-AB
archives to 256 MiB; and the selected decompressed GeoJSON member to 512 MiB.
CSV fields have a finite default parser limit of 1 MiB. The relevant CLI limit
option may be increased deliberately for an expected larger input; record and
pagination ceilings remain enforced by each source command.

For commands that expose `--timeout`, the value controls socket connection and
read inactivity. It is not a strict total end-to-end deadline for an operation
that may perform multiple requests or continue receiving data.

See `CHANGELOG.md` for release history.

## Expose the agent Skill

The canonical Skill source is
`skill/humanitarian-data-access/SKILL.md`. Copy it into the recipient agent's
skill directory according to that environment's skill-discovery convention.
For an OpenClaw workspace:

```sh
mkdir -p "$OPENCLAW_WORKSPACE/skills/humanitarian-data-access"
cp skill/humanitarian-data-access/SKILL.md \
  "$OPENCLAW_WORKSPACE/skills/humanitarian-data-access/SKILL.md"
```

Credentials, when an interface requires them, belong in external deployment
state. HDA ships registry descriptions of credential requirements, not actual
credentials. The recipient supplies its own local credentials where the
registry says they are required; no distributed credential is expected.

## Inspect and validate

Validate JSON and Python syntax from the product root:

```sh
python3 -m json.tool source_registry.json >/dev/null
python3 -m py_compile *.py
```

Inspect interfaces that are access-ready or require credentials:

```sh
python3 - <<'PY'
import json
r = json.load(open("source_registry.json"))
for source_id, source in r["sources"].items():
    for interface in source["interfaces"]:
        if interface["l2_status"] == "access_ready" or interface["credential_nature"] != "none":
            print(source_id, interface["interface_id"], interface["l2_status"], interface["auth_class"])
PY
```

## Smoke verification

Run one bounded anonymous query from the product root:

```sh
python3 hda_unhcr.py query countries --max-records 1 --stdout-records 1
```

For geographic access, `resolve` returns a bounded country identity and
`boundary` writes one requested administrative level as GeoJSON:

```sh
python3 hda_geo.py resolve LBN
python3 hda_geo.py boundary LBN --admin-level 0 --output /tmp/lbn-admin0.geojson
```

Geographic output preserves the source CRS declaration and does not silently
assert transformation or equivalence to another CRS. `hda_geo.py` does not
perform final map styling or rendering; downstream GIS or rendering tools own
that separate step.

Public distribution excludes private development history, credentials, and
research artifacts. See `DISTRIBUTION.md` for the exact clean-product
allowlist.
