"""
Motion step of the planner: which motion graphic goes where (Python)

Problem: a talking-head reel needs a visual change every 4 to 6 seconds to hold
attention, but every graphic must be relevant to what is being said, never
cover the face, and never compete with another graphic or a b-roll card.
Placing them by hand in an editor takes longer than recording the video.

This step reads the word-level transcript (Whisper) and turns what the speaker
says into motion graphics:
  - AI talk (prompt, Claude, ChatGPT, IA...)       -> a typing prompt box
  - a site screenshot in the b-roll                -> cursor click + zoom
  - two places within 8 s                          -> a route line on a map
  - phones / social media / followers              -> a phone mockup
  - numbers, money, percent                        -> a number counter
Quote cards, stat cards and charts only come from the edit file, because they
need real input (the creator's own quote or numbers), never invented ones.

Then it resolves collisions with a few explicit rules: one graphic on screen at
a time, starts at least ~3.5 s apart, the same automatic effect at most every
15 s, and overlays tried at a couple of positions before giving up rather than
touching the face keep-out (a face ellipse from the YuNet detector, scaled by
the live camera zoom).

Adapted from the private codebase: the logic is unchanged; comments were added
for this case study and the transcript trigger matching (candidates()) is
summarised rather than included.
"""
from common import BRAND, norm  # BRAND = src/brand.ts as a dict (see sample 03); norm() lowercases and strips accents

B = BRAND
MG = B["mg"]  # motion graphics settings: rhythm, first/last allowed second, sounds, phone size...

# Trigger vocabularies (English and Brazilian Portuguese; matched after norm(), so "notificações" -> "notificacoes")
AI_WORDS = {"prompt", "claude", "chatgpt", "gpt", "gemini", "ia", "ai", "openai", "llm", "copilot"}
FEED_WORDS = {"scrollando", "scrolling", "scroll", "feed", "instagram", "tiktok", "reels", "celular", "phone"}

# What each type is worth when two candidates collide (explicit entries from the edit file always win).
PRIORITY = {"cursor": 9, "quote": 9, "route": 8, "typing": 8, "stat": 8, "chart": 8, "counter": 7, "phone": 6,
            "reveal": 5}
# Split-screen graphics push the speaker down; full-screen ones replace him; everything else floats over him.
LAYOUT = {"cursor": "split", "route": "split", "quote": "full"}

# Overlay boxes (x, y, w, h) on the 1080x1920 frame at their default position.
BOXES = {"typing": (90, 230, 870, 240), "counter": (90, 190, 870, 320), "stat": (90, 175, 870, 415),
         "chart": (90, 190, 870, 425), "reveal": (90, 600, 870, 300)}
PHONE = MG["phone"]


def sec_for(kind, props):
    """How long a graphic stays on screen: long enough to read, never longer."""
    if kind == "typing":  # the prompt types out, then holds briefly
        return round(min(4.8, max(2.6, len(props["text"]) / MG["typing"]["charsPerSec"] + 1.6)), 2)
    if kind == "quote":  # word-by-word reveal + hold
        words = len(props["text"].split())
        return round(0.3 + words * MG["quote"]["wordFrames"] / 30 + 0.4 + MG["quote"]["holdSec"], 2)
    return {"cursor": 3.5, "route": 3.5, "phone": 3.4, "counter": 2.6, "stat": 3.4, "chart": 3.4, "reveal": 2.4}[kind]


def overlaps(a0, a1, b0, b1, pad=0.4):
    """Two time spans touch, with a 0.4 s breathing gap between graphics."""
    return a0 < b1 + pad and b0 < a1 + pad


def plan(cands, broll, dur, report, hits_face, base_scale_at, rhythm=None):
    """Accepted motion items, sorted by time (they become props for the Remotion MotionLayer).

    cands          candidates from the transcript and the edit file: {type, t, props, explicit, why, replaces?}
    broll          b-roll items already placed (cards, splits); a cursor screen can replace its screenshot
    hits_face      (box, camera_scale, t0, t1) -> True when the box touches the face keep-out in that span
    base_scale_at  (t0, t1) -> the largest camera zoom in that span (push-ins make the face bigger)
    """
    first = max(MG["firstAllowedSec"], B["hook"]["holdSec"] + B["hook"]["outSec"] + 0.3)  # never during the hook
    gap = (rhythm or MG["rhythmSec"])[0] - 0.5  # starts ~3.5 s apart, so the overall rhythm lands at 4-6 s
    # Explicit first, then by priority, then by time: the creator's own choices are never displaced by an auto pick.
    cands.sort(key=lambda c: (not c["explicit"], -PRIORITY.get(c["type"], 1), c["t"]))
    accepted, used_types, quote_done = [], {}, False

    for c in cands:
        kind = c["type"]
        props = dict(c["props"])
        if kind == "typing" and "text" not in props:
            continue
        if kind == "quote" and quote_done:  # one quote card per reel
            continue
        sec = float(props.pop("sec", sec_for(kind, props)))
        t0 = round(c["t"], 3)
        t1 = t0 + sec

        # 1. Inside the allowed window (after the hook, clear of the end card).
        if t0 < first or t1 > dur - MG["lastAllowedSecBeforeEnd"]:
            if c["explicit"]:
                report["warnings"].append(f"motion {kind} at {t0:.1f}s skipped: during the hook or too close to the end")
            continue

        # 2. Never on top of, or right next to, a b-roll card (unless this graphic replaces that b-roll).
        replaced = c.get("replaces")
        others = [b for b in broll if b is not replaced]
        if any(overlaps(t0, t1, b["t"], b["t"] + b["dur"]) or abs(t0 - b["t"]) < gap for b in others):
            if c["explicit"]:
                report["warnings"].append(f"motion {kind} at {t0:.1f}s skipped: b-roll is on screen")
            continue

        # 3. One graphic at a time, with the rhythm gap between starts.
        if any(overlaps(t0, t1, a["t"], a["t"] + a["sec"]) or abs(t0 - a["t"]) < gap for a in accepted):
            continue

        # 4. Variety: the same automatic effect at most every 15 s.
        if not c["explicit"] and used_types.get(kind, -99) > t0 - MG["sameTypeGapSec"]:
            continue

        # 5. Overlays never cover the face, checked at the live camera zoom for the whole span.
        layout = LAYOUT.get(kind, "overlay")
        scale = base_scale_at(t0, t1)
        if layout == "overlay":
            if kind == "phone":
                # Right side first, then left; full-size phone first, then the smaller fallback.
                sb = B["safe"]["box"]
                ok = [(x, 230, k) for k in (1, PHONE["smallSize"])
                      for x in (sb["x"] + sb["w"] - PHONE["w"] * k, sb["x"])
                      if not hits_face((x, 230, PHONE["w"] * k, PHONE["h"] * k), scale, t0, t1)]
                if not ok:
                    report["warnings"].append(f"motion phone at {t0:.1f}s skipped: no face-safe side")
                    continue
                props.update(x=round(ok[0][0]), y=ok[0][1], size=ok[0][2])
            else:
                # Try the default height, then nudge up twice (never into the top 130 px of app UI).
                x, y, w, h = BOXES[kind]
                y = props.get("y", y)
                for dy in (0, -30, -60):
                    if y + dy >= 130 and not hits_face((x, y + dy, w, h), scale, t0, t1):
                        props["y"] = y + dy
                        break
                else:
                    report["warnings"].append(f"motion {kind} at {t0:.1f}s skipped: it would cover the face")
                    continue

        if kind == "quote":
            props.setdefault("attribution", B["name"])
            quote_done = True
        if replaced is not None:
            broll.remove(replaced)  # the cursor screen shows the same screenshot, now with a click and zoom
        used_types[kind] = t0
        # Every decision is recorded with its reason, so the plan report explains why each graphic is there.
        accepted.append({"type": kind, "t": t0, "sec": round(sec, 2), "layout": layout, "props": props,
                         "why": c["why"]})

    accepted.sort(key=lambda a: a["t"])
    return accepted
