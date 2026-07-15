-- doc_parser 用スキーマ
-- Supabase ダッシュボード → SQL Editor に貼り付けて実行してください

create table if not exists documents (
  id          uuid primary key default gen_random_uuid(),
  code        text not null,
  title       text not null,
  source_url  text not null,
  created_at  timestamptz not null default now()
);

create table if not exists pages (
  id             uuid primary key default gen_random_uuid(),
  document_id    uuid not null references documents(id) on delete cascade,
  page_no        int  not null,
  has_background boolean not null default false,
  image_path     text,
  unique (document_id, page_no)
);

create table if not exists blocks (
  id          uuid primary key default gen_random_uuid(),
  document_id uuid not null references documents(id) on delete cascade,
  page_no     int  not null,
  seq         int  not null,
  kind        text not null check (kind in ('heading','text','formula','figure_caption','table_caption')),
  content     text,
  latex       text,
  bbox        jsonb,
  image_path  text,
  unique (document_id, page_no, seq)
);

create index if not exists blocks_doc_order on blocks (document_id, page_no, seq);

-- RLS: 匿名キーは読み取りのみ許可（書き込みはサービスロールキーが RLS をバイパス）
alter table documents enable row level security;
alter table pages     enable row level security;
alter table blocks    enable row level security;

create policy "public read documents" on documents for select using (true);
create policy "public read pages"     on pages     for select using (true);
create policy "public read blocks"    on blocks    for select using (true);
