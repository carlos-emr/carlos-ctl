# CDS export command contract

The implementation provides `carlos-ctl cds-export` beside `cds-import`.
The matching Java 25 engine uses the same clinical job service for foreground
execution and durable asynchronous browser work. OntarioMD 5.2 is registered;
PCON is not advertised.

The actual root-delegated CLI has exported a native chart with no import metadata,
observed its durable job from independent processes, downloaded the encrypted
artifact, and refused an existing output destination. An independent recipient
recovered the exact validated ZIP and all thirteen expected clinical facts.
Status and diagnostics remained available with invalid active export settings.
This establishes that installed checkpoint, not browser end-to-end qualification.
The application PR remains a draft while authenticated browser and final
integration checks are completed:
[carlos-emr/carlos#5706](https://github.com/carlos-emr/carlos/pull/5706).

Import and export share `cdsruntime.py` for Java launch selection, bounded capability
probes, root-issued delegation tickets, signal forwarding and process outcomes.
Clinical mapping, permissions, snapshots, jobs and package publication belong to
the Java application. Existing import argument/protocol semantics remain unchanged.

The export main class is
`io.github.carlos_emr.carlos.demographic.cdsexport.CdsExport`. Its capability response
must identify `engine` as `cds-export`, protocol 1, Java 25, a heap budget in
128–16384 MiB, and operations `export`, `status`, `resume`, `cancel`, `report` and
`download`. A new export additionally needs its exact profile in `qualifiedProfiles`.
Detached submission and submission lookup require literal `true` capability flags.
A mismatched or unqualified engine is refused before a delegation ticket is created.

The invoking Unix identity is independently established by the root supervisor.
Its ticket lives in `/run/carlos-cds-export-delegations`, binds the exact Java
arguments/provider/operation and expires within 300 seconds. The environment variable
`CARLOS_CDS_DELEGATION_TICKET` carries its path, not authority by itself. The Java
export verifier must require that exact export directory and verify root ownership,
permissions, process identity, expiry, nonce and argument digest. An import ticket
cannot supply export authority. The service process runs as `carlos`.

New requests require an explicit provider, profile, encryption recipient registry key,
verified audit-plan registry key, and either physician roster IDs or a protected patient
selection file. Categories default to all; an explicit list must include Demographics.
Physician rosters include all statuses. The Java engine validates a protected UTF-8
JSON selection file with the exact shape `{"version":1,"patients":[27,28]}`. IDs must be
positive, unique integers, with at most 10,000 patients. Paths must remain inside
`cds.export.selectionRoot` and meet the Java ownership/permission/no-symlink checks; a
Python path string alone does not validate content. Selection bytes are captured before
durable acceptance, so later path changes cannot replace the accepted patient IDs.

Existing jobs accept their original intent only. Status, resume, cancel, report and
download cannot replace selection, categories, profile or recipient. Downloads require
an opaque artifact ID and an owned, new protected destination. `--detach` is accepted
only for new submission/resume; acceptance must report waiting when no persistent
compatible worker exists. Stopping a foreground wait is distinct from durable cancellation.

Exit 0 means a completed clinical export or a successful observation/control request;
an observation exit does not establish clinical completeness. Exit 1 is an argument
error, 2 a configuration/runtime/compatibility failure, 3 denied authority, 4 an incomplete,
failed or review-required result, and 130 interrupted execution. Python preserves the
engine's outcome and never constructs a clinical success result itself. CLI and browser
use the same completion predicate: READY, an available artifact, known positive patient
total, every selected patient processed and no blocked patients. Waiting preserves an
unknown total; accepted intent or exit-zero observation alone cannot imply completeness.
