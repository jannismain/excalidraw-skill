---
name: excalidraw
description: Read and render Excalidraw drawings (`.excalidraw.md` from Obsidian, or `.excalidraw`). Use when a task references a wireframe, mockup, diagram or sketch stored in one, or before implementing UI from it. The file is compressed, so Read and grep show nothing useful.
---

# Excalidraw

`scripts/excalidraw.py` decodes the drawing (Python 3 standard library only) and shows it three ways: a list of screens, their text, and a PNG you can look at. PNG output needs `rsvg-convert` or `magick`.

Run `python3 -I <skill-dir>/scripts/excalidraw.py` with these commands. `<skill-dir>` is this skill's directory.

1. `list <file> --sections` numbers every screen with its title. Screens are frames, or when the drawing has none, rectangles of at least 600×400 (`--min-size WxH` tunes this). `--sections` adds large headings outside screens.
2. `render <file> -s <screen>` prints the path of a PNG. Read the PNG, then compare against `texts` before you build. Done means every label in `texts` appears in what you built.
3. `texts <file> -s <screen>` prints every label grouped into rows (`@x` is the offset from the screen's left edge, `y` from its top). Use it for exact wording and row order, since the render is approximate.

`-s` takes the screen number, an id prefix, or text from the title or content. An ambiguous query exits and lists the candidates.

`-r x0,y0,x1,y1` replaces `-s` for any region in drawing coordinates, such as a component library that is not a screen. Take the coordinates from `list --sections`.

Output goes to a temp directory unless `-o <path>` is given, so the drawing's folder stays untouched.

## Reading the result

- `texts` marks text with a non-default colour as `[#color]`. In drawings that annotate in a second colour (purple, red), those marks are commentary: user-flow notes, TODOs, sample chat messages. Check one screen to confirm the convention before relying on it.
- The render approximates Excalidraw. Fonts differ and the hand-drawn roughness is gone. Structure, labels and positions are reliable; colours and exact spacing are indicative.
- Read the saved file, which can lag the open editor. Ask the user to save when a screen looks stale.
- Obsidian drawings often hold several dated copies (`Name 2026-10-30.excalidraw.md`). Take the newest unless told otherwise.
- Embedded images resolve from the drawing's folder (Obsidian stores them as sibling files). A missing one renders as a red-outlined grey box.
