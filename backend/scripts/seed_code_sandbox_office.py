"""Install LibreOffice onto the code sandbox's office file system (spec D9).

``run_code``'s sessions mount that file system read-only at ``/mnt/office`` and
run with ``/mnt/office/current/program`` first on ``PATH``. Nothing in a
session can write to it, so this script is the only way a build gets there:
it starts a session on the **seeding** interpreter (the one role allowed to
write, ``code-sandbox-office-construct.ts``), mounts the write access point,
and installs one pinned build.

What it does, all of it re-runnable:

1. Downloads the official LibreOffice aarch64 RPM bundle and two Amazon Linux
   2023 libraries the sandbox lacks (``libXinerama``, ``libXrandr``) to a local
   cache, and refuses any file whose SHA-256 is not the pinned one.
2. Unpacks the RPMs it needs (core, Writer, Calc, Impress, Draw, Math, en-US,
   fonts and filters: about 550 MB) into a tree, with the two libraries in
   ``program/``, where LibreOffice's ``$ORIGIN`` rpath finds them.
3. Copies the tree's archive into the seeding session in chunks, unpacks it to
   ``releases/<version>.partial``, checks that ``soffice --version`` runs from
   there, renames it to ``releases/<version>``, and points ``current`` at it with
   one atomic rename, so a session that starts mid-upgrade sees the old build
   or the new one, never half of either.

Run it once after the deploy that turns ``CDK_CODE_SANDBOX_OFFICE_ENABLED`` on,
and again for each LibreOffice upgrade (bump the pins below). It takes about
five minutes, most of it the unpack over NFS. Needs credentials for the
deployment's account (SSM read, Code Interpreter sessions on the seeding
interpreter) and, for step 2, ``bsdtar`` (macOS ``tar``) or ``rpm2cpio`` and
``cpio``.

    cd backend
    AWS_PROFILE=<profile> uv run python scripts/seed_code_sandbox_office.py --prefix <project-prefix>

``--dry-run`` stops after step 2 and prints what it would install.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

# ---------------------------------------------------------------------------
# Pins. Bump all of these together for an upgrade.
# ---------------------------------------------------------------------------

LIBREOFFICE_VERSION = "26.8.3"
#: The RPMs' own build number, in their file names.
LIBREOFFICE_BUILD = "26.8.3.1"
#: The install folder the RPMs unpack into, under ``opt/``.
LIBREOFFICE_DIR = "libreoffice26.8"
LIBREOFFICE_URL = (
    "https://download.documentfoundation.org/libreoffice/stable/26.8.3/rpm/aarch64/"
    "LibreOffice_26.8.3_Linux_aarch64_rpm.tar.gz"
)
#: From the ``.sha256`` file published beside it.
LIBREOFFICE_SHA256 = "41a09c1e1f117bda5de842eae96f558cde2d0da431da90247378a4b071b98acd"

#: What a document conversion and a recalculation need. Base (the database
#: app), the Java and JavaScript script providers, the desktop integrations,
#: online update and the extra dictionaries are left out.
LIBREOFFICE_PACKAGES = [
    "libobasis26.8-core", "libobasis26.8-calc", "libobasis26.8-writer", "libobasis26.8-impress",
    "libobasis26.8-draw", "libobasis26.8-math", "libobasis26.8-en-US", "libobasis26.8-graphicfilter",
    "libobasis26.8-images", "libobasis26.8-ooofonts", "libobasis26.8-ooolinguistic",
    "libobasis26.8-xsltfilter", "libreoffice26.8", "libreoffice26.8-ure", "libreoffice26.8-calc",
    "libreoffice26.8-writer", "libreoffice26.8-impress", "libreoffice26.8-draw",
    "libreoffice26.8-math", "libreoffice26.8-en-US",
]

#: Amazon Linux 2023 aarch64 libraries the sandbox image lacks. The URLs are the
#: repository's content-addressed blobstore paths, from its primary.xml metadata.
_AL2023 = "https://cdn.amazonlinux.com/al2023/blobstore"
EXTRA_LIBRARIES = [
    {
        "url": f"{_AL2023}/d7da99f4f5ab36963d4c7dc51d947226c25557cca3a35dd178712a76fed8332d/"
               "libXinerama-1.1.5-6.amzn2023.0.1.aarch64.rpm",
        "sha256": "d7da99f4f5ab36963d4c7dc51d947226c25557cca3a35dd178712a76fed8332d",
    },
    {
        "url": f"{_AL2023}/68b6669b9ceafb9212820fc79592e948b527d07ccc770ffbc7999304d849c18e/"
               "libXrandr-1.5.4-3.amzn2023.0.1.aarch64.rpm",
        "sha256": "68b6669b9ceafb9212820fc79592e948b527d07ccc770ffbc7999304d849c18e",
    },
]

# Must match code-sandbox-office-construct.ts / office_mount.py.
OFFICE_MOUNT_SSM_SUFFIX = "agentcore/code-sandbox-office-mount"
MOUNT_PATH = "/mnt/office"

#: Bytes per ``writeFiles`` call; 24 MB calls worked on dev.
CHUNK_BYTES = 24 * 1024 * 1024
SESSION_TIMEOUT_SECONDS = 1800


def _say(message: str) -> None:
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Step 1: downloads
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _fetch(url: str, sha256: str, cache: Path) -> Path:
    """``url`` in ``cache``, downloaded if absent; raises unless its SHA-256 matches."""
    target = cache / url.rsplit("/", 1)[1]
    if not target.exists() or _sha256(target) != sha256:
        _say(f"downloading {url}")
        partial = target.with_suffix(target.suffix + ".partial")
        with urllib.request.urlopen(url, timeout=120) as response, partial.open("wb") as out:  # noqa: S310 - pinned https
            shutil.copyfileobj(response, out)
        partial.rename(target)
    actual = _sha256(target)
    if actual != sha256:
        target.unlink()
        raise SystemExit(f"{target.name}: SHA-256 {actual} is not the pinned {sha256}; refusing it")
    return target


# ---------------------------------------------------------------------------
# Step 2: the tree
# ---------------------------------------------------------------------------


def _unpack_rpm(rpm: Path, into: Path) -> None:
    """Extract an RPM's payload into ``into`` with bsdtar, or rpm2cpio | cpio."""
    for tar in ("bsdtar", "tar"):
        exe = shutil.which(tar)
        if exe and "bsdtar" in subprocess.run([exe, "--version"], capture_output=True, text=True).stdout:
            subprocess.run([exe, "-xf", str(rpm), "-C", str(into)], check=True)
            return
    if shutil.which("rpm2cpio") and shutil.which("cpio"):
        payload = subprocess.run(["rpm2cpio", str(rpm)], check=True, capture_output=True).stdout
        subprocess.run(["cpio", "-idm", "--quiet"], input=payload, cwd=into, check=True)
        return
    raise SystemExit("unpacking RPMs needs bsdtar (macOS tar) or rpm2cpio and cpio")


def _rpm_for(rpms: Path, package: str) -> Path:
    """The bundle's RPM for exactly ``package`` (``libreoffice26.8`` is not ``libreoffice26.8-calc``)."""
    matches = [
        p for p in rpms.glob(f"{package}-{LIBREOFFICE_BUILD}-*.rpm")
        if p.name[len(package) + 1:].startswith(LIBREOFFICE_BUILD)
    ]
    if len(matches) != 1:
        raise SystemExit(f"expected one RPM for {package} in the bundle, found {[m.name for m in matches]}")
    return matches[0]


def build_tree(cache: Path, work: Path) -> Path:
    """The install tree (``work/tree/<version>``), with the extra libraries in ``program/``."""
    bundle = _fetch(LIBREOFFICE_URL, LIBREOFFICE_SHA256, cache)
    libraries = [_fetch(lib["url"], lib["sha256"], cache) for lib in EXTRA_LIBRARIES]

    unpacked = work / "bundle"
    with tarfile.open(bundle) as archive:
        archive.extractall(unpacked, filter="data")
    rpms = next(unpacked.glob("*/RPMS"))

    root = work / "rpm-root"
    root.mkdir()
    for package in LIBREOFFICE_PACKAGES:
        _unpack_rpm(_rpm_for(rpms, package), root)

    tree = work / "tree" / LIBREOFFICE_VERSION
    tree.parent.mkdir()
    (root / "opt" / LIBREOFFICE_DIR).rename(tree)

    libs = work / "libs"
    libs.mkdir()
    for rpm in libraries:
        _unpack_rpm(rpm, libs)
    program = tree / "program"
    for so in sorted((libs / "usr" / "lib64").glob("*.so*")):
        shutil.copy2(so, program / so.name, follow_symlinks=False)

    if not (program / "soffice").exists():
        raise SystemExit("the unpacked tree has no program/soffice")
    return tree


def pack(tree: Path, work: Path) -> Path:
    archive = work / f"libreoffice-{LIBREOFFICE_VERSION}.tgz"
    with tarfile.open(archive, "w:gz", compresslevel=6) as out:
        out.add(tree, arcname=LIBREOFFICE_VERSION)
    return archive


# ---------------------------------------------------------------------------
# Step 3: the seeding session
# ---------------------------------------------------------------------------


class Seeder:
    """One session on the seeding interpreter, with the write access point mounted."""

    def __init__(self, region: str, mount: Dict[str, Any]) -> None:
        import boto3
        from botocore.config import Config

        self.client = boto3.client(
            "bedrock-agentcore", region_name=region,
            config=Config(read_timeout=900, retries={"max_attempts": 2}),
        )
        self.interpreter_id = mount["seederInterpreterId"]
        started = time.monotonic()
        response = self.client.start_code_interpreter_session(
            codeInterpreterIdentifier=self.interpreter_id,
            name=f"seed-office-{LIBREOFFICE_VERSION}",
            sessionTimeoutSeconds=SESSION_TIMEOUT_SECONDS,
            filesystemConfigurations=[{"efsConfiguration": {
                "fileSystemArn": mount["fileSystemArn"],
                "accessPointArn": mount["writeAccessPointArn"],
                "mountPath": MOUNT_PATH,
            }}],
        )
        self.session_id = response["sessionId"]
        _say(f"seeding session {self.session_id} started in {time.monotonic() - started:.1f}s")

    def _invoke(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        response = self.client.invoke_code_interpreter(
            codeInterpreterIdentifier=self.interpreter_id, sessionId=self.session_id,
            name=name, arguments=arguments,
        )
        merged: Dict[str, Any] = {"isError": False, "stdout": "", "stderr": "", "exitCode": None}
        for event in response["stream"]:
            result = event.get("result") or {}
            merged["isError"] = merged["isError"] or bool(result.get("isError"))
            structured = result.get("structuredContent") or {}
            merged["stdout"] += structured.get("stdout") or ""
            merged["stderr"] += structured.get("stderr") or ""
            if structured.get("exitCode") is not None:
                merged["exitCode"] = structured["exitCode"]
        return merged

    def run(self, command: str) -> str:
        result = self._invoke("executeCommand", {"command": command})
        if result["isError"] or result["exitCode"] not in (0, None):
            raise SystemExit(f"command failed ({result['exitCode']}): {command}\n{result['stdout']}{result['stderr']}")
        return result["stdout"].strip()

    def upload(self, archive: Path, remote: str) -> None:
        size = archive.stat().st_size
        parts: List[str] = []
        with archive.open("rb") as handle:
            for index, block in enumerate(iter(lambda: handle.read(CHUNK_BYTES), b"")):
                part = f"{remote}.part{index:03d}"
                started = time.monotonic()
                result = self._invoke("writeFiles", {"content": [{"path": part, "blob": block}]})
                if result["isError"]:
                    raise SystemExit(f"writeFiles failed for {part}")
                parts.append(part)
                _say(f"  uploaded {min((index + 1) * CHUNK_BYTES, size) / 1e6:.0f}/{size / 1e6:.0f} MB "
                     f"({time.monotonic() - started:.1f}s)")
        self.run(f"cat {' '.join(shlex.quote(p) for p in parts)} > {shlex.quote(remote)} && rm -f {' '.join(shlex.quote(p) for p in parts)}")

    def stop(self) -> None:
        try:
            self.client.stop_code_interpreter_session(
                codeInterpreterIdentifier=self.interpreter_id, sessionId=self.session_id,
            )
        except Exception as exc:  # noqa: BLE001 - its timeout ends it anyway
            _say(f"stopping the session failed ({exc}); it ends on its own timeout")


def install(seeder: Seeder, archive: Path, expected_sha256: str) -> str:
    """Unpack into a new release folder, check it, switch ``current``; returns ``soffice --version``.

    Every run installs into a folder of its own (``releases/<version>-<time>``),
    so ``current`` always moves between two complete builds with one rename. The
    build ``current`` pointed at before is kept, since a session may still be
    running it; anything older is removed.
    """
    remote = f"libreoffice-{LIBREOFFICE_VERSION}.tgz"
    seeder.upload(archive, remote)
    got = seeder.run(f"sha256sum {remote} | cut -d' ' -f1")
    if got != expected_sha256:
        raise SystemExit(f"the uploaded archive's SHA-256 is {got}, not {expected_sha256}")

    release = f"{LIBREOFFICE_VERSION}-{int(time.time())}"
    folder = f"{MOUNT_PATH}/releases/{release}"
    started = time.monotonic()
    seeder.run(
        f"mkdir -p {folder} && tar -xzf {remote} -C {folder} --strip-components=1 --no-same-owner "
        f"&& chmod -R a+rX,go-w {folder} && rm -f {remote}"
    )
    _say(f"unpacked onto the file system in {time.monotonic() - started:.0f}s")
    version = seeder.run(f"SAL_USE_VCLPLUGIN=svp {folder}/program/soffice --headless --version")
    if LIBREOFFICE_VERSION not in version:
        raise SystemExit(f"soffice did not report {LIBREOFFICE_VERSION}: {version}")

    previous = seeder.run(f"readlink {MOUNT_PATH}/current || true").rsplit("/", 1)[-1]
    seeder.run(
        f"cd {MOUNT_PATH} && ln -sfn releases/{release} current.new && mv -T current.new current"
    )
    keep = {release, previous} - {""}
    seeder.run(
        f"cd {MOUNT_PATH}/releases && for r in *; do case \" $r \" in "
        + "".join(f"\" {k} \") ;; " for k in sorted(keep))
        + "*) rm -rf \"$r\" ;; esac; done"
    )
    return seeder.run(f"SAL_USE_VCLPLUGIN=svp {MOUNT_PATH}/current/program/soffice --headless --version")


def read_mount(prefix: str, region: str) -> Dict[str, Any]:
    import boto3

    name = f"/{prefix}/{OFFICE_MOUNT_SSM_SUFFIX}"
    try:
        value = boto3.client("ssm", region_name=region).get_parameter(Name=name)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(
            f"cannot read {name} ({exc}). Is CDK_CODE_SANDBOX_OFFICE_ENABLED on and deployed for this prefix?"
        ) from exc
    return json.loads(value)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prefix", required=True, help="The deployment's project prefix (CDK_PROJECT_PREFIX)")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--cache", type=Path, default=Path.home() / ".cache" / "code-sandbox-office",
                        help="Where downloads are kept between runs")
    parser.add_argument("--dry-run", action="store_true", help="Build the tree locally and stop")
    args = parser.parse_args()

    args.cache.mkdir(parents=True, exist_ok=True)
    mount = None if args.dry_run else read_mount(args.prefix, args.region)
    with tempfile.TemporaryDirectory(prefix="seed-office-") as tmp:
        work = Path(tmp)
        tree = build_tree(args.cache, work)
        size = sum(p.stat().st_size for p in tree.rglob("*") if p.is_file() and not p.is_symlink())
        _say(f"tree for LibreOffice {LIBREOFFICE_VERSION}: {size / 1e6:.0f} MB")
        if args.dry_run:
            _say("dry run: nothing uploaded")
            return
        archive = pack(tree, work)
        seeder = Seeder(args.region, mount)
        try:
            version = install(seeder, archive, _sha256(archive))
        finally:
            seeder.stop()
    _say(f"installed: {version}")


if __name__ == "__main__":
    main()
