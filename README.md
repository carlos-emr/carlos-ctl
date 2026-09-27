# carlos-ctl

`carlos-ctl` is the administration command for a [CARLOS EMR](https://github.com/carlos-emr/carlos)
host installed from the Debian packages: the deployment check, service
lifecycle, schema migration and database accounts, certificates, the
ModSecurity front door, backups and restore drills, and the experimental
OSCAR 19 clinic import. Every routine job is a `carlos-ctl` verb, run with
`sudo`; `carlos-ctl check` is the one command to remember.

It ships as its own package, `carlos-ctl_<version>_all.deb`, which the
`carlos-emr` application package depends on. Through carlos-emr
2026.08.0-alpha15 the same code lived inside `carlos-emr`
(`debian/assets/carlos_ctl` in [carlos-emr/carlos](https://github.com/carlos-emr/carlos));
the split is [carlos-emr/carlos#4001](https://github.com/carlos-emr/carlos/issues/4001),
and this repository's history is that directory's history, rewritten to
this layout.

- Operator documentation: [docs/carlos-ctl.md](docs/carlos-ctl.md) and
  `man carlos-ctl` ([man/carlos-ctl.8](man/carlos-ctl.8)).
- Installing CARLOS: the application's
  [docs/install-deb.md](https://github.com/carlos-emr/carlos/blob/develop/docs/install-deb.md).

## How the two packages fit together

| | `carlos-emr` | `carlos-ctl` |
|---|---|---|
| Built from | [carlos-emr/carlos](https://github.com/carlos-emr/carlos) | this repository |
| Installs | the application, its Flyway migrations (`/usr/share/carlos-emr/schema`), helper scripts (`/usr/lib/carlos-emr`), configuration skeletons, the OSCAR 19 import manifests (`/usr/share/carlos-emr/o19-manifest`) | the CLI (`/usr/lib/carlos-ctl`, `/usr/sbin/carlos-ctl`) and its man page |
| Depends on | `carlos-ctl (>= …)` — its maintainer scripts call `init-config`, `db-users`, `db-migrate`, `bootstrap-admin`, `demo-data`, `db-apply-settings`, `db-rename-schema`; its boot-time unit calls `finish-install --boot` | never on `carlos-emr` (that would be a cycle); every verb checks at run time and says so when the application package is absent |

The CLI is a driver over what `carlos-emr` ships: `db-migrate` applies the
migrations of the installed WAR through the runner jar `carlos-emr`
provides, and the OSCAR 19 importer reads the manifests `carlos-emr`
generates beside its schema (checking their `format` before trusting a
key). That is what lets the two version independently — a schema
migration never needs a CLI release, and a CLI fix ships without rebuilding
the application. When a new `carlos-emr` needs a new verb or flag it raises
its `Depends: carlos-ctl (>= N)`; when a CLI release changes or drops a verb
an older `carlos-emr` calls, it adds `Breaks: carlos-emr (<< M)`.

Every carlos release re-attaches the pinned `carlos-ctl` package
(`debian/carlos-ctl.pin` in carlos), so one release page carries the whole
install. Operators install and upgrade with the same command as before,
with one more file:

```bash
sudo apt install --no-remove ./carlos-emr_<version>_amd64.deb \
                 ./carlos-ctl_<version>_all.deb \
                 ./carlos-emr-drugref_<version>_all.deb \
                 ./carlos-emr-eform-renderer_<version>_all.deb
```

## Development

```bash
python3 -m unittest discover -s tests -t .       # the unit suite (~15 s)
ruff check carlos_ctl tests
groff -ww -z -Tutf8 -man man/carlos-ctl.8        # the man page, warnings are errors
dpkg-buildpackage -us -uc -b                     # the package (runs both gates)
```

The suite runs against a snapshot of the OSCAR 19 manifests under
`tests/fixtures/o19-manifest/` (see its README). Tests that pin this CLI to
the application itself — the login-name rule, the Flyway set, the packaging
that ships the o19 fixups, the operator runbook — read a carlos checkout
through `CARLOS_SRC` and skip without one; carlos's CI runs this suite with
`CARLOS_SRC` set against the release it pins:

```bash
CARLOS_SRC=/path/to/carlos python3 -m unittest discover -s tests -t .
```

`carlos_ctl/o19_preflight.py` is copied alone to a 2014-era OSCAR 19 server
and is written for Python 3.4 at 79 columns on purpose; `ruff` is told to
leave it alone.

### Releases

Tag `MAJOR.MINOR.PATCH` (a pre-release as `1.0.0-rc1`; the workflow maps
`-` to `~` for dpkg) on a commit whose `carlos_ctl.__version__` matches.
The release workflow builds the `.deb` in an `ubuntu:26.04` container,
attaches it with its `.sha256` and a build-provenance attestation, and
publishes the release. Verify a download with:

```bash
sha256sum -c carlos-ctl_<version>_all.deb.sha256
gh attestation verify carlos-ctl_<version>_all.deb --repo carlos-emr/carlos-ctl
```

## Relationship to carlos-podman

[carlos-podman](https://github.com/carlos-emr/carlos-podman) ships a
different `carlos-ctl` (project `carlos-podman-ctl`, same command name,
same language) for its rootless-podman deployment; the verb sets overlap
where the two deployments share a concept. This repository is the
single-host Debian package's tool; unifying the two trees here is a
separate, later issue.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
