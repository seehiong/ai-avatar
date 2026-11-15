# Third-party notices

The source code in this repository is MIT-licensed (see `LICENSE`). It includes or is derived from the
third-party work below, and some assets are **not** covered by the MIT licence.

## Code

### coi-serviceworker — `web/coi-serviceworker.js`
MIT License. Copyright (c) Guido Zuidhof and contributors. https://github.com/gzuidhof/coi-serviceworker
(The licence notice is kept in the file header.)

### metahuman-to-glb — hair/eyebrow card shading in `web/index.html` (`mhHairCards`)
Ported from the viewer of https://github.com/creategamecharacters/metahuman-to-glb.

```
MIT License

Copyright (c) 2026 smorchj

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### TalkingHead — rig used by `tools/build_mpfb_avatar.py` and baked into `assets/models/mpfb-interviewer-male.glb`
MIT License. Copyright (c) met4citizen. https://github.com/met4citizen/TalkingHead

## Assets

| Asset | Terms |
| --- | --- |
| `assets/models/mpfb-interviewer-male.glb` | Built from MakeHuman / MPFB system assets (CC0) with TalkingHead's rig (MIT, above). |
| `assets/env/poly_haven_studio_2k.hdr` | CC0, from [Poly Haven](https://polyhaven.com). |
| `assets/models/metahuman-interviewer-v2.glb`, `assets/models/metahuman-v2/` | **Not MIT.** Derived from an Epic Games MetaHuman; subject to Epic's MetaHuman licence terms. |
| Hair in `metahuman-interviewer-v2.glb` | MakeHuman community asset `toigo_blunt_bob` by MRT, CC0 (fitted by `tools/makehuman_hair.py`). |
| `assets/ai-interviewer.png`, `assets/photoreal/` | **Not MIT.** The interviewer portrait and files derived from it; all rights reserved by their owner. |
| `assets/models/hair-generated-experiment.png`, `assets/models/skin-young-clean.jpg` | **Not MIT.** Texture images; all rights reserved by their owner. |

## Downloaded at run time (not in this repository)

Kokoro-82M, Zyphra ZONOS3-Mini (server built from https://github.com/Zyphra/ZONOS3-Mini, weights `Zyphra/ZONOS3-Mini`), and wav2arkit_cpu (Apache-2.0),
Piper voices, faster-whisper / Whisper, Silero VAD, WebLLM models, and the photoreal models (MuseTalk and
LivePortrait code are MIT; LivePortrait's InsightFace detector weights are for non-commercial research only). Each
keeps its own licence.
