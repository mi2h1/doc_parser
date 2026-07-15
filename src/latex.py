"""数式のレイアウトデータ（断片座標＋括線位置）から LaTeX を構造復元する。

画像 OCR は使わない。pdftohtml の出力に含まれる正確な座標情報と、
背景 PNG から検出した括線・ルート棒の位置をテキストで Gemini に渡し、
レイアウト構造から数式を組み立てさせる。
（合成画像はフォント差でズレるため、人間の目視検証用にのみ使う）

GEMINI_API_KEY が未設定の場合は何もせず正常終了する（オプショナルな工程）。
モデルは LATEX_MODEL で変更可能（デフォルト: gemini-2.5-flash）。
"""
import json
import os
import sys
import time
from pathlib import Path

import requests

DEFAULT_MODEL = "gemini-2.5-flash"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """あなたはJIS規格書（機械・圧力容器分野）のPDFから抽出された数式レイアウトデータを
LaTeXに復元する専門家です。

入力データの説明:
- テキスト断片: PDFから抽出した文字列と、その開始座標 (x=左端px, y=上端px)、フォントサイズ(px)
  - y が小さいほど上。フォントサイズが小さい断片は添字（下付き/上付き）の可能性が高い
  - 基準行より y が小さければ上付き・分子、大きければ下付き・分母の候補
- 横線: 背景画像から検出した水平線。分数の括線またはルート記号の上棒
  - sqrt=true はルート記号（左下に斜線を検出）、sqrt=false は分数の括線
  - 線の上にある断片が分子（またはルートの中身）、下にある断片が分母

添字の帰属規則（重要）:
- フォントサイズが小さい断片は、x 座標が「すぐ左にある通常サイズの文字」に最も近い
  変数の添字である。x 順で後続の記号（閉じ括弧など）より前に添字を付ける。
  例: σ(x=403) y(x=415, size=8) )(x=421) → \\sigma_{y}) であって \\sigma)y ではない

数字・記号断片の逆順補正（重要）:
- PDF変換の癖で、数字や記号のみの断片は文字順が逆転していることがある
  （例: 「3.0」は実際は「0.3」、「1(」は「(1」、「.0」は「0.」）
- 逆転している断片とそうでない断片が混在するため、隣接する断片を連結したとき
  工学式として自然な数値・構文になる読み方を選ぶこと
  例: 「1(」「+」「.0」「004」→ 「(1 + 0.004」
- 座屈・圧力容器の設計式では 0.3, 0.004, 1.5, 2/3 のような係数が典型的。
  「3.0Et」のような不自然な係数は「0.3Et」の逆転を疑う
- ギリシャ文字（σ, π等）はそのままLaTeXコマンドにする（\\sigma, \\pi）
- 「≦」は \\le、「≧」は \\ge

出力規則:
- LaTeX数式本体のみを1行で出力する（$ や \\[ \\] などの区切り、説明文は一切不要）
- 復元不能な場合は NOT_A_FORMULA とだけ出力する"""


def build_user_prompt(layout: dict) -> str:
    lines = ["テキスト断片:"]
    for f in layout["fragments"]:
        lines.append(f'  "{f["text"]}"  x={f["x"]} y={f["y"]} size={f["size"]}')
    if layout.get("lines"):
        lines.append("横線:")
        for ln in layout["lines"]:
            kind = "ルート上棒(sqrt=true)" if ln["sqrt"] else "分数の括線(sqrt=false)"
            lines.append(f"  x={ln['x0']}..{ln['x1']} y={ln['y']}  {kind}")
    else:
        lines.append("横線: なし（分数・ルートを含まない式）")
    lines.append("\nこのレイアウトをLaTeXに復元してください。")
    return "\n".join(lines)


def call_gemini(api_key: str, model: str, layout: dict) -> str:
    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [{"text": build_user_prompt(layout)}]}
        ],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 8192,
            # 説明文の混入を防ぎ、LaTeX 本体だけを JSON で返させる
            "responseMimeType": "application/json",
            "responseSchema": {
                "type": "OBJECT",
                "properties": {"latex": {"type": "STRING"}},
                "required": ["latex"],
            },
        },
    }
    for attempt in range(4):
        r = requests.post(
            GEMINI_URL.format(model=model),
            json=body,
            headers={"x-goog-api-key": api_key},
            timeout=120,
        )
        if r.status_code in (429, 503):
            time.sleep(2 ** (attempt + 2))
            continue
        r.raise_for_status()
        data = r.json()
        parts = data["candidates"][0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        return json.loads(text).get("latex", "").strip()
    raise RuntimeError("Gemini API: リトライ上限に達しました")


def clean_latex(text: str) -> str:
    """モデルが付けがちな囲い（```や$）を剥がす。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("latex"):
            t = t[5:]
    return t.strip().strip("$").strip()


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY 未設定のため LaTeX 変換をスキップ", file=sys.stderr)
        return

    model = os.environ.get("LATEX_MODEL", DEFAULT_MODEL)
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    doc_path = out_dir / "document.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))

    converted = 0
    for page in doc["pages"]:
        for block in page["blocks"]:
            if block["kind"] != "formula" or not block.get("layout"):
                continue
            try:
                text = clean_latex(call_gemini(api_key, model, block["layout"]))
            except Exception as e:
                print(f"p{page['page_no']}: エラー {e}", file=sys.stderr)
                continue
            if text and text != "NOT_A_FORMULA":
                block["latex"] = text
                converted += 1
            print(f"p{page['page_no']}: {text[:80]}", file=sys.stderr)

    doc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"LaTeX 変換: {converted} 件 (model: {model})")


if __name__ == "__main__":
    main()
