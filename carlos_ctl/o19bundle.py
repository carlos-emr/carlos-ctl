# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 CARLOS Contributors
"""Migration bundles and native OSCAR full backups.

SQL parts form a verified source set; identical overlapping tables are restored
once and conflicting overlap is refused. Nested backup documents and auxiliary
artifacts are retained. An optional o19-inputs.json declares ambiguous roles and
the order of full/incremental document archives.

Accepted outer archives are .tar, .tar.gz and encrypted .enc variants. Encryption
uses the documented openssl derivation; decryption and tar checks must succeed.
Members may have safe nested paths. Traversal, links and special files are
refused, and extracted clinic files remain private.
"""

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import tarfile
import zlib
from typing import Dict, List, Optional, Tuple

from .util import die, log, run

GZIP_MAGIC = b"\x1f\x8b"
# Bound format discovery of unrelated backup metadata. Real ON/BC digest
# documents are below 1 MiB; allow substantial clinic-specific schema growth.
DIGEST_DISCOVERY_MAX_BYTES = 64 << 20
# A tar header is 512 bytes; ustar/gnu archives carry "ustar" at offset 257,
# a v7 tar does not — so validity is decided by the header CHECKSUM (bytes
# 148..155, octal sum of the header with the checksum field as spaces),
# which every tar variant carries.
TAR_HEADER_LEN = 512
TAR_CHECKSUM_OFFSET = 148
TAR_CHECKSUM_LEN = 8
TAR_MAGIC_OFFSET = 257
TAR_MAGIC = b"ustar"

# tar -tv mode-letter -> member type; only plain files (and, for the
# documents tar, directories) may be extracted from clinic-supplied input.
TAR_TYPE_FILE = "-"
TAR_TYPE_DIR = "d"
TAR_TYPE_SYMLINK = "l"
TAR_TYPE_HARDLINK = "h"

DEFAULT_CIPHER = "aes-256-cbc"
DEFAULT_DERIVATION = ["-pbkdf2", "-iter", "200000"]

WRONG_KEY_GUIDANCE = (
    "decryption failed. The two usual causes:\n"
    "  1. wrong password;\n"
    "  2. key-derivation mismatch with the openssl that CREATED the bundle\n"
    "     (older openssl defaulted to -md md5 and no -pbkdf2).\n"
    "Remedies: re-check the password, or match the creator's derivation,\n"
    "e.g.  --bundle-openssl-opt -md --bundle-openssl-opt md5\n"
    "(and drop the default -pbkdf2 by passing the creator's exact options)."
)


# --------------------------------------------------------------------------
# pure functions
# --------------------------------------------------------------------------

def bundle_kind(name: str) -> Tuple[bool, bool]:
    """(encrypted, gzipped) from the bundle file name; error otherwise."""
    base = os.path.basename(name)
    if base.endswith(".tar.gz.enc"):
        return True, True
    if base.endswith(".tar.enc"):
        return True, False
    if base.endswith(".tar.gz"):
        return False, True
    if base.endswith(".tar"):
        return False, False
    raise ValueError(
        "unsupported bundle name '{0}' — expected .tar, .tar.gz, .tar.enc "
        "or .tar.gz.enc".format(base))


def _strip_dot_slash(name: str) -> str:
    """`tar -C dir -czf bundle .` writes every member as `./name`, which
    is a perfectly ordinary way to build the bundle. The traversal checks
    are unaffected by the prefix, so it is normalised away once rather
    than reported as "carries a path"."""
    while name.startswith("./"):
        name = name[2:]
    return name


def _document_tree_member(name: str) -> bool:
    """A stored clinical file keeps its role regardless of its extension.

    Native backups include these OSCAR document directories directly. A
    patient's uploaded tar, saved SQL report, or JSON named like an import
    descriptor must not become an importer control file.
    """
    directories = {"document", "eform", "form", "hrm", "billing", "measurementmap",
                   "intake", "tickler", "image", "document_cache", "incoming", "outgoing", "templates"}
    return bool(directories.intersection(part.lower() for part in
                                        _strip_dot_slash(name).split("/")[:-1]))


def classify_members(names: List[str], digest_documents: Optional[List[str]] = None) -> Dict[str, object]:
    """Recognize both migration bundles and native OSCAR full backups.

    Multiple EMR SQL parts are a set, verified before staging by o19sources.
    Auxiliary drugref SQL and unrelated backup artifacts are retained without
    becoming EMR input. Native full backups keep their documents inside the
    context directory rather than in a second tar.
    """
    files = [name for name in names if name and not name.endswith("/")]
    for raw in files:
        clean = _strip_dot_slash(raw)
        if clean.startswith(("/", "-")) or ".." in clean.split("/"):
            raise ValueError("invalid bundle member path: {0}".format(raw))
    controls = [name for name in files if not _document_tree_member(name)]
    sql = [name for name in controls if name.endswith((".sql", ".sql.gz"))]
    auxiliary = [name for name in sql if os.path.basename(name).lower().startswith("drugref")
                 or os.path.basename(name).lower() in ("mysql.sql", "mysql.sql.gz", "sys.sql", "sys.sql.gz")]
    dumps = [name for name in sql if name not in auxiliary]
    props = [name for name in controls if os.path.basename(name).lower() in ("oscar.properties", "carlos.properties")]
    if not props:
        props = [name for name in controls if name.endswith(".properties")]
    if props:
        depth = min(_strip_dot_slash(name).count("/") for name in props)
        props = [name for name in props if _strip_dot_slash(name).count("/") == depth]
    if not dumps:
        raise ValueError("bundle contains no EMR *.sql/*.sql.gz dump")
    if len(props) != 1:
        raise ValueError("expected exactly one deployed properties file, found {0}".format(len(props)))
    docs = [name for name in controls if name.endswith((".tar", ".tar.gz"))]
    digests = [name for name in controls if os.path.basename(name).lower() in ("o19-digests.json", "o19_digests.json", "digests.json")
               or name.lower().endswith(".o19-digests.json") or name in (digest_documents or [])]
    descriptors = [name for name in controls if os.path.basename(name) == "o19-inputs.json"]
    if len(descriptors) > 1:
        raise ValueError("expected at most one o19-inputs.json descriptor")
    if len(digests) > 1:
        raise ValueError("expected at most one content-digest document")
    selected = set(dumps + docs + props + digests)
    extras = [name for name in files if name not in selected]
    root = os.path.dirname(_strip_dot_slash(props[0]))
    embedded = []
    if root:
        for name in extras:
            clean = _strip_dot_slash(name)
            if not clean.startswith(root + "/") or name in auxiliary:
                continue
            relative = clean[len(root) + 1:]
            if "/" in relative or not relative.endswith((".war", ".sh", ".log", ".json")):
                embedded.append(name)
    if docs and embedded and not descriptors:
        raise ValueError("bundle mixes embedded documents and documents archives; select an explicit document backup set")
    if len(docs) > 1 and not descriptors:
        # Filename order is not proof of an incremental restore order.
        raise ValueError("multiple documents archives require o19-inputs.json to declare their full/incremental order")
    if len(dumps) == 1 and len(docs) <= 1 and not extras and not embedded:
        return {"dump": dumps[0], "documents": docs[0] if docs else None,
                "properties": props[0], "digests": digests[0] if digests else None}
    result = {"dump": dumps[0], "documents": docs[0] if docs else None,
              "properties": props[0], "digests": digests[0] if digests else None,
              "dumps": dumps, "document_parts": docs, "extras": extras}
    if embedded:
        result["embedded_documents"] = embedded
        result["document_context"] = root
    return result


def input_manifest_roles(document, names):
    """Versioned explicit roles/order for an otherwise ambiguous native tar."""
    if not isinstance(document, dict) or type(document.get("format")) is not int or document["format"] != 1:
        raise ValueError("o19-inputs.json must have format 1")
    dumps = document.get("dumps")
    docs = document.get("documents", [])
    props = document.get("properties")
    digests = document.get("digests")
    if not isinstance(dumps, list) or not dumps or not isinstance(docs, list) or not isinstance(props, str):
        raise ValueError("o19-inputs.json requires dumps[], properties and optionally documents[]/digests")
    selected = dumps + docs + [props] + ([digests] if digests else [])
    if any(not isinstance(name, str) or name not in names for name in selected):
        raise ValueError("o19-inputs.json names a missing or invalid archive member")
    if len(set(selected)) != len(selected):
        raise ValueError("o19-inputs.json assigns one file more than one role")
    if any(not name.endswith((".sql", ".sql.gz")) for name in dumps) or any(not name.endswith((".tar", ".tar.gz")) for name in docs):
        raise ValueError("o19-inputs.json dump/document roles have incorrect file types")
    if not props.endswith(".properties") or (digests and not digests.endswith(".json")):
        raise ValueError("o19-inputs.json properties/digests roles have incorrect file types")
    return {"dump": dumps[0], "dumps": dumps, "properties": props,
            "documents": docs[0] if docs else None, "document_parts": docs,
            "digests": digests, "extras": [name for name in names if name not in selected]}


#: what reading a clinic archive raises that does NOT descend from
#: OSError, and so has to be named explicitly:
#:   * zlib.error — a gzip member whose deflate stream is corrupt
#:     mid-body surfaces it unwrapped through tarfile;
#:   * ValueError — tarfile parses GNU sparse pax headers with bare
#:     int()/unpack calls (_proc_gnusparse_01/_10, _apply_pax_info), so a
#:     `tar --sparse` archive cut inside a sparse member raises it.
#: Both are reached by the same event: a truncated multi-GB scp. Letting
#: either escape costs more than a traceback — for the o19-preflight verb
#: an uncaught exception exits 1, and exit 1 IS the "go with
#: acknowledgements" verdict, so a crash reads as a migration verdict.
ARCHIVE_ERRORS = (tarfile.TarError, zlib.error, ValueError, OSError,
                  EOFError)


def read_tar_entries(path: str, gzipped: bool
                     ) -> List[Tuple[str, str, int]]:
    """(type_letter, member_name, size) for every entry, read from the
    archive's own headers.

    NOT from `tar -tv`: that output is a formatted, quoted rendering, and
    parsing it made two independent assumptions that do not hold. GNU tar
    defaults to --quoting-style=escape, so under LC_ALL=C a member named
    `documents-santé.tar.gz` prints as `docum\303\251nts...` and the name
    handed back to tar for extraction does not exist in the archive;
    bsdtar prints nine columns rather than six, so the size column reads
    as a group name and the whole listing mis-parses into names that
    still pass the suffix rules. Reading the headers removes both.

    A link entry keeps its target in the name, as the verbose listing did,
    so a link can never masquerade as a plain member."""
    mode = "r:gz" if gzipped else "r:"
    out: List[Tuple[str, str, int]] = []
    with tarfile.open(path, mode) as tf:
        for info in tf:
            if info.issym():
                out.append((TAR_TYPE_SYMLINK,
                            "{0} -> {1}".format(info.name, info.linkname),
                            0))
            elif info.islnk():
                out.append((TAR_TYPE_HARDLINK,
                            "{0} link to {1}".format(info.name,
                                                     info.linkname), 0))
            elif info.isdir():
                out.append((TAR_TYPE_DIR, info.name, 0))
            elif info.isfile():
                out.append((TAR_TYPE_FILE, info.name, info.size))
            else:
                # character/block device, fifo, or anything else the
                # validator must refuse by type
                out.append(("?", info.name, 0))
    return out


def validate_tar_members(entries: List[Tuple[str, str]],
                         allow_dirs: bool) -> List[str]:
    """Refuse anything but plain files (and directories when allowed) with
    relative, traversal-free names. Returns the member names.

    Clinic-supplied archives are extracted as root: a symlink, hardlink or
    device entry could redirect the later chmod/hash/reads to an arbitrary
    host path, and an absolute or `..` name could write outside the
    work directory. GNU tar refuses most of these itself, but the rule is
    enforced here so it does not depend on the tar flavour installed."""
    problems: List[str] = []
    names: List[str] = []
    #: plain-file names already seen, by their normalised path: the
    #: archive is extracted wholesale later and a repeated path would
    #: silently keep whichever copy came last, so a bundle that names a
    #: document twice is ambiguous and refused (a directory entry may
    #: legitimately repeat)
    seen_files: set = set()
    allowed = {TAR_TYPE_FILE}
    if allow_dirs:
        allowed.add(TAR_TYPE_DIR)
    for type_letter, name in entries:
        if type_letter == TAR_TYPE_DIR and not _strip_dot_slash(name).strip(
                "/").replace(".", "", 1):
            # the archive root itself, which `tar -C dir -czf bundle .`
            # always writes. It creates nothing outside the work
            # directory and is not a member: skipped rather than refused,
            # so the ordinary way of building a bundle works.
            continue
        if type_letter not in allowed:
            problems.append("member '{0}' is not a plain file (tar type "
                            "'{1}': symlinks, hardlinks, devices and fifos "
                            "are refused)".format(name, type_letter))
            continue
        clean = _strip_dot_slash(name)
        if clean.startswith("/") or not clean:
            problems.append("member '{0}' has an absolute or empty name"
                            .format(name))
        elif ".." in clean.split("/"):
            problems.append("member '{0}' contains a '..' path component"
                            .format(name))
        elif clean.startswith("-"):
            # GNU tar permutes its argv: a member name such as
            # "--to-command=sh x" handed to tar as a positional argument
            # is parsed as an OPTION. Extraction also passes "--", but a
            # dash-prefixed name has no legitimate use in these archives.
            problems.append("member '{0}' starts with '-' (option-like "
                            "names are refused)".format(name))
        elif type_letter == TAR_TYPE_FILE:
            # compared by the path extraction resolves to: 'docs/./a.pdf'
            # and 'docs//a.pdf' land on 'docs/a.pdf' ('..' was refused above)
            key = os.path.normpath(clean)
            if key in seen_files:
                problems.append("member '{0}' appears more than once "
                                "(extraction would keep only the last "
                                "copy)".format(name))
            seen_files.add(key)
        names.append(name)
    if problems:
        raise ValueError("archive rejected:\n  " + "\n  ".join(problems))
    return names


def entries_size(entries: List[Tuple[str, str, int]]) -> int:
    """Expanded footprint of the plain files in an archive, for the
    disk-headroom check (a compressed archive's file size says nothing
    about what it unpacks to)."""
    return sum(size for kind, _name, size in entries
               if kind == TAR_TYPE_FILE)


def native_documents_size(workdir: str, names: List[str], context: str) -> int:
    """Exact uncompressed tar footprint, including PAX headers and padding.

    Tiny files still need tar headers and 512-byte data blocks; long/Unicode
    names and fractional mtimes add PAX records. Inspect metadata without
    writing a document archive before the capacity check.
    """
    required = 0
    with tarfile.open(fileobj=io.BytesIO(), mode="w") as probe:
        for name in names:
            relative = os.path.relpath(_strip_dot_slash(name), context)
            member = probe.gettarinfo(os.path.join(workdir, _strip_dot_slash(name)),
                                      arcname=os.path.join(os.path.basename(context), relative))
            required += len(member.tobuf(probe.format, probe.encoding, probe.errors))
            required += (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE * tarfile.BLOCKSIZE
    required += 2 * tarfile.BLOCKSIZE
    return (required + tarfile.RECORDSIZE - 1) // tarfile.RECORDSIZE * tarfile.RECORDSIZE


def tar_header_checksum_ok(header: bytes) -> bool:
    """True if a 512-byte tar header's stored checksum matches its content
    (the one validity test common to v7, ustar, gnu and pax archives)."""
    if len(header) < TAR_HEADER_LEN or not header.strip(b"\0"):
        return False
    field = header[TAR_CHECKSUM_OFFSET:TAR_CHECKSUM_OFFSET + TAR_CHECKSUM_LEN]
    digits = field.strip(b"\0 ").split(b"\0")[0].strip()
    try:
        stored = int(digits, 8)
    except ValueError:
        return False
    body = (header[:TAR_CHECKSUM_OFFSET] + b" " * TAR_CHECKSUM_LEN
            + header[TAR_CHECKSUM_OFFSET + TAR_CHECKSUM_LEN:TAR_HEADER_LEN])
    return stored == sum(body)


def openssl_decrypt_argv(cipher: str, openssl_opts: List[str],
                         pass_spec: str, in_path: str) -> List[str]:
    """openssl argv reading the bundle from -in (NOT stdin, so that
    `-pass stdin` keeps working), plaintext to stdout."""
    opts = list(openssl_opts) if openssl_opts else list(DEFAULT_DERIVATION)
    return (["openssl", "enc", "-d", "-" + cipher] + opts
            + ["-pass", pass_spec, "-in", in_path])


def pass_spec_fd(pass_spec: str) -> Optional[int]:
    """The descriptor number of an `fd:N` -pass spec, else None."""
    if pass_spec.startswith("fd:") and pass_spec[3:].isdigit():
        return int(pass_spec[3:])
    return None


def validate_bundle_args(bundle: str, pass_spec: Optional[str]) -> None:
    """Raise `ValueError` when `--bundle-pass` and the bundle's suffix
    disagree.

    Both directions are refused: a `.enc` bundle needs a password, and a
    password given for a plain bundle is an ambiguity (the wrong file,
    or a bundle that was meant to be encrypted and is not) rather than
    something to ignore."""
    encrypted, _ = bundle_kind(bundle)
    if encrypted and not pass_spec:
        raise ValueError(
            "bundle '{0}' is encrypted (.enc) — --bundle-pass is required "
            "(openssl -pass syntax: file:PATH, env:VAR, fd:N, stdin)"
            .format(os.path.basename(bundle)))
    if not encrypted and pass_spec:
        raise ValueError(
            "--bundle-pass given but '{0}' is not .enc — refusing the "
            "ambiguity".format(os.path.basename(bundle)))


def check_magic(path: str, gzipped: bool) -> None:
    """Cross-check the file's magic bytes against what its name claims."""
    with open(path, "rb") as fh:
        head = fh.read(TAR_HEADER_LEN)
    if gzipped:
        if not head.startswith(GZIP_MAGIC):
            raise ValueError(
                "'{0}' is named .gz but does not start with the gzip magic "
                "bytes — the file is not what its name claims"
                .format(os.path.basename(path)))
    else:
        # ustar magic is sufficient; a v7 archive has none, so fall back to
        # the header checksum every tar variant carries
        if head[TAR_MAGIC_OFFSET:TAR_MAGIC_OFFSET + len(TAR_MAGIC)] \
                != TAR_MAGIC and not tar_header_checksum_ok(head):
            raise ValueError(
                "'{0}' is not a tar archive (header check failed)"
                .format(os.path.basename(path)))


def sha256_file(path: str) -> str:
    """The file's SHA-256, read in 1 MiB chunks so a multi-gigabyte bundle
    is never held in memory."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def _decrypt_to(bundle: str, dest_tar: str, cipher: str,
                openssl_opts: List[str], pass_spec: str) -> None:
    """Decrypt `bundle` to `dest_tar` with openssl.

    `-pass fd:N` needs that descriptor inherited past the default
    `close_fds`, and `-pass stdin` needs stdin left alone (the bundle
    goes in through `-in`); both are handled here, and openssl itself
    validates an unusable spec."""
    argv = openssl_decrypt_argv(cipher, openssl_opts, pass_spec, bundle)
    # `-pass fd:N` needs that descriptor inherited (close_fds is the
    # default); `-pass stdin` needs stdin left alone (the bundle goes via
    # -in). Both are validated by openssl itself if unusable.
    fd = pass_spec_fd(pass_spec)
    extra = {"pass_fds": (fd,)} if fd is not None and fd > 2 else {}
    # 0600 from the first byte, and never through a plain open(): this
    # file is the clinic's WHOLE database in clear, and `open(..., "wb")`
    # creates it 0666 & ~umask -- 0644 under the packaged default, world
    # readable for as long as the import runs. O_EXCL because a file
    # already sitting at this path is not ours to write through, and its
    # mode would survive the truncation.
    out_fd = os.open(dest_tar, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(out_fd, "wb") as out:
        os.fchmod(out.fileno(), 0o600)
        cp = subprocess.run(argv, stdout=out, **extra)  # nosec B603
    if cp.returncode != 0:
        try:
            os.unlink(dest_tar)
        except OSError:
            # Best-effort: the run is already failing and die() below is the
            # outcome the operator sees. A leftover is 0600 in the 0700
            # workdir, and open_bundle clears stale copies before its next
            # attempt.
            pass
        die(WRONG_KEY_GUIDANCE)


def open_bundle(bundle: str, workdir: str, pass_spec: Optional[str] = None,
                cipher: str = DEFAULT_CIPHER,
                openssl_opts: Optional[List[str]] = None,
                expected_sha256: Optional[str] = None) -> Dict[str, object]:
    """Decrypt (if needed), verify, classify and extract a bundle.

    Members land directly in workdir (0700, created if needed). Returns
    {"dump": path, "documents": path-or-None, "properties": path,
     "digests": path-or-None, "bundle_sha256": hex} — a heterogeneous
    dict (str / None), hence the `object` value type.

    Only the bundle itself is hashed here. The extracted members are
    not: nothing consumed a per-member digest (not the ledger, not the
    report, not --resume), and computing them re-read the dump and the
    documents tar -- the run's largest inputs -- on every invocation,
    every --resume included. P1 and P5 hash the member each consumes.
    """
    validate_bundle_args(bundle, pass_spec)
    encrypted, gzipped = bundle_kind(bundle)
    os.makedirs(workdir, mode=0o700, exist_ok=True)

    # one private snapshot (0600, in the 0700 workdir) is what gets hashed,
    # decrypted and extracted: the digest recorded is the digest of the
    # bytes that were opened, whatever happens to the operator's path
    # meanwhile. The disk check budgets two bundle sizes on this volume.
    snapshot = os.path.join(workdir, ".bundle.in")
    tar_path = snapshot
    if encrypted:
        tar_path = os.path.join(workdir,
                                ".bundle.tar.gz" if gzipped else ".bundle.tar")
    # the snapshot (and, for an encrypted bundle, its decrypted twin) land
    # on the state volume before the tar listing can size the members:
    # refused while nothing has been written rather than filling the volume
    # an interrupted earlier attempt left copies here: clear them BEFORE
    # measuring, or their bytes are counted as consumed and the resume is
    # refused for space it is about to reclaim
    for stale in (snapshot, tar_path):
        if os.path.lexists(stale):
            os.unlink(stale)
    # A FIFO or a device sizes as 0, so the capacity check below would pass
    # and `copyfileobj` then block forever on a writer that never comes --
    # the import hangs with nothing written and no error.
    #
    # The check and the copy must see the SAME inode. Checking the path and
    # re-opening it by name later leaves a window in which the operator's
    # path is replaced by a FIFO, and `open()` on a FIFO blocks before any
    # check could run -- reintroducing the hang this refusal exists to
    # prevent. So open once, fstat THAT descriptor, and copy from it.
    #
    # O_NONBLOCK makes the open itself safe: opening a FIFO read-only with
    # it returns immediately instead of waiting for a writer, so a swapped
    # path is refused rather than hung on. It is cleared afterwards,
    # because a non-blocking read of a regular file is not what
    # copyfileobj expects.
    try:
        src_fd = os.open(bundle, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        die("cannot open {0}: {1}".format(bundle, exc))
    try:
        mode = os.fstat(src_fd).st_mode
        if not stat.S_ISREG(mode):
            die("{0} is not a regular file ({1}) -- a bundle must be a file "
                "on disk, not a pipe, device or directory".format(
                    bundle, stat.filemode(mode)))
        os.set_blocking(src_fd, True)
        needed = os.fstat(src_fd).st_size * (2 if encrypted else 1)
        st = os.statvfs(workdir)
        free = st.f_bavail * st.f_frsize
        if free < needed:
            die("insufficient disk under {0} to open the bundle: {1} MB "
                "free, ~{2} MB needed for its private copy{3}".format(
                    workdir, free // 1048576, needed // 1048576,
                    " and decrypted form" if encrypted else ""))
    except BaseException:
        os.close(src_fd)
        raise
    try:
        fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out, os.fdopen(src_fd, "rb") as src:
            shutil.copyfileobj(src, out, 1 << 20)
        actual = sha256_file(snapshot)
        if expected_sha256 and actual != expected_sha256:
            # the caller verified a digest on ITS read of the file; the
            # bytes opened here must be those
            die("the bundle changed on disk between its digest check and "
                "opening it — obtain it again")
        return _open_bundle(snapshot, tar_path, workdir, encrypted, gzipped,
                            pass_spec, cipher, openssl_opts, actual)
    finally:
        # neither the snapshot nor the decrypted plaintext outlives this
        # call, whichever refusal ended it
        for path in (snapshot, tar_path):
            if path != bundle and os.path.exists(path):
                os.unlink(path)


def _open_bundle(bundle: str, tar_path: str, workdir: str, encrypted: bool,
                 gzipped: bool, pass_spec: Optional[str], cipher: str,
                 openssl_opts: Optional[List[str]], actual: str
                 ) -> Dict[str, object]:
    """Decrypt if needed, verify the archive magic, and extract.

    The magic check is what turns a wrong password into a clear error:
    without a `-pbkdf2` header openssl cannot always tell, and the
    plaintext is garbage rather than a failure."""
    if encrypted:
        log("decrypting bundle ...")
        _decrypt_to(bundle, tar_path, cipher, openssl_opts or [], pass_spec)
        # a wrong key that openssl does not catch (no -pbkdf2 header)
        # produces garbage — the magic check turns that into a clear error
        try:
            check_magic(tar_path, gzipped)
        except ValueError:
            die(WRONG_KEY_GUIDANCE)
    else:
        # a truncated scp or a .tar renamed .tar.gz raises here; without
        # this the ValueError escapes as a traceback, and for the
        # o19-preflight verb exit 1 IS a verdict
        try:
            check_magic(tar_path, gzipped)
        except ValueError as exc:
            die("{0} is not the archive its name says it is ({1}) — the "
                "transfer may be truncated".format(bundle, exc))

    try:
        entries = read_tar_entries(tar_path, gzipped)
    except ARCHIVE_ERRORS as exc:
        die("cannot read the bundle's contents ({0}) — the archive is "
            "corrupt or truncated".format(exc))
    try:
        validate_tar_members(
            [(kind, name) for kind, name, _ in entries], allow_dirs=True)
        descriptors = [name for kind, name, _ in entries
                       if kind == TAR_TYPE_FILE and os.path.basename(name) == "o19-inputs.json"
                       and not _document_tree_member(name)]
        if len(descriptors) > 1:
            raise ValueError("expected at most one o19-inputs.json descriptor")
        if descriptors:
            with tarfile.open(tar_path, mode="r:*") as archive:
                descriptor = archive.getmember(descriptors[0])
                if descriptor.size > 1 << 20:
                    raise ValueError("o19-inputs.json exceeds 1 MiB")
                with archive.extractfile(descriptor) as content:
                    document = json.load(content)
            members = input_manifest_roles(document, [name for kind, name, _ in entries if kind == TAR_TYPE_FILE])
        else:
            # Older migration bundles permit any JSON filename for the
            # content digests. Identify their format, while leaving unrelated
            # backup metadata and stored clinical JSON as ordinary files.
            digest_documents = []
            with tarfile.open(tar_path, mode="r:*") as archive:
                for kind, name, size in entries:
                    if kind != TAR_TYPE_FILE or not name.lower().endswith(".json") or _document_tree_member(name):
                        continue
                    if size > DIGEST_DISCOVERY_MAX_BYTES:
                        continue
                    with archive.extractfile(name) as content:
                        try:
                            document = json.load(content)
                        except (ValueError, UnicodeError):
                            continue
                    if isinstance(document, dict) and "digest_format" in document:
                        digest_documents.append(name)
            members = classify_members([name for kind, name, _ in entries if kind == TAR_TYPE_FILE], digest_documents)
    except ValueError as exc:
        die(str(exc))
    # the headers know the expanded size: refused before extraction fills
    # the state volume (a .tar.gz bundle can expand far beyond its size)
    needed = entries_size(entries)
    st = os.statvfs(workdir)
    free = st.f_bavail * st.f_frsize
    if needed and free < needed:
        die("insufficient disk under {0} for the bundle's members: {1} MB "
            "free, ~{2} MB needed".format(workdir, free // 1048576,
                                          needed // 1048576))

    extract_flags = "-xzf" if gzipped else "-xf"
    # ownership/permissions come from the host policy (chmod below), never
    # from a clinic-authored archive
    # A native backup can contain hundreds of thousands of document names.
    # Send them as NUL-delimited data, avoiding argv limits and preserving
    # spaces/newlines without tar interpreting filenames as options.
    selected = set()
    for value in members.values():
        if isinstance(value, list):
            selected.update(value)
    selected.update(members[role] for role in ("dump", "documents", "properties", "digests") if members[role])
    cp = run(["tar", extract_flags, tar_path, "-C", workdir,
              "--no-same-owner", "--no-same-permissions", "--null",
              "--verbatim-files-from", "--files-from=-"],
             input=b"".join(os.fsencode(name) + b"\0" for name in sorted(selected)), text=False)
    if cp.returncode != 0:
        die("bundle extraction failed")

    result: Dict[str, object] = {"bundle_sha256": actual}
    for role in ("dump", "documents", "properties", "digests"):
        name = members[role]
        if name is None:
            result[role] = None
            continue
        # tar drops the `./` prefix when it writes the file
        path = os.path.join(workdir, _strip_dot_slash(name))
        if os.path.islink(path) or not os.path.isfile(path):
            die("bundle member '{0}' did not extract as a plain file"
                .format(name))
        os.chmod(path, 0o600)
        result[role] = path
    for role in ("dumps", "document_parts", "extras"):
        if role in members:
            paths = [os.path.join(workdir, _strip_dot_slash(name)) for name in members[role]]
            for path in paths:
                if not os.path.isfile(path) or os.path.islink(path):
                    die("bundle member did not extract as a plain file: {0}".format(path))
                os.chmod(path, 0o600)
            result[role] = paths
    if members.get("embedded_documents"):
        documents_path = os.path.join(workdir, ".native-documents.tar")
        context = members["document_context"]
        required = native_documents_size(workdir, members["embedded_documents"], context)
        if shutil.disk_usage(workdir).free < required:
            die("insufficient disk to prepare the native documents archive")
        fd = os.open(documents_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as document_file, tarfile.open(fileobj=document_file, mode="w") as archive:
            os.fchmod(document_file.fileno(), 0o600)
            for name in members["embedded_documents"]:
                # A backup may itself be inside another directory. The
                # documents phase expects exactly one context directory.
                relative = os.path.relpath(_strip_dot_slash(name), context)
                archive.add(os.path.join(workdir, _strip_dot_slash(name)),
                            arcname=os.path.join(os.path.basename(context), relative), recursive=False)
        result["documents"] = documents_path
    return result
