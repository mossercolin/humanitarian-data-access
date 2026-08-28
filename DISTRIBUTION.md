# HDA public product boundary

Build an HDA product directory from empty by copying only this allowlist:

- `LICENSE`
- `README.md`
- `DISTRIBUTION.md`
- `source_registry.json`
- `hdx_discover.py`
- `hdx_acquire_inspect.py`
- `hda_query.py`
- `hda_hapi.py`
- `hda_hpc.py`
- `hda_unhcr.py`
- `hda_gdacs.py`
- `hda_geo.py`
- `hda_http.py`
- `hda_unicef_sdmx.py`
- `hda_who_xmart.py`
- `hda_unfpa_arcgis.py`
- `hda_who_don.py`
- `skill/humanitarian-data-access/SKILL.md`

This allowlist contains exactly 18 files. The shared `hda_http.py` module is
required by the hardened runtime command modules and therefore ships as part of
the public product.

The Python files are the active runnable command surface. They require Python
3.9 or newer, use only the standard library in this candidate, require no pip
installation, and require network access for live interfaces. The registry
provides source and interface availability metadata. The Skill source is
`skill/humanitarian-data-access/SKILL.md`; expose it through the recipient
agent environment's normal skill-discovery procedure described in `README.md`.

The public runtime accepts only HTTP(S) remote URLs, bounds remote bodies,
downloads, local inputs, archives, decompressed GeoJSON members, result counts,
and CSV field sizes, and exposes deliberate size-limit overrides where
appropriate. User-selected durable outputs are no-clobber by default; `--force`
is the explicit replacement option, including the HDX output/sidecar pair.

Do not add Git metadata, development history, tests, prior query outputs,
acquired evidence, generated artifacts, caches, temporary state, credentials,
or private account material to the product export.

These exclusions are by product design. Presentation-specific compilers,
vendored development dependencies, tests, evidence, artifacts, research
material, and private history are not part of HDA's public runtime. No shipped
runtime file imports or invokes excluded vendor content.

Credentials are never shipped. If a registry entry identifies a credentialed
interface, the recipient owns credential acquisition and storage outside the
product export.

A public distribution must be built cleanly from an empty directory by copying
only the positive allowlist. It must not be produced by pruning or publishing
the private development repository or its Git history.

To validate the Skill with the local OpenClaw tooling:

```sh
python3 /path/to/openclaw/skills/skill-creator/scripts/quick_validate.py \
  skill/humanitarian-data-access
```

To expose the Skill, follow the recipient procedure in `README.md`.
