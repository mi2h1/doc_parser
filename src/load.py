"""document.json と画像を Supabase に投入する。

必要な環境変数:
  SUPABASE_URL              例: https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY サービスロールキー（RLS をバイパスして書き込む）

テーブルは supabase/schema.sql で事前に作成しておくこと。
画像は Storage バケット jis-assets（public）に格納する。
同じ source_url のドキュメントは削除してから入れ直す（再実行しても重複しない）。
"""
import json
import mimetypes
import os
import sys
from pathlib import Path

import requests

BUCKET = "jis-assets"


class Supabase:
    def __init__(self, url: str, key: str):
        self.url = url.rstrip("/")
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
        }

    def rest(self, method, table, *, params=None, body=None, prefer=None):
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        if prefer:
            headers["Prefer"] = prefer
        r = requests.request(
            method,
            f"{self.url}/rest/v1/{table}",
            params=params,
            json=body,
            headers=headers,
            timeout=60,
        )
        if not r.ok:
            raise RuntimeError(f"{method} {table}: {r.status_code} {r.text}")
        return r.json() if r.text else None

    def ensure_bucket(self):
        r = requests.post(
            f"{self.url}/storage/v1/bucket",
            json={"id": BUCKET, "name": BUCKET, "public": True},
            headers=self.headers,
            timeout=60,
        )
        if r.status_code not in (200, 201) and "already exists" not in r.text.lower():
            # 409 等で既存ならそのまま使う
            if r.status_code != 409:
                raise RuntimeError(f"bucket: {r.status_code} {r.text}")

    def upload(self, path: str, data: bytes):
        content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
        headers = dict(self.headers)
        headers["Content-Type"] = content_type
        headers["x-upsert"] = "true"
        r = requests.post(
            f"{self.url}/storage/v1/object/{BUCKET}/{path}",
            data=data,
            headers=headers,
            timeout=120,
        )
        if not r.ok:
            raise RuntimeError(f"upload {path}: {r.status_code} {r.text}")


def main():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY を設定してください", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "out")
    doc = json.loads((out_dir / "document.json").read_text(encoding="utf-8"))
    sb = Supabase(url, key)

    sb.ensure_bucket()

    # 冪等性: 同じ source_url の既存ドキュメントを削除（pages/blocks は CASCADE）
    sb.rest("DELETE", "documents", params={"source_url": f"eq.{doc['source_url']}"})

    rows = sb.rest(
        "POST",
        "documents",
        body={
            "code": doc["code"],
            "title": doc["title"],
            "source_url": doc["source_url"],
        },
        prefer="return=representation",
    )
    doc_id = rows[0]["id"]
    print(f"document: {doc_id}")

    image_root = out_dir / "images"
    prefix = f"{doc['code']}"

    page_rows = []
    block_rows = []
    for page in doc["pages"]:
        page_image = None
        if page["image_path"]:
            page_image = f"{prefix}/{page['image_path']}"
            sb.upload(page_image, (image_root / page["image_path"]).read_bytes())
        page_rows.append(
            {
                "document_id": doc_id,
                "page_no": page["page_no"],
                "has_background": page["has_background"],
                "image_path": page_image,
            }
        )
        for seq, block in enumerate(page["blocks"]):
            block_image = None
            if block.get("image_path"):
                block_image = f"{prefix}/{block['image_path']}"
                sb.upload(block_image, (image_root / block["image_path"]).read_bytes())
            block_rows.append(
                {
                    "document_id": doc_id,
                    "page_no": page["page_no"],
                    "seq": seq,
                    "kind": block["kind"],
                    "content": block["content"],
                    "latex": block.get("latex"),
                    "bbox": block.get("bbox"),
                    "image_path": block_image,
                }
            )

    sb.rest("POST", "pages", body=page_rows)
    sb.rest("POST", "blocks", body=block_rows)
    print(f"loaded: {len(page_rows)} pages, {len(block_rows)} blocks")


if __name__ == "__main__":
    main()
