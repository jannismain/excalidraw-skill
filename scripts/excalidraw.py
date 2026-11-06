#!/usr/bin/env python3
"""Read Excalidraw drawings: list screens, dump texts, render PNG/SVG.

Obsidian stores a drawing LZ-String-compressed inside an `.excalidraw.md` file,
so grep and Read show nothing useful. This tool decodes it (standard library
only; plain `.excalidraw` JSON works too) and gives three views of the data:

    list    the screens in the drawing (frames, else large rectangles) with a label
    texts   the text elements of a screen or region, grouped into rows
    render  a PNG (needs rsvg-convert or magick) or SVG of a screen or region

Run `python3 -I excalidraw.py <command> --help` for options. Output goes to a
temporary directory unless `-o` is given; the tool never writes next to the drawing.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_STROKE = "#1e1e1e"
DEFAULT_MIN_SIZE = (600, 400)
LABEL_REACH = 140  # px above a screen to look for its title
FRAME_LABEL_REACH = 28  # frames draw their own name just above the border


# --------------------------------------------------------------------------- decoding


def lz_decompress_base64(data: str) -> str:
    """Port of LZString.decompressFromBase64 (what the Excalidraw plugin writes)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
    lookup = {c: i for i, c in enumerate(alphabet)}
    data = re.sub(r"\s", "", data)
    length = len(data)
    reset = 32

    def value_at(i: int) -> int:
        return lookup.get(data[i], 0) if i < length else 0

    state = {"val": value_at(0), "pos": reset, "idx": 1}

    def read_bits(n: int) -> int:
        bits, power, maxpower = 0, 1, 1 << n
        while power != maxpower:
            resb = state["val"] & state["pos"]
            state["pos"] >>= 1
            if state["pos"] == 0:
                state["pos"] = reset
                state["val"] = value_at(state["idx"])
                state["idx"] += 1
            if resb > 0:
                bits |= power
            power <<= 1
        return bits

    dictionary: dict[int, str] = {0: "0", 1: "1", 2: "2"}
    enlarge_in, dict_size, num_bits = 4, 4, 3
    first = read_bits(2)
    if first == 0:
        c = chr(read_bits(8))
    elif first == 1:
        c = chr(read_bits(16))
    else:
        return ""
    dictionary[3] = c
    w = c
    result = [c]
    while True:
        if state["idx"] > length:
            return ""
        code = read_bits(num_bits)
        if code in (0, 1):
            dictionary[dict_size] = chr(read_bits(8 if code == 0 else 16))
            dict_size += 1
            code = dict_size - 1
            enlarge_in -= 1
        elif code == 2:
            break
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        if code in dictionary:
            entry = dictionary[code]
        elif code == dict_size:
            entry = w + w[0]
        else:
            raise ValueError("corrupt LZ-String data")
        result.append(entry)
        dictionary[dict_size] = w + entry[0]
        dict_size += 1
        enlarge_in -= 1
        w = entry
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
    # JS strings are UTF-16 code units; rejoin surrogate pairs (emoji).
    return "".join(result).encode("utf-16", "surrogatepass").decode("utf-16")


class Drawing:
    def __init__(self, path: Path, min_size: tuple[int, int] = DEFAULT_MIN_SIZE):
        self.path = path
        self.min_size = min_size
        text = path.read_text(encoding="utf-8")
        match = re.search(r"```compressed-json\n(.*?)\n```", text, re.S)
        if match:
            raw = lz_decompress_base64(match.group(1))
        else:
            match = re.search(r"```json\n(.*?)\n```", text, re.S)
            if match:
                raw = match.group(1)
            elif text.lstrip().startswith("{"):
                raw = text  # plain .excalidraw
            else:
                sys.exit(f"{path}: no Excalidraw drawing data found")
        scene = json.loads(raw)
        self.elements = [e for e in scene["elements"] if not e.get("isDeleted")]
        self.files = scene.get("files") or {}
        self.file_links = dict(re.findall(r"^([0-9a-f]{40}): \[\[(.+?)\]\]", text, re.M))
        self._screens: list[dict] | None = None

    # ---- screens

    def screens(self) -> list[dict]:
        if self._screens is None:
            frames = [e for e in self.elements if e["type"] == "frame"]
            min_w, min_h = self.min_size
            rects = frames or [
                e
                for e in self.elements
                if e["type"] == "rectangle" and e["width"] >= min_w and e["height"] >= min_h
            ]
            rects.sort(key=lambda e: (round(e["x"] / 200), e["y"]))  # columns left to right, top to bottom
            self._screens = []
            for i, r in enumerate(rects, 1):
                box = (r["x"], r["y"], r["x"] + r["width"], r["y"] + r["height"])
                label = (r.get("name") or "") if frames else self._label(box)
                self._screens.append(
                    {"n": i, "id": r["id"], "box": box, "label": label, "texts": self.texts_in(box), "frame": bool(frames)}
                )
        return self._screens

    def _label(self, box) -> str:
        x0, y0, x1, y1 = box
        above = [
            t
            for t in self.elements
            if t["type"] == "text"
            and y0 - LABEL_REACH <= t["y"] + t["height"] <= y0 + 4
            and x0 - 20 <= t["x"] <= x1
        ]
        above.sort(key=lambda t: -(t["y"] + t["height"]))  # nearest first
        if above:
            return " ".join(above[0]["text"].split())
        return ""

    def resolve_screen(self, query: str) -> dict:
        screens = self.screens()
        if query.isdigit() and 1 <= int(query) <= len(screens):
            return screens[int(query) - 1]
        q = query.lower()
        by_id = [s for s in screens if s["id"].lower().startswith(q)]
        if len(by_id) == 1:
            return by_id[0]
        hits = [s for s in screens if q in s["label"].lower()] or [
            s for s in screens if any(q in t["text"].lower() for t in s["texts"])
        ]
        if len(hits) == 1:
            return hits[0]
        names = hits or screens
        lines = "\n".join(f"  {s['n']:>2}  {s['id'][:8]}  {s['label'] or '(no label)'}" for s in names)
        sys.exit(f"screen {query!r} matched {len(hits)} screens; use the number or id:\n{lines}")

    # ---- element queries

    def in_box(self, e, box) -> bool:
        x0, y0, x1, y1 = box
        return e["x"] < x1 and e["x"] + e["width"] > x0 and e["y"] < y1 and e["y"] + e["height"] > y0

    def texts_in(self, box) -> list[dict]:
        x0, y0, x1, y1 = box
        out = []
        for e in self.elements:
            if e["type"] != "text":
                continue
            cx, cy = e["x"] + e["width"] / 2, e["y"] + e["height"] / 2
            if x0 <= cx <= x1 and y0 <= cy <= y1:  # centre inside, so neighbours' labels are excluded
                out.append(e)
        return out


# --------------------------------------------------------------------------- texts


def rows_of(texts: list[dict]) -> list[list[dict]]:
    rows: list[list[dict]] = []
    row_cy: list[float] = []
    for t in sorted(texts, key=lambda t: (t["y"] + t["height"] / 2, t["x"])):
        cy = t["y"] + t["height"] / 2
        if rows and abs(cy - row_cy[-1]) <= max(8, t["fontSize"] * 0.6):
            rows[-1].append(t)
        else:
            rows.append([t])
            row_cy.append(cy)
    for row in rows:
        row.sort(key=lambda t: t["x"])
    return rows


def format_texts(texts: list[dict], origin: tuple[float, float]) -> str:
    ox, oy = origin
    lines = []
    for row in rows_of(texts):
        parts = []
        for t in row:
            body = " / ".join(t["text"].split("\n"))
            color = t.get("strokeColor", DEFAULT_STROKE)
            tag = "" if color == DEFAULT_STROKE else f" [{color}]"
            parts.append(f"@{round(t['x'] - ox)}: {body}{tag}")
        lines.append(f"y={round(row[0]['y'] - oy):>4} | " + "  ||  ".join(parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------- rendering


def _dash(e) -> str:
    return {"dashed": ' stroke-dasharray="8 6"', "dotted": ' stroke-dasharray="2 5"'}.get(e.get("strokeStyle"), "")


def _paint(e) -> str:
    sw = e.get("strokeWidth", 1)
    stroke = e.get("strokeColor", DEFAULT_STROKE) if sw > 0 else "none"
    fill = e.get("backgroundColor", "transparent")
    return f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" opacity="{e.get("opacity", 100) / 100}"{_dash(e)}'


def _image_href(d: Drawing, e) -> str | None:
    fid = e.get("fileId")
    if fid in d.files and d.files[fid].get("dataURL"):
        return d.files[fid]["dataURL"]
    name = d.file_links.get(fid)
    if not name:
        return None
    for base in (d.path.parent, d.path.parent.parent):
        p = base / name
        if p.is_file():
            mime = "image/svg+xml" if p.suffix == ".svg" else "image/png"
            return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
    return None


def to_svg(d: Drawing, box: tuple[float, float, float, float]) -> str:
    x0, y0, x1, y1 = box
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {x1 - x0} {y1 - y0}" '
        f'width="{x1 - x0}" height="{y1 - y0}">',
        f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="white"/>',
    ]
    for e in d.elements:  # file order is z-order
        if not d.in_box(e, box):
            continue
        t, x, y, w, h = e["type"], e["x"], e["y"], e["width"], e["height"]
        rot = f' transform="rotate({math.degrees(e["angle"])} {x + w / 2} {y + h / 2})"' if e.get("angle") else ""
        if t == "frame":
            out.append(
                f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="none" stroke="#bbb" stroke-width="2"/>'
                f'<text x="{x}" y="{y - 8}" font-size="20" font-family="Helvetica, Arial, sans-serif" '
                f'fill="#868e96">{html.escape(e.get("name") or "")}</text>'
            )
        elif t == "rectangle":
            r = min(32, min(w, h) * 0.25) if e.get("roundness") else 0
            out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" {_paint(e)}{rot}/>')
        elif t == "ellipse":
            out.append(f'<ellipse cx="{x + w / 2}" cy="{y + h / 2}" rx="{w / 2}" ry="{h / 2}" {_paint(e)}{rot}/>')
        elif t in ("line", "arrow"):
            pts = [(x + px, y + py) for px, py in e["points"]]
            closed = e["points"][0] == e["points"][-1] and e.get("backgroundColor") != "transparent"
            fill = e["backgroundColor"] if closed else "none"
            out.append(
                f'<polyline points="{" ".join(f"{a},{b}" for a, b in pts)}" fill="{fill}" '
                f'stroke="{e.get("strokeColor", DEFAULT_STROKE)}" stroke-width="{e.get("strokeWidth", 1)}" '
                f'opacity="{e.get("opacity", 100) / 100}"{_dash(e)}{rot}/>'
            )
            if t == "arrow" and e.get("endArrowhead") and len(pts) >= 2:
                (ax, ay), (bx, by) = pts[-2], pts[-1]
                ang = math.atan2(by - ay, bx - ax)
                tip = [(bx, by)] + [
                    (bx - 14 * math.cos(ang + s * 0.45), by - 14 * math.sin(ang + s * 0.45)) for s in (-1, 1)
                ]
                out.append(
                    f'<polygon points="{" ".join(f"{a},{b}" for a, b in tip)}" fill="{e.get("strokeColor")}"/>'
                )
        elif t == "text":
            fs = e["fontSize"]
            align = e.get("textAlign", "left")
            ax = {"left": x, "center": x + w / 2, "right": x + w}[align]
            anchor = {"left": "start", "center": "middle", "right": "end"}[align]
            for i, line in enumerate(e["text"].split("\n")):
                out.append(
                    f'<text x="{ax}" y="{y + fs * 0.95 + i * fs * 1.25}" font-size="{fs}" '
                    f'font-family="Helvetica, Arial, sans-serif" fill="{e.get("strokeColor", DEFAULT_STROKE)}" '
                    f'text-anchor="{anchor}"{rot}>{html.escape(line)}</text>'
                )
        elif t == "image":
            href = _image_href(d, e)
            if href:
                out.append(
                    f'<image x="{x}" y="{y}" width="{w}" height="{h}" preserveAspectRatio="none" href="{href}"{rot}/>'
                )
            else:
                out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#eee" stroke="red"/>')
    out.append("</svg>")
    return "\n".join(out)


def rasterize(svg: Path, png: Path, width: int) -> bool:
    if shutil.which("rsvg-convert"):
        cmd = ["rsvg-convert", "-w", str(width), str(svg), "-o", str(png)]
    elif shutil.which("magick"):
        cmd = ["magick", "-background", "white", "-density", "96", str(svg), "-resize", f"{width}x", str(png)]
    else:
        return False
    subprocess.run(cmd, check=True)
    return True


# --------------------------------------------------------------------------- cli


def parse_size(s: str) -> tuple[int, int]:
    try:
        w, h = (int(v) for v in s.lower().split("x"))
    except ValueError:
        sys.exit("--min-size expects WxH, e.g. 600x400")
    return w, h


def parse_region(s: str) -> tuple[float, float, float, float]:
    try:
        x0, y0, x1, y1 = (float(v) for v in s.split(","))
    except ValueError:
        sys.exit("--region expects x0,y0,x1,y1 in drawing coordinates, e.g. 2400,-300,3500,500")
    return x0, y0, x1, y1


def target_box(d: Drawing, args, pad: int = 0) -> tuple[tuple[float, float, float, float], str]:
    if args.region:
        return parse_region(args.region), "region"
    if not args.screen:
        sys.exit("give --screen <n|id|text> or --region x0,y0,x1,y1 (see `list`)")
    s = d.resolve_screen(args.screen)
    x0, y0, x1, y1 = s["box"]
    reach = FRAME_LABEL_REACH if s["frame"] else LABEL_REACH
    top = pad + (reach if pad else 0)
    return (x0 - pad, y0 - top, x1 + pad, y1 + pad), f"screen-{s['n']}"


def cmd_list(args) -> None:
    d = Drawing(Path(args.file), parse_size(args.min_size))
    kind = "frames" if any(e["type"] == "frame" for e in d.elements) else f"rectangles >= {d.min_size[0]}x{d.min_size[1]}"
    print(f"{len(d.elements)} elements, {len(d.screens())} screens ({kind})\n")
    print(" n  id        x      y      w     h   label")
    for s in d.screens():
        x0, y0, x1, y1 = s["box"]
        print(f"{s['n']:>2}  {s['id'][:8]}  {x0:>5.0f}  {y0:>5.0f}  {x1 - x0:>5.0f} {y1 - y0:>4.0f}   {s['label'] or '(no label)'}")
    if args.sections:
        print("\nSection headings (large text, outside screens):")
        for t in sorted(d.elements, key=lambda e: (e["y"], e["x"])):
            if t["type"] == "text" and t["fontSize"] >= 28:
                print(f"  @({t['x']:.0f},{t['y']:.0f})  {' '.join(t['text'].split())}")


def cmd_texts(args) -> None:
    d = Drawing(Path(args.file), parse_size(args.min_size))
    box, _ = target_box(d, args)
    texts = d.texts_in(box)
    if args.json:
        print(json.dumps([{k: t[k] for k in ("text", "x", "y", "width", "height", "fontSize", "strokeColor")} for t in texts], indent=2))
    else:
        print(f"region {tuple(round(v) for v in box)}  (x/y below are relative to its top-left; [color] = non-default text colour, often annotations)\n")
        print(format_texts(texts, (box[0], box[1])))


def cmd_render(args) -> None:
    d = Drawing(Path(args.file), parse_size(args.min_size))
    box, name = target_box(d, args, pad=args.pad)
    outdir = Path(args.output).parent if args.output else Path(tempfile.mkdtemp(prefix="excalidraw-"))
    stem = Path(args.output).stem if args.output else name
    svg = outdir / f"{stem}.svg"
    outdir.mkdir(parents=True, exist_ok=True)
    svg.write_text(to_svg(d, box), encoding="utf-8")
    if args.svg_only:
        print(svg)
        return
    png = outdir / f"{stem}.png"
    if rasterize(svg, png, args.width):
        print(png)
    else:
        print(f"{svg}\n(no rsvg-convert or magick found; install librsvg for PNG output)", file=sys.stderr)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def target(sp):
        sp.add_argument("file", help="path to an .excalidraw.md or .excalidraw file")
        sp.add_argument("--min-size", default="600x400", help="smallest rectangle treated as a screen when the drawing has no frames (WxH)")
        sp.add_argument("--screen", "-s", help="screen number, id prefix, or text in its label/content")
        sp.add_argument("--region", "-r", help="x0,y0,x1,y1 in drawing coordinates (instead of --screen)")

    sp = sub.add_parser("list", help="list screens")
    sp.add_argument("file")
    sp.add_argument("--min-size", default="600x400", help="smallest rectangle treated as a screen when the drawing has no frames (WxH)")
    sp.add_argument("--sections", action="store_true", help="also print large headings (titles of sections outside screens)")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("texts", help="text content of a screen or region, grouped into rows")
    target(sp)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_texts)

    sp = sub.add_parser("render", help="render a screen or region to PNG (or SVG)")
    target(sp)
    sp.add_argument("-o", "--output", help="output path; extension is replaced (.png/.svg). Default: temp dir")
    sp.add_argument("--width", type=int, default=1600, help="PNG width in px (default 1600)")
    sp.add_argument("--pad", type=int, default=20, help="margin around a --screen, incl. its title above (default 20, 0 = none)")
    sp.add_argument("--svg-only", action="store_true")
    sp.set_defaults(fn=cmd_render)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
