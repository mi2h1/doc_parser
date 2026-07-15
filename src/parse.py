"""kikakurui.com の JIS 規格 HTML を構造化 JSON + 画像に変換するパーサー。

pdftohtml 形式の HTML を対象とする:
- ページは <!-- Page N --> コメントで区切られる
- テキストは座標付き <p style="top:..;left:..;" class="ftXX"> 要素
- 図・罫線・数式の記号はページ全体の背景 PNG に含まれる

数式は座標クラスタリングで検出し、背景 PNG から矩形を切り出す。
"""
import argparse
import html as htmllib
import io
import json
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from PIL import Image, ImageDraw, ImageFont

PAGE_W, PAGE_H = 892, 1263
UA = {"User-Agent": "doc-parser-prototype/0.1 (internal QMS research)"}

RE_STYLE = re.compile(r"\.(ft\d+)\{font-size:(\d+)px")
RE_ITEM = re.compile(
    r'<p style="top:(-?\d+)px;\s*left:(-?\d+)px;?[^"]*"\s+class="(ft\d+)">(.*?)</p>',
    re.S,
)
RE_BG = re.compile(r'<img[^>]*src="\./([^"]+?/page-\d+\.png)"')
RE_HEADING = re.compile(r"^(\d+(\.\d+)*|附属書\s*[A-Z]?|[A-Z]\.\d+(\.\d+)*)\s+\S")
RE_FIG_CAP = re.compile(r"^図\s*\d+")
RE_TBL_CAP = re.compile(r"^表\s*\d+")

FRAG_MAX_LEN = 4          # この文字数以下の断片は数式候補
CLUSTER_V_GAP = 28        # 数式クラスタの縦方向許容ギャップ(px)
CLUSTER_MIN_SIZE = 4      # 数式と見なす最小断片数
LINE_TOL = 5              # 同一行と見なす top の許容差(px)
RENDER_SCALE = 2          # 数式画像の拡大倍率（ビジョンモデルの読み取り精度向上）

# 日本語を含む断片は数式ではなく本文の一部（「ここに，」等）
RE_JP = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")
# 箇条書きマーカー（1)  a)  など）は数式断片から除外
RE_LIST_MARKER = re.compile(r"^\d{1,2}\)$|^[a-zA-Z]\)$")

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]
_font_cache = {}


def load_font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    font = None
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default(size)
    _font_cache[size] = font
    return font


def parse_pages_arg(spec: str):
    """'9-14' や '9,12,20-25' を集合に展開する。"""
    result = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            result.update(range(int(a), int(b) + 1))
        elif part:
            result.add(int(part))
    return result


def clean_text(raw: str) -> str:
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", raw))
    return text.replace("\xa0", " ").strip()  # nbsp を通常スペースに正規化


def is_header_footer(top: int, text: str) -> bool:
    if top < 50:  # 法改正の注意書きバナー
        return True
    if top >= 1150:  # フッター（ページ番号等）
        return True
    if top <= 110 and (re.fullmatch(r"\d+", text) or text.startswith("B 8267")):
        return True
    return False


def cluster_formulas(fragments):
    """縦方向に近接する断片群を数式クラスタにまとめる。"""
    clusters = []
    for frag in sorted(fragments, key=lambda f: f["top"]):
        placed = False
        for cl in clusters:
            if frag["top"] - cl["max_top"] <= CLUSTER_V_GAP:
                cl["items"].append(frag)
                cl["max_top"] = max(cl["max_top"], frag["top"])
                placed = True
                break
        if not placed:
            clusters.append({"items": [frag], "max_top": frag["top"]})
    return [c["items"] for c in clusters if len(c["items"]) >= CLUSTER_MIN_SIZE]


def trim_horizontal_outliers(items):
    """クラスタ本体から横に大きく離れた断片（箇条書き記号等）を除外する。"""
    if len(items) < 3:
        return items
    lefts = sorted(it["left"] for it in items)
    median = lefts[len(lefts) // 2]
    return [it for it in items if abs(it["left"] - median) <= 250]


def bbox_of(items):
    # ルート記号や大括弧などの図形は断片の座標より外側に伸びるため広めに取る
    x0 = min(it["left"] for it in items) - 14
    y0 = min(it["top"] for it in items) - 16
    x1 = max(it["left"] + int(len(it["text"]) * it["size"] * 0.7) for it in items) + 24
    y1 = max(it["top"] + it["size"] for it in items) + 26
    return [max(0, x0), max(0, y0), min(PAGE_W, x1), min(PAGE_H, y1)]


def is_formula_fragment(text: str) -> bool:
    if len(text) > FRAG_MAX_LEN:
        return False
    if RE_JP.search(text):
        return False
    if RE_LIST_MARKER.match(text):
        return False
    return True


def detect_lines(bg_image, bbox, scale, min_len_css=12):
    """背景 PNG の切り出し範囲から横線（分数の括線・ルートの上棒）を検出する。

    背景には図形しか描かれていないため、暗いピクセルの水平ランは
    ほぼ確実に括線・ルート棒・罫線である。
    左端の下に斜線（ルートのチェック記号）があれば sqrt と判定する。

    背景 PNG は HTML の表示サイズ（892x1263）より高解像度なため、
    scale で PNG ピクセル座標へ変換して走査し、結果は CSS px に戻して返す。
    """
    x0, y0, _, _ = bbox
    pixel_bbox = [int(v * scale) for v in bbox]
    min_len = int(min_len_css * scale)
    crop = bg_image.crop(pixel_bbox).convert("L")
    w, h = crop.size
    px = crop.load()

    segments = []
    for y in range(h):
        run_start = None
        for x in range(w + 1):
            dark = x < w and px[x, y] < 128
            if dark and run_start is None:
                run_start = x
            elif not dark and run_start is not None:
                if x - run_start >= min_len:
                    segments.append((run_start, x - 1, y))
                run_start = None

    # 太さのある線は複数行に検出されるのでマージ
    merged = []
    for sx, ex, sy in sorted(segments, key=lambda s: (s[2], s[0])):
        for m in merged:
            if abs(m["y"] - sy) <= 3 and not (ex < m["x0"] - 5 or sx > m["x1"] + 5):
                m["x0"] = min(m["x0"], sx)
                m["x1"] = max(m["x1"], ex)
                break
        else:
            merged.append({"x0": sx, "x1": ex, "y": sy})

    result = []
    for m in merged:
        # ルート判定: 線の左端の左下に図形ピクセル（ルートの斜線）があるか
        sqrt = False
        for dy in range(int(2 * scale), int(16 * scale)):
            for dx in range(int(-12 * scale), int(2 * scale)):
                xx, yy = m["x0"] + dx, m["y"] + dy
                if 0 <= xx < w and 0 <= yy < h and px[xx, yy] < 128:
                    sqrt = True
                    break
            if sqrt:
                break
        # PNG ピクセル → CSS px に戻す
        result.append(
            {
                "x0": round(m["x0"] / scale) + x0,
                "x1": round(m["x1"] / scale) + x0,
                "y": round(m["y"] / scale) + y0,
                "sqrt": sqrt,
            }
        )
    return result


def render_formula_image(bg_image, items, bbox, scale):
    """背景の切り出し（分数の横棒等の図形）にテキスト断片を座標どおり描画して合成する。

    背景 PNG には図形しか含まれず、文字は HTML テキスト層にしかないため、
    両者を重ねて初めて完全な数式画像になる。
    背景 PNG は表示サイズより高解像度なため scale で換算して切り出す。
    """
    x0, y0, x1, y1 = bbox
    s = RENDER_SCALE
    pixel_bbox = [int(v * scale) for v in bbox]
    canvas = bg_image.crop(pixel_bbox).resize(
        ((x1 - x0) * s, (y1 - y0) * s), Image.LANCZOS
    )
    draw = ImageDraw.Draw(canvas)
    for it in items:
        draw.text(
            ((it["left"] - x0) * s, (it["top"] - y0) * s),
            it["text"],
            fill=(0, 0, 0),
            font=load_font(it["size"] * s),
        )
    return canvas


def classify_line(text: str) -> str:
    if RE_FIG_CAP.match(text):
        return "figure_caption"
    if RE_TBL_CAP.match(text):
        return "table_caption"
    if RE_HEADING.match(text) and len(text) < 80:
        return "heading"
    return "text"


def parse_page(page_no, chunk, styles, base_url, out_dir, session):
    bg_match = RE_BG.search(chunk)
    bg_rel = bg_match.group(1) if bg_match else None
    bg_image = None
    page_image_path = None

    if bg_rel:
        url = urljoin(base_url, bg_rel)
        resp = session.get(url, headers=UA, timeout=60)
        resp.raise_for_status()
        bg_image = Image.open(io.BytesIO(resp.content)).convert("RGB")
        page_image_path = f"pages/page-{page_no:03d}.png"
        dest = out_dir / "images" / page_image_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)

    items = []
    for top, left, cls, raw in RE_ITEM.findall(chunk):
        text = clean_text(raw)
        if not text:
            continue
        top, left = int(top), int(left)
        if is_header_footer(top, text):
            continue
        items.append(
            {"top": top, "left": left, "size": int(styles.get(cls, 13)), "text": text}
        )

    fragments = [it for it in items if is_formula_fragment(it["text"])]
    formula_clusters = cluster_formulas(fragments)

    blocks = []
    used = set()
    for idx, cluster in enumerate(formula_clusters):
        cluster = trim_horizontal_outliers(cluster)
        for it in cluster:
            used.add(id(it))
        bbox = bbox_of(cluster)
        image_path = None
        lines = []
        if bg_image:
            scale = bg_image.width / PAGE_W
            image_path = f"formulas/p{page_no:03d}_f{idx}.png"
            dest = out_dir / "images" / image_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            render_formula_image(bg_image, cluster, bbox, scale).save(dest)
            lines = detect_lines(bg_image, bbox, scale)
        raw_text = " ".join(
            it["text"] for it in sorted(cluster, key=lambda i: (i["top"], i["left"]))
        )
        blocks.append(
            {
                "kind": "formula",
                "top": bbox[1],
                "content": raw_text,
                "latex": None,
                "bbox": bbox,
                "image_path": image_path,
                "layout": {
                    "fragments": [
                        {
                            "text": it["text"],
                            "x": it["left"],
                            "y": it["top"],
                            "size": it["size"],
                        }
                        for it in sorted(cluster, key=lambda i: (i["top"], i["left"]))
                    ],
                    "lines": lines,
                },
            }
        )

    # 残りのテキストを行にまとめる
    rest = [it for it in items if id(it) not in used]
    rest.sort(key=lambda i: (i["top"], i["left"]))
    lines = []
    for it in rest:
        if lines and abs(it["top"] - lines[-1]["top"]) <= LINE_TOL:
            lines[-1]["parts"].append(it)
        else:
            lines.append({"top": it["top"], "parts": [it]})
    for line in lines:
        parts = sorted(line["parts"], key=lambda i: i["left"])
        text = " ".join(p["text"] for p in parts)
        blocks.append(
            {
                "kind": classify_line(text),
                "top": line["top"],
                "content": text,
                "latex": None,
                "bbox": None,
                "image_path": None,
            }
        )

    blocks.sort(key=lambda b: b["top"])
    return {
        "page_no": page_no,
        "has_background": bg_rel is not None,
        "image_path": page_image_path,
        "blocks": blocks,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://kikakurui.com/b8/B8267-2020-01.html")
    ap.add_argument("--pages", default="9-14", help="例: 9-14 / 9,12,20-25")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = parse_pages_arg(args.pages)

    session = requests.Session()
    print(f"fetch: {args.url}", file=sys.stderr)
    resp = session.get(args.url, headers=UA, timeout=120)
    resp.raise_for_status()
    resp.encoding = "utf-8"  # HTTP ヘッダに charset がなく Latin-1 と誤判定されるため明示
    src = resp.text

    title_m = re.search(r"<title>(.*?)</title>", src)
    title = htmllib.unescape(title_m.group(1)) if title_m else args.url
    code_m = re.search(r"JIS([A-Z]\s?\d+)", title)
    code = code_m.group(1).replace(" ", "") if code_m else "UNKNOWN"

    styles = dict(RE_STYLE.findall(src))
    parts = re.split(r"<!--\s*Page\s+(\d+)\s*-->", src)

    pages = []
    for i in range(1, len(parts) - 1, 2):
        page_no = int(parts[i])
        if page_no not in wanted:
            continue
        print(f"parse page {page_no}", file=sys.stderr)
        pages.append(parse_page(page_no, parts[i + 1], styles, args.url, out_dir, session))

    doc = {
        "code": code,
        "title": title,
        "source_url": args.url,
        "pages": pages,
    }
    (out_dir / "document.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    n_blocks = sum(len(p["blocks"]) for p in pages)
    n_formulas = sum(
        1 for p in pages for b in p["blocks"] if b["kind"] == "formula"
    )
    print(f"done: {len(pages)} pages, {n_blocks} blocks ({n_formulas} formulas)")


if __name__ == "__main__":
    main()
