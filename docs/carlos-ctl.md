# carlos-ctl — post-install configuration and administration

`carlos-ctl` is the administration command for a CARLOS EMR server installed
from the Debian packages. It is the one tool an operator needs day to day:
every routine job — health checks, logs, restarts, schema migrations,
certificates, the web application firewall, backups and restore drills — is a
`carlos-ctl` verb, run with `sudo`.

This page is the GitHub-readable companion to the documentation that installs
with the packages (`man carlos-ctl` and
`/usr/share/doc/carlos-emr/README.Debian`), so you can read it *before* you
have a server. For the install itself, start at the application's
[docs/install-deb.md](https://github.com/carlos-emr/carlos/blob/develop/docs/install-deb.md).

The command ships in its own package, `carlos-ctl` (this repository), which
`carlos-emr` depends on; through carlos-emr 2026.08.0-alpha15 it was part
of `carlos-emr` itself. The two version independently: the CLI is a driver
over what `carlos-emr` installs (its Flyway migrations, helper scripts,
configuration skeletons and the OSCAR 19 import manifests under
`/usr/share/carlos-emr/o19-manifest/`), so a schema change ships with
`carlos-emr` and never needs a CLI release. On a host where `carlos-emr` is
not installed every verb refuses with one line saying so, and
`carlos-ctl check` reports both package versions first.

Under the hood the command is a small shim (`/usr/sbin/carlos-ctl`) around a
Python package installed at `/usr/lib/carlos-ctl/carlos_ctl/`. The
[carlos-podman](https://github.com/carlos-emr/carlos-podman) deployment ships
its own `carlos-ctl` (project `carlos-podman-ctl`) with the same name,
language, and overlapping verbs
(`check`, `db`, `db-migrate`, `db-users`, `backup full|verify|status`,
`cert-renew`, `rotate`), so operators can move between the two deployments
without relearning. The old `carlosctl` spelling still works and prints a
deprecation note.

## The one command to remember

```bash
sudo carlos-ctl check
```

`check` probes the whole deployment — services, process ownership, network
exposure, TLS, live WAF blocking, a live DrugRef lookup, schema state, and
backup freshness — and tells you what is wrong in plain language. Run it
after installing, after upgrading, after changing configuration, and first
thing when anything misbehaves.

## The configuration loop

All operator-owned configuration lives under `/etc/carlos-emr/`. The package
writes these files once at install time and **never overwrites them again** —
an upgrade will not undo your edits. The loop is always the same: edit the
file, then run the verb beside it.

| File | What it holds | After editing, run |
|---|---|---|
| `/etc/carlos-emr/carlos-emr.env` | Site settings: host name, listen address, billing province (read-only after install), Java heap, timezone | `sudo carlos-ctl init-config` — re-renders *and applies*: nginx reload, certificate refresh, and it tells you if a restart is also needed |
| `/etc/carlos-emr/carlos.properties` | CARLOS application settings | `sudo carlos-ctl restart` |
| `/etc/carlos-emr/backup.env` | Backup repository and credentials (`RESTIC_REPOSITORY`, `RESTIC_PASSWORD`) | nothing — the next timer run reads it; prove it now with `sudo carlos-ctl backup full` |
| `/etc/carlos-emr/modsecurity/` | WAF policy and site-specific rule exclusions | `sudo carlos-ctl waf reload` |
| `/etc/carlos-emr/tomcat/` | Tomcat server configuration | `sudo carlos-ctl restart` |

Notes a first-time operator should not learn the hard way:

- `carlos-emr.env` is parsed by systemd, **not** by a shell: no command
  substitution, no variable expansion, and values with spaces need quotes.
- Upgrades never add new keys to your copy. After an upgrade, compare with
  the shipped skeleton in `/usr/share/carlos-emr/skel/` to see what is new.
- `backup.env` is deliberately unreadable by the application account — that
  separation is what stops a compromised EMR from deleting or decrypting its
  own backups. **Copy `RESTIC_PASSWORD` somewhere off the host**: without it
  every backup the machine has ever written is permanently unreadable.

## Post-install configuration, in order

The first-hour walkthrough in [docs/install-deb.md](https://github.com/carlos-emr/carlos/blob/develop/docs/install-deb.md#quickstart--the-first-hour)
covers this end to end; the short version, in the order that retires the most
risk first:

1. **Verify** — `sudo carlos-ctl check`, and confirm the eForm render browser
   (`sudo systemctl status carlos-emr-render-browser`).
2. **Credentials** — `sudo cat /etc/carlos-emr/initial-admin.txt`, log in,
   complete the forced password reset, create real named accounts, disable
   the seeded `carlosdoc` account, delete the file. If the install-time
   replacement was declined or failed, run `sudo carlos-ctl bootstrap-admin`
   before anything else.
3. **Backups offsite** — set an offsite `RESTIC_REPOSITORY` in `backup.env`,
   copy `RESTIC_PASSWORD` off the host, then prove the pipeline:
   `sudo carlos-ctl backup full && sudo carlos-ctl backup verify`.
4. **Real TLS** — once DNS resolves and port 80 is reachable:
   `sudo carlos-ctl cert acme you@example.ca`.
5. **Read the installed reference** —
   `/usr/share/doc/carlos-emr/README.Debian` marks the decisions only an
   operator can make (**DECIDE THIS**): backup custody and log retention.

## Verb reference

What `carlos-ctl --help` prints, grouped the way `man carlos-ctl` documents
it. Everything runs with `sudo`.

### Diagnosis

| Verb | What it does |
|---|---|
| `check` | Full deployment check — start here |
| `status` | systemd status of the EMR and its timers |
| `logs [args]` | The EMR/Tomcat journal (`journalctl -u carlos-emr`); `-f` follows, `-n 200` tails |

### Service control

| Verb | What it does |
|---|---|
| `restart` | Restart the EMR — applies configuration changes; the webapp takes about two minutes to redeploy |
| `start` / `stop` | Start or stop the EMR |

### Database and schema

| Verb | What it does |
|---|---|
| `db [args]` | SQL shell on the EMR database as root (interactive with no args; `-e` and redirects pass through, e.g. `sudo carlos-ctl db < f.sql`) |
| `db-info` | Show the Flyway schema migration state |
| `db-validate` | Verify the schema matches the deployed WAR |
| `db-migrate` | Apply pending migrations — **back up first** |
| `db-baseline` | Adopt an existing pre-Flyway (OSCAR 19 / OpenO) database: reconcile the live schema up to the genesis, prepare the adopted data for the forward migrations, then stamp (`--dry-run`, `--stamp-only`) — see [OSCAR 19 adoption](https://github.com/carlos-emr/carlos/blob/develop/docs/oscar19-legacy-adoption.md) |
| `db-repair` | Fix `flyway_schema_history` after a failed migration |
| `db-apply-settings` | Restart MariaDB if it is not running the settings in the CARLOS drop-in |
| `db-dump` | Consistent dump to stdout |
| `db-users` | (Re)create the databases and least-privilege accounts |
| `demo-data` | Load the fictitious demonstration dataset (~3000 `FAKE-` patients, 60 fake specialists) into an **empty**, freshly migrated database — what answering yes to the installer's demonstration-data question runs; refuses on any database holding patients, and the only removal is `destroy-data` |

### Certificates

| Verb | What it does |
|---|---|
| `cert status` | What certificate is being served |
| `cert selfsigned` | (Re)generate the self-signed certificate |
| `cert acme <email>` | Switch to a Let's Encrypt certificate (needs public DNS + port 80) |
| `cert manual` | Adopt a certificate you placed at `/etc/carlos-emr/tls/` |
| `cert-renew` | What the twice-daily renewal timer runs |

### Web application firewall

| Verb | What it does |
|---|---|
| `waf status` | ModSecurity engine state and file locations |
| `waf tail [lines]` | Recent WAF blocks, human-readable — the first stop when a request vanishes without reaching the application |
| `waf reload` | Apply edited exclusion/policy files |
| `waf detect-only` | Stop blocking (triage only — heed the warning it prints) |
| `waf blocking` | Resume blocking |

### Backups

| Verb | What it does |
|---|---|
| `backup full` | Take a backup now |
| `backup verify` | Restore the newest backup into a scratch database — the proof the pipeline works |
| `backup status` | When backups and restore drills last succeeded |
| `backup snapshots` | List what is in the repository |
| `backup restic <args>` | Raw restic against the configured repository |

### Provisioning

| Verb | What it does |
|---|---|
| `init-config` | Re-render and apply configuration from `carlos-emr.env` (nginx reload, certificate refresh) |
| `bootstrap-admin` | Reset the seeded administrator credential to random values in `/etc/carlos-emr/initial-admin.txt` |
| `rotate` | Rotate every generated database password |

### Decommissioning

Removing or purging the packages **never** destroys clinical data: not the
databases, not the document store, not the backups. Deliberate destruction is
its own explicit command, and it makes you type the host's own configured
name back to it:

```bash
sudo carlos-ctl destroy-data --confirm <server-name>
```

## CDS import

`carlos-ctl cds-import` runs the same CARLOS CDS import engine as the existing
Administration → Data Management → Demographic Import web workflow. It accepts
an XML file, ZIP or directory within the application's approved source root.
It does not reinterpret source records, choose a target patient, or maintain
its own success counters. A compatible CARLOS engine is required; a bounded
capability handshake refuses older/unsupported application combinations before
clinical execution. Java 25 is required, and the Java process runs as `carlos`.

Prepare an access-controlled, immutable source and a verified database/document
backup according to the migration runbook. Configure the approved source
namespace/profile, the operator's delegation and the migration window in CARLOS;
the command does not establish those approvals by itself. Start with a dry run:

```bash
sudo carlos-ctl cds-import --input /var/lib/carlos-emr/import/batch.zip \
  --provider 999998 --source-namespace clinic-source --source-profile omd-5.2 \
  --program 1 --facility 1 --dry-run
```

After resolving preflight findings, invoke with the same reviewed source/options
without `--dry-run`. Namespace and profile are required for a new import; the
namespace identifies the approved vendor/instance rather than an arbitrary
new label to bypass prior-import protection. The engine independently checks
the invoking OS operator, active provider, privileges and program/facility
delegation. `sudo` does not grant clinical attribution by itself.

| Option | Meaning |
|---|---|
| `--input PATH` | Source XML, ZIP or directory; paths with spaces/metacharacters remain literal arguments |
| `--provider ID` | Explicit active CARLOS provider key (for example `999998` or `T099`) with approved operator delegation |
| `--source-namespace KEY`, `--source-profile KEY` | Registered source instance and supported contract; required for a new import |
| `--program ID`, `--facility ID` | Explicit target context where required by the deployment |
| `--dry-run` | Read-only clinical preflight; administrative staging/run evidence may be written |
| `--merge-existing` | Opt in to a proven unique eligible HIN + exact DOB match; default off |
| `--no-match-providers` | Preserve historical source provider identity without name matching |
| `--synthetic`, `--course ID`, `--timeshift DAYS` | Approved synthetic/anonymized workflow; course/nonzero shift require synthetic mode, which cannot merge real charts |
| `--status-submission KEY` | Read-only recovery after acceptance output loss; same verified OS operator and selected provider; unknown outcome remains uncertain, never resubmits |
| `--status RUN_ID`, `--resume RUN_ID`, `--cancel RUN_ID`, `--report RUN_ID` | Mutually exclusive existing-run operations, always with `--provider ID`; source/import options cannot be replaced |

The foreground import command streams the engine's safe progress and waits for
its outcome. It does not capture batch output in memory. The engine retains
durable per-patient/category receipts, warnings, failed/not-started/uncertain
work, checkpoints and protected event/error/completeness files. A heartbeat,
parsed-byte count or command acknowledgement is not proof of clinical completion.

```bash
sudo carlos-ctl cds-import --status RUN_ID --provider 999998
sudo carlos-ctl cds-import --report RUN_ID --provider 999998
sudo carlos-ctl cds-import --resume RUN_ID --provider 999998
```

Exit `0` means a clean complete import, verified no-op or valid dry run. For
`--status`/`--report`, it means that query succeeded: inspect the returned run
outcome to determine clinical completeness. Exit `1` means invalid arguments;
`2` configuration/runtime/protocol failure; `3` authorization denied; `4`
refused, busy, failed, partial, uncertain or otherwise incomplete import.
An interrupted process may exit `130` or the conventional signal status.
A first termination signal is relayed to the engine; a second terminates its
process group. Inspect the durable run after either interruption.
A nonzero result can coexist with committed clinical data. Never automatically
resubmit a failed input; inspect and resume/reconcile the recorded run.
Report-delivery or cleanup failure is separate from the actual clinical commit
outcome and must not be treated as proof of rollback.

Only `/etc/carlos-emr/carlos.properties` is selected by this host adapter; no
credential is placed in argv. The root wrapper records the verified invoking
OS/SUDO identity and creates a root-owned, service-readable attestation under
`/run/carlos-cds-delegations/`. It binds the exact request arguments, operator,
provider/operation, nonce and five-minute expiry. CARLOS independently verifies
the file's trusted ownership/permissions, request digest and explicit
operator/provider delegation; exporting an operator name alone is insufficient.
The root supervisor drops the engine to the service account, relays signals,
inherits console streams without capturing batch output, and removes only its
own ticket after execution. Root-owned expiry cleanup retires orphaned tickets
after abnormal wrapper termination. Inherited JVM-option/classpath overrides
are removed. A bounded 128 MiB probe obtains the launch budget from Java's
actual production-properties loader, including its escaping/separator rules;
Python never independently parses or guesses the effective heap. Configure
`cds.import.cliHeapMiB` in that production file (default 512, integer 128–16384).
This is a process budget, not a guarantee that arbitrary input fits: validation,
payload handling, persistence and reporting must remain bounded by the engine.
Shared admission must reserve this heap plus process overhead across browser
and CLI workers. Do not use a larger heap as a substitute for investigating a
failed import or recovering its durable state.

## Where the rest lives

- `man carlos-ctl` — the same verbs with full detail, `FILES`, and exit
  statuses.
- `/usr/share/doc/carlos-emr/README.Debian` — the operations manual: what
  runs as whom, TLS, WAF tuning and the false-positive workflow, backups and
  point-in-time restore, upgrades, log retention as a compliance decision,
  and troubleshooting.
- [docs/install-deb.md](https://github.com/carlos-emr/carlos/blob/develop/docs/install-deb.md) — installation, the first hour, and
  a first-day troubleshooting table.
