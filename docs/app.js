(async function () {
  const app = document.getElementById("app");
  const cfg = window.APP_CONFIG || {};

  if (!cfg.SUPABASE_URL || !cfg.SUPABASE_ANON_KEY) {
    app.innerHTML =
      '<div class="notice">docs/config.js に SUPABASE_URL と SUPABASE_ANON_KEY を設定してください。</div>';
    return;
  }

  const sb = supabase.createClient(cfg.SUPABASE_URL, cfg.SUPABASE_ANON_KEY);
  const storageUrl = (path) =>
    `${cfg.SUPABASE_URL}/storage/v1/object/public/jis-assets/${path}`;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));

  async function loadDocuments() {
    const { data, error } = await sb
      .from("documents")
      .select("id, code, title, created_at")
      .order("created_at", { ascending: false });
    if (error) throw error;
    return data;
  }

  async function loadBlocks(docId) {
    const [{ data: blocks, error: e1 }, { data: pages, error: e2 }] =
      await Promise.all([
        sb.from("blocks").select("*").eq("document_id", docId)
          .order("page_no").order("seq"),
        sb.from("pages").select("*").eq("document_id", docId).order("page_no"),
      ]);
    if (e1) throw e1;
    if (e2) throw e2;
    return { blocks, pages };
  }

  function renderBlocks(container, blocks, pages) {
    const pageImages = Object.fromEntries(
      pages.map((p) => [p.page_no, p.image_path])
    );
    let currentPage = null;
    const frag = document.createDocumentFragment();

    for (const b of blocks) {
      if (b.page_no !== currentPage) {
        currentPage = b.page_no;
        const marker = document.createElement("div");
        marker.className = "page-marker";
        let inner = `ページ ${currentPage}`;
        if (pageImages[currentPage]) {
          inner += ` <span class="page-image-link">｜<a href="${storageUrl(
            pageImages[currentPage]
          )}" target="_blank" rel="noopener">元ページ画像を開く</a></span>`;
        }
        marker.innerHTML = inner;
        frag.appendChild(marker);
      }

      const div = document.createElement("div");
      if (b.kind === "heading") {
        div.className = "b-heading";
        div.textContent = b.content;
      } else if (b.kind === "figure_caption" || b.kind === "table_caption") {
        div.className = "b-caption";
        div.textContent = b.content;
      } else if (b.kind === "formula") {
        div.className = "b-formula";
        if (b.latex) {
          const k = document.createElement("div");
          k.className = "katex-line";
          try {
            katex.render(b.latex, k, { displayMode: true, throwOnError: true });
            div.appendChild(k);
          } catch {
            div.innerHTML = `<code>${esc(b.latex)}</code>`;
          }
        }
        if (b.image_path) {
          const img = document.createElement("img");
          img.src = storageUrl(b.image_path);
          img.alt = "数式（元画像）";
          img.loading = "lazy";
          div.appendChild(img);
        }
        const raw = document.createElement("div");
        raw.className = "raw";
        raw.textContent = `抽出断片: ${b.content}`;
        div.appendChild(raw);
      } else {
        div.className = "b-text";
        div.textContent = b.content;
      }
      frag.appendChild(div);
    }
    container.appendChild(frag);
  }

  try {
    const docs = await loadDocuments();
    if (!docs.length) {
      app.innerHTML =
        '<div class="notice">まだデータがありません。GitHub Actions の「Parse JIS document」ワークフローを実行してください。</div>';
      return;
    }

    app.innerHTML = "";
    const select = document.createElement("select");
    select.id = "doc-select";
    for (const d of docs) {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = `${d.code} — ${d.title}`;
      select.appendChild(opt);
    }
    const content = document.createElement("div");
    app.append(select, content);

    async function show(docId) {
      content.innerHTML = "<p>読み込み中…</p>";
      const { blocks, pages } = await loadBlocks(docId);
      content.innerHTML = "";
      renderBlocks(content, blocks, pages);
    }

    select.addEventListener("change", () => show(select.value));
    await show(docs[0].id);
  } catch (err) {
    app.innerHTML = `<p class="error">エラー: ${esc(err.message || err)}</p>`;
  }
})();
