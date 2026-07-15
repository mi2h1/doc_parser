"""数式の切り出し画像を Claude のビジョン機能で LaTeX に変換する。

ANTHROPIC_API_KEY が未設定の場合は何もせず正常終了する（オプショナルな工程）。
document.json の formula ブロックに latex フィールドを書き込む。
"""
import base64
import json
import os
import sys
from pathlib import Path

PROMPT = """この画像は JIS 規格書から切り出した数式です。LaTeX に書き起こしてください。

- 数式本体のみを LaTeX で出力する（$ や \\[ \\] などの区切りは不要）
- 変数の添字（下付き・上付き）を正確に再現する
- 分数は \\frac、ルートは \\sqrt を使う
- 数式として読み取れない場合は NOT_A_FORMULA とだけ出力する
- 説明文は一切付けない"""


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY 未設定のため LaTeX 変換をスキップ", file=sys.stderr)
        return

    import anthropic

    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    doc_path = out_dir / "document.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))

    client = anthropic.Anthropic()
    converted = 0
    for page in doc["pages"]:
        for block in page["blocks"]:
            if block["kind"] != "formula" or not block.get("image_path"):
                continue
            image_file = out_dir / "images" / block["image_path"]
            if not image_file.exists():
                continue
            data = base64.standard_b64encode(image_file.read_bytes()).decode()
            response = client.messages.create(
                model="claude-opus-4-8",
                max_tokens=2048,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": data,
                                },
                            },
                            {"type": "text", "text": PROMPT},
                        ],
                    }
                ],
            )
            text = next(
                (b.text for b in response.content if b.type == "text"), ""
            ).strip()
            if text and text != "NOT_A_FORMULA":
                block["latex"] = text
                converted += 1
            print(f"p{page['page_no']} {block['image_path']}: {text[:60]}", file=sys.stderr)

    doc_path.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"LaTeX 変換: {converted} 件")


if __name__ == "__main__":
    main()
