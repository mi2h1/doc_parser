"""数式の切り出し画像を Groq のビジョンモデルで LaTeX に変換する。

GROQ_API_KEY が未設定の場合は何もせず正常終了する（オプショナルな工程）。
document.json の formula ブロックに latex フィールドを書き込む。

モデルは GROQ_MODEL で変更可能（デフォルト: Llama 4 Maverick）。
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"

PROMPT = """この画像は JIS 規格書から切り出した数式です。LaTeX に書き起こしてください。

- 数式本体のみを LaTeX で出力する（$ や \\[ \\] などの区切りは不要）
- 変数の添字（下付き・上付き）を正確に再現する
- 分数は \\frac、ルートは \\sqrt を使う
- 数式として読み取れない場合は NOT_A_FORMULA とだけ出力する
- 説明文は一切付けない"""


def transcribe(api_key: str, model: str, png_bytes: bytes) -> str:
    data_url = "data:image/png;base64," + base64.standard_b64encode(png_bytes).decode()
    body = {
        "model": model,
        "max_completion_tokens": 1024,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": PROMPT},
                ],
            }
        ],
    }
    for attempt in range(3):
        r = requests.post(
            GROQ_URL,
            json=body,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120,
        )
        if r.status_code == 429:
            wait = int(r.headers.get("retry-after", 2 ** (attempt + 1)))
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError("Groq API: リトライ上限に達しました (429)")


def clean_latex(text: str) -> str:
    """モデルが付けがちな囲い（```や$）を剥がす。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("latex"):
            t = t[5:]
    return t.strip().strip("$").strip()


def main():
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        print("GROQ_API_KEY 未設定のため LaTeX 変換をスキップ", file=sys.stderr)
        return

    model = os.environ.get("GROQ_MODEL", DEFAULT_MODEL)
    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    doc_path = out_dir / "document.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))

    converted = 0
    for page in doc["pages"]:
        for block in page["blocks"]:
            if block["kind"] != "formula" or not block.get("image_path"):
                continue
            image_file = out_dir / "images" / block["image_path"]
            if not image_file.exists():
                continue
            try:
                text = clean_latex(transcribe(api_key, model, image_file.read_bytes()))
            except Exception as e:
                print(f"p{page['page_no']} {block['image_path']}: エラー {e}", file=sys.stderr)
                continue
            if text and text != "NOT_A_FORMULA":
                block["latex"] = text
                converted += 1
            print(f"p{page['page_no']} {block['image_path']}: {text[:60]}", file=sys.stderr)

    doc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"LaTeX 変換: {converted} 件 (model: {model})")


if __name__ == "__main__":
    main()
