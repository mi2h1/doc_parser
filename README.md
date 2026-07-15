# doc_parser

kikakurui.com に掲載されている JIS 規格の HTML（pdftohtml 形式）を構造化データに変換し、
Supabase に格納して GitHub Pages のビューアで閲覧するプロトタイプ。

このリポジトリのコードはすべて **GitHub Actions 上で実行** される。ローカルでの実行は不要。

## 構成

```
src/parse.py                 HTML → 構造化 JSON + 数式/ページ画像の切り出し
src/latex.py                 数式画像 → LaTeX（Claude ビジョン。API キーがあれば実行）
src/load.py                  JSON + 画像 → Supabase（テーブル + Storage）
supabase/schema.sql          テーブル定義（Supabase SQL Editor で実行）
.github/workflows/parse.yml  手動実行ワークフロー
docs/                        GitHub Pages 用の静的ビューア
```

## 処理の仕組み

対象サイトは PDF を pdftohtml で変換したもので、次の構造を持つ:

- ページごとに座標付き `<p>` 要素でテキストが配置されている
- 図・罫線・数式記号（分数の横棒など）はページ全体の背景 PNG に含まれる
- 数式は文字単位のバラバラな断片として配置されている

パーサーは座標クラスタリングで数式領域を検出し、背景 PNG から矩形を切り出す。
切り出した画像を Claude のビジョン機能で LaTeX に変換し、テキスト・見出し・
キャプションとともに順序付きブロックとして DB に格納する。

## セットアップ

### 1. Supabase

1. プロジェクトを作成（既存でも可）
2. SQL Editor で `supabase/schema.sql` を実行
3. Project Settings → API から以下を控える
   - Project URL
   - `anon` public キー
   - `service_role` キー

### 2. GitHub リポジトリの Secrets

Settings → Secrets and variables → Actions に登録:

| Secret | 用途 |
|---|---|
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | DB / Storage への書き込み |
| `ANTHROPIC_API_KEY` | （任意）数式の LaTeX 変換。未設定ならスキップ |

### 3. GitHub Pages

Settings → Pages → Source を「Deploy from a branch」、Branch を `main` / `docs` に設定。
`docs/config.js` に Project URL と anon キーを記入してコミット（anon キーは公開前提のキー）。

## 実行

Actions タブ → 「Parse JIS document」 → Run workflow。

- `url`: 対象ページ（デフォルト: B8267 圧力容器の設計 その1）
- `pages`: 対象ページ番号（デフォルト: `9-14`）

完了後、GitHub Pages のビューアで結果を確認できる。
`out/` の中間生成物はワークフローの Artifacts からもダウンロード可能。

## 注意

JIS 規格の著作権は日本規格協会に帰属する。本ツールは技術検証用であり、
データの社内利用にあたってはライセンス条件を確認すること。
