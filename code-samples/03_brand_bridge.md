# Sample 03: one brand file for TypeScript and Python

**Problem:** the pipeline has two halves. Remotion (React + TypeScript) draws every frame; Python cuts the video, transcribes it, plans captions, b-roll and motion, and checks the result. Both need the same numbers: caption size, safe areas, the accent colour, how far the footage may scale, sound volumes, Whisper settings. Two copies of those values drift apart within a week.

**Solution:** `src/brand.ts` is the only place any brand value lives. React components import it directly. Python reads it through a 12-line Node script that prints it as JSON. The one rule that makes this work: `brand.ts` stays a plain object literal with no imports, so it can be evaluated without a TypeScript compiler.

Adapted from the private codebase; comments were added for this case study.

## 1. `src/brand.ts` (excerpt)

```ts
// SINGLE SOURCE OF TRUTH for the brand.
// Pure data, no imports: Remotion components import it directly and the Python
// tools read it through `node tools/brand_dump.mjs`. Change values here only.

export const brand = {
  name: 'Michel Ward',
  handle: '@michelward',

  colors: {
    text: '#FFFFFF',
    coral: '#FF8A5B', // the only accent
    ink: '#141414',   // neutral dark surface, never an accent
  },

  fonts: {
    sans: {family: 'Inter Tight', weight: '600'},                     // hook, captions, cards
    serif: {family: 'Instrument Serif', style: 'italic', weight: '400'}, // key words, numbers
  },

  video: {width: 1080, height: 1920, fps: 30},

  // 9:16 safe areas in px. Nothing important outside `box`.
  safe: {top: 120, bottom: 320, left: 90, right: 120, box: {x: 90, y: 120, w: 870, h: 1480}},

  captions: {fontSize: 66, centerY: 1330, maxWords: 3, maxPageMs: 1800},

  // No black or empty edge: footage scales up at most 1.15x while slid aside,
  // and the edge check reads a 3 px strip of every frame over a magenta probe.
  cover: {maxScale: 1.15, probeColor: '#FF00FF', probeScale: 0.25, edgePx: 3,
          maxEdgeFraction: 0.005, blackLuma: 6, blackRun: 0.1},

  // ... about 400 more lines: hook, end card, icons, b-roll, motion graphics,
  // sound effects and volumes, loudness target, encoder flags, Whisper model
} as const;
```

## 2. `tools/brand_dump.mjs`: TypeScript object to JSON

```js
// Prints src/brand.ts as JSON so the Python tools share the single source of truth.
// brand.ts must stay a plain object literal: `export const brand = {...} as const;`
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(path.join(root, 'src', 'brand.ts'), 'utf8');

// Cut out the object literal between `export const brand = ` and `} as const;`.
// Without imports or type annotations inside, it is valid JavaScript as it stands.
const start = src.indexOf('export const brand = ') + 'export const brand = '.length;
const end = src.indexOf('} as const;', start) + 1;

// Evaluate it as an expression. The input is a file in this repository, never user input.
const brand = new Function(`return (${src.slice(start, end)});`)();
process.stdout.write(JSON.stringify(brand));
```

## 3. `tools/common.py`: loaded once, shared by every Python step

```python
"""Shared helpers for the reel pipeline. Brand values come from src/brand.ts."""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_brand():
    try:
        out = subprocess.run(["node", str(ROOT / "tools" / "brand_dump.mjs")],
                             capture_output=True, text=True, encoding="utf-8", check=True, cwd=ROOT).stdout
    except FileNotFoundError:
        sys.exit("error: node not found. Install Node.js; the pipeline reads src/brand.ts through tools/brand_dump.mjs.")
    except subprocess.CalledProcessError as e:
        # The usual cause: someone added an import or a type annotation to brand.ts.
        sys.exit(f"error: could not read src/brand.ts (keep it a plain object literal, no imports):\n{e.stderr.strip()}")
    return json.loads(out)


# Imported by every step: `from common import BRAND`, then BRAND["captions"]["fontSize"], BRAND["cover"]...
BRAND = load_brand()
```

## Why not JSON, YAML or a generated file?

* **JSON** has no comments, and the comments are half the value: each number says who decided it and why.
* **A generated file** (TS to JSON at build time) adds a build step that can go stale between runs; the Python tools run on their own, outside any build.
* **`as const` in TypeScript** keeps exact literal types, so the components get autocomplete and a typo in a key is a compile error.

The cost is one rule (no imports in `brand.ts`) and one Node process per Python run, about 50 ms.
