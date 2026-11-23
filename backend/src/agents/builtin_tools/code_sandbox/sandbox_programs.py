"""The fixed programs ``run_code`` writes into the sandbox and runs there.

Nothing here runs on the Runtime: these are source texts. They are constants,
so the only per-call input a program sees is ``.run_code/request.json`` (data,
never pasted into source), the way the Word tools pass ``params``.

One call is a single ``executeCommand``::

    python3 .run_code/snapshot.py
    timeout -k 5 <seconds> python3 .run_code/entry.py > .run_code/output.txt 2>&1
    python3 .run_code/finish.py <exit code>

* ``snapshot.py`` records what is in ``outputs/`` before the run, so ``finish``
  can tell which files this call wrote.
* ``entry.py`` runs the script or the model's code, chosen by the target's
  extension. Python runs by ``runpy`` as ``__main__`` with ``sys.argv`` set and
  its folder first on ``sys.path``, so sibling imports work. JavaScript
  (``.js``/``.cjs``/``.mjs``) replaces the process with ``node <target> <argv…>``
  (``os.execvp``), so its exit code, its output and ``timeout``'s signal are
  Node's own, and its ``require('./x')`` resolves beside the file as usual. It is a child process of the command, so a script's
  ``sys.exit``, ``os.chdir`` or module state ends with it. Its output goes to a
  file because the command channel merges stdout into stderr when a command
  fails.
* ``finish.py`` prints one marked JSON line: the exit code, the tail of the
  output, the files this call wrote under ``outputs/``, and downscaled copies of
  the images the model asked to see. It runs even when the entry timed out.
"""

from __future__ import annotations

import shlex
from typing import Optional

# Folder, relative to the sandbox's working directory, holding the programs,
# the request and the run's output. Hidden so it does not crowd a listing.
CONTROL_DIR = ".run_code"
OUTPUTS_DIR = "outputs"
INPUTS_DIR = "inputs"
SKILLS_DIR = "skills"

# The line ``finish.py`` prints its JSON after. Anything else the command
# printed is ignored.
RESULT_MARKER = "@@RUN_CODE_RESULT@@"

SNAPSHOT_PY = r'''
import json, os
seen = {}
for root, _dirs, files in os.walk("outputs"):
    for name in files:
        p = os.path.join(root, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        seen[p] = [st.st_size, st.st_mtime_ns]
with open(".run_code/before.json", "w") as f:
    json.dump(seen, f)
'''

#: Targets ``entry.py`` hands to ``node`` instead of ``runpy``.
NODE_EXTENSIONS = (".js", ".cjs", ".mjs")

ENTRY_PY = r'''
import json, os, runpy, sys
with open(".run_code/request.json", encoding="utf-8") as f:
    req = json.load(f)
for d in ("inputs", "outputs"):
    os.makedirs(d, exist_ok=True)
target = req["target"]
if os.path.splitext(target)[1].lower() in (".js", ".cjs", ".mjs"):
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp("node", ["node", target, *req.get("argv", [])])
    except OSError as exc:
        print("node could not be started: %s" % exc, file=sys.stderr)
        sys.exit(127)
sys.argv = [target, *req.get("argv", [])]
sys.path.insert(0, os.path.dirname(os.path.abspath(target)))
runpy.run_path(target, run_name="__main__")
'''

FINISH_PY = r'''
import json, os, sys
MARKER = "@@RUN_CODE_RESULT@@"
exit_code = int(sys.argv[1]) if len(sys.argv) > 1 else -1
with open(".run_code/request.json", encoding="utf-8") as f:
    req = json.load(f)
cap = int(req.get("output_cap", 4000))
try:
    with open(".run_code/output.txt", encoding="utf-8", errors="replace") as f:
        text = f.read().replace("\r\n", "\n")
except OSError:
    text = ""
truncated = len(text) > cap
if truncated:
    text = text[-cap:]
try:
    with open(".run_code/before.json") as f:
        before = json.load(f)
except (OSError, ValueError):
    before = {}
written = []
for root, _dirs, files in os.walk("outputs"):
    for name in files:
        p = os.path.join(root, name)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if before.get(p) != [st.st_size, st.st_mtime_ns]:
            written.append([p, st.st_size])
written.sort()
previews = []
max_px = int(req.get("image_max_pixels", 1150000))
max_edge = int(req.get("image_max_edge", 1568))
for i, path in enumerate(req.get("show_images", [])):
    entry = {"path": path}
    norm = os.path.normpath(path)
    if os.path.isabs(norm) or norm.startswith(".."):
        entry["error"] = "only paths inside the working folder can be shown"
        previews.append(entry)
        continue
    try:
        from PIL import Image
        with Image.open(norm) as im:
            im.load()
            w, h = im.size
            scale = min(1.0, max_edge / max(w, h), (max_px / float(w * h)) ** 0.5)
            if scale < 1.0:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))))
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")
            out = ".run_code/preview_%d.png" % i
            im.save(out, "PNG", optimize=True)
            entry.update({"preview": out, "width": im.size[0], "height": im.size[1]})
    except Exception as exc:
        entry["error"] = "%s: %s" % (type(exc).__name__, str(exc)[:200])
    previews.append(entry)
print(MARKER + json.dumps({
    "exit_code": exit_code,
    "output": text,
    "output_truncated": truncated,
    "written": written,
    "previews": previews,
}))
'''

# Decodes the base64 staging files ``run_code`` wrote with ``writeFiles``
# (which takes text), then deletes them. The manifest lists
# ``[b64_path, final_path]`` pairs.
DECODE_PY = r'''
import base64, json, os
with open(".run_code/decode.json") as f:
    pairs = json.load(f)
for src, dst in pairs:
    d = os.path.dirname(dst)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(src) as f:
        raw = base64.b64decode(f.read())
    with open(dst, "wb") as f:
        f.write(raw)
    os.remove(src)
print(len(pairs))
'''


def run_command(
    timeout_seconds: int,
    office_program_dir: Optional[str] = None,
    office_node_modules: Optional[str] = None,
) -> str:
    """The one shell command a call runs (see the module docstring).

    ``office_program_dir`` puts a mounted LibreOffice first on the run's
    ``PATH`` (``office_mount.py``), with the headless VCL plugin its own
    ``soffice.py`` wrappers set too. ``office_node_modules`` puts the mount's
    pinned Node packages (docx-js, pptxgenjs, …) first on ``NODE_PATH``, so a
    script's ``require('docx')`` or ``require('pptxgenjs')`` resolves; anything the sandbox already had on
    ``NODE_PATH`` stays after it.
    """
    office = ""
    if office_program_dir:
        office += f'PATH={shlex.quote(office_program_dir)}:"$PATH" SAL_USE_VCLPLUGIN=svp '
    if office_node_modules:
        office += f'NODE_PATH={shlex.quote(office_node_modules)}"${{NODE_PATH:+:$NODE_PATH}}" '
    return (
        f"python3 {CONTROL_DIR}/snapshot.py; "
        f"{office}timeout -k 5 {int(timeout_seconds)} python3 {CONTROL_DIR}/entry.py "
        f"> {CONTROL_DIR}/output.txt 2>&1; "
        f"python3 {CONTROL_DIR}/finish.py $?"
    )
