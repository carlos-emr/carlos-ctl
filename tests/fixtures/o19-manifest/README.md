# OSCAR 19 import manifests — test snapshot

These three files are a **snapshot** of the manifests the `carlos-emr`
package ships under `/usr/share/carlos-emr/o19-manifest/`. They are
generated in the `carlos-emr/carlos` repository by
`scripts/migration/o19/generate_manifests.py` (from the OSCAR 19 schema, the
CARLOS Flyway set and the curated overlays there) and are NOT edited by
hand here.

The test suite loads them through `CARLOS_CTL_O19_MANIFEST_DIR` (set by
`tests/__init__.py`) so the ETL, roles and preflight tests run without a
carlos checkout or an installed `carlos-emr`. When `CARLOS_SRC` points at a
carlos checkout, the suite uses that checkout's
`debian/assets/o19-manifest/` instead, which is how carlos's own CI runs
these tests against the manifests it is about to ship.

Refresh the snapshot from a carlos checkout when the manifest **format**
changes, or when a content change matters to a test here:

```bash
cp /path/to/carlos/debian/assets/o19-manifest/*.json tests/fixtures/o19-manifest/
```

Content-level integrity of the manifests (every table classified, every
column grounded in the CARLOS schema) is tested in carlos, next to the
generator: `scripts/migration/o19/tests/`.
