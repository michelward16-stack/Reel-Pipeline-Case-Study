"""
Edge check: never a black or empty edge on any frame (Python + OpenCV + Remotion CLI)

Problem: when a b-roll card or a split screen appears, the talking-head footage
slides aside (down for a split, sideways for a card). Slide it too far and a
band of empty background shows at the edge of the frame. On a phone that looks
like a rendering bug, and it is easy to miss when you only look at a few stills.

Two layers stop it:
  1. Prevention, in the Remotion composition: while the footage is slid, it is
     scaled up around the face just enough that every edge still passes the
     frame edge. The planner caps each slide so that scale stays <= 1.15
     (brand.cover.maxScale), so the face never looks zoomed in.
  2. Verification, in this file: render EVERY content frame with the
     background replaced by a probe colour (magenta), then read a thin strip
     along each edge. Any magenta, or a long run of pure black (letterboxed
     media), fails the frame.

The render step refuses to run unless this check passed for the exact props
file it is about to render (matched by SHA-1), so a fix can't be forgotten.

Adapted from the private codebase: the logic is unchanged; comments were
added for this case study. Helpers (BRAND, WORK, run, need) come from the
pipeline's common module.
"""
import hashlib
import json
import shutil

import cv2
import numpy as np

from common import BRAND, ROOT, WORK, need, run  # BRAND = src/brand.ts as a dict (see sample 03)

CV = BRAND["cover"]  # {probeColor: '#FF00FF', probeScale: 0.25, edgePx: 3, maxEdgeFraction: 0.005, blackLuma: 6, blackRun: 0.1, ...}
FPS = BRAND["video"]["fps"]
NPX = shutil.which("npx") or "npx"


def props_hash(path):
    return hashlib.sha1(path.read_bytes()).hexdigest()


def passed(name, props_path):
    """True when edgecheck.json holds a pass for exactly this props file content.

    The render command calls this first: editing the props after a pass (a new
    b-roll, a moved card) changes the hash, so the check has to run again.
    """
    f = WORK / name / "edgecheck.json"
    if not f.exists():
        return False
    r = json.loads(f.read_text(encoding="utf-8")).get(props_path.name)
    return bool(r) and r["pass"] and r["hash"] == props_hash(props_path)


def edge_faults(img):
    """{edge: reason} for one BGR frame; an empty dict means the frame is clean."""
    probe = tuple(int(CV["probeColor"][i:i + 2], 16) for i in (1, 3, 5))  # '#FF00FF' -> (r, g, b)
    e = CV["edgePx"]
    # Each strip is reshaped to (depth, length, 3) so the four edges share one code path.
    strips = {"top": img[:e, :], "bottom": img[-e:, :], "left": img[:, :e].transpose(1, 0, 2),
              "right": img[:, -e:].transpose(1, 0, 2)}
    out = {}
    for edge, s in strips.items():
        s = s.astype(np.int16)
        # A pixel position along the edge is "empty" when any of its strip pixels is near the probe colour.
        # The tolerance absorbs anti-aliasing at the footage boundary and video compression.
        empty = ((np.abs(s[..., 2] - probe[0]) < 60) & (np.abs(s[..., 1] - probe[1]) < 60)
                 & (np.abs(s[..., 0] - probe[2]) < 60)).any(axis=0)
        # "Black" needs the whole strip depth to be near black: a dark shirt touching the edge is fine,
        # but a letterbox bar from a badly scaled screenshot is not.
        luma = 0.299 * s[..., 2] + 0.587 * s[..., 1] + 0.114 * s[..., 0]
        black = (luma <= CV["blackLuma"]).all(axis=0)
        run_, longest = 0, 0
        for b in black:
            run_ = run_ + 1 if b else 0
            longest = max(longest, run_)
        if empty.mean() > CV["maxEdgeFraction"]:
            out[edge] = f"empty {empty.mean():.1%}"
        elif longest >= CV["blackRun"] * len(black):
            out[edge] = f"black run {longest / len(black):.0%}"
    return out


def check(name, props_path=None):
    props_path = props_path or WORK / name / "props_sfx.json"
    need(props_path, f"Run: python tools/reel.py plan {name}")
    p = json.loads(props_path.read_text(encoding="utf-8"))
    wd = WORK / name / "edgecheck"
    if wd.exists():
        shutil.rmtree(wd)
    frames_dir = wd / "frames"
    frames_dir.mkdir(parents=True)

    # Same props, three switches: `probe` paints the background magenta instead of the dark ink colour,
    # grain and the debug overlay are off so they can't hide or fake an edge.
    probe_props = wd / "props_probe.json"
    probe_props.write_text(json.dumps(dict(p, probe=True, debug=False, grain=False), ensure_ascii=False), encoding="utf-8")

    # Every content frame (the end card is excluded: its dark surface is by design), at 1/4 scale and muted.
    # A 45 s reel is ~1,300 PNGs; at 270x480 that renders in a few minutes on a laptop GPU.
    last = p["contentFrames"] - 1
    run([NPX, "remotion", "render", "src/index.ts", "Reel", str(frames_dir), f"--props={probe_props}", "--sequence",
         "--image-format=png", f"--frames=0-{last}", f"--scale={CV['probeScale']}", "--muted", "--gl=angle",
         "--log=error"], cwd=ROOT)

    fails = []
    for i, f in enumerate(sorted(frames_dir.glob("*.png"))):
        faults = edge_faults(cv2.imread(str(f)))
        if faults:
            fails.append({"frame": i, "t": round(i / FPS, 2), "edges": faults})

    # Full-size probe stills of the first, middle and last failure, so the cause is visible at a glance.
    for f in {id(x): x for x in (fails[:1] + fails[len(fails) // 2:len(fails) // 2 + 1] + fails[-1:])}.values():
        run([NPX, "remotion", "still", "src/index.ts", "Reel", str(wd / f"fail_{f['frame']}.png"), f"--frame={f['frame']}",
             f"--props={probe_props}", "--gl=angle", "--log=error"], cwd=ROOT)
    shutil.rmtree(frames_dir)

    # Results are keyed by props file name + content hash (read back by passed()).
    result = {"pass": not fails, "hash": props_hash(props_path), "frames": p["contentFrames"], "failed": len(fails),
              "failures": fails[:40]}
    rf = WORK / name / "edgecheck.json"
    allr = json.loads(rf.read_text(encoding="utf-8")) if rf.exists() else {}
    allr[props_path.name] = result
    rf.write_text(json.dumps(allr, indent=1), encoding="utf-8")

    if fails:
        # Group consecutive failing frames into time spans: "9.80-10.40s {'top': 'empty 12.0%'}" is actionable,
        # 18 separate frame numbers are not.
        spans = []
        for f in fails:
            if spans and f["frame"] == spans[-1][1] + 1:
                spans[-1][1] = f["frame"]
            else:
                spans.append([f["frame"], f["frame"]])
        print(f"[edgecheck] FAIL {props_path.name}: {len(fails)} frames have a black or empty edge")
        for a, b in spans[:12]:
            print(f"  {a / FPS:.2f}-{b / FPS:.2f}s  {fails[[x['frame'] for x in fails].index(a)]['edges']}")
        return False
    print(f"[edgecheck] pass {props_path.name}: every edge covered")
    return True
