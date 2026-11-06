const LOGIN_URL = "https://selectcourse.cgbf.nccu.edu.tw/login";

function stripTags(html) {
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .trim();
}

function normalizeName(name) {
  return name
    .replace(/英文授課/g, "")
    .replace(/\s+/g, "")
    .replace(/[（(].*?[)）]/g, "")
    .trim();
}

function parseDateTokens(dateStr) {
  // Remove "(原11/5停課改至11/26)" style parentheticals so they don't create a fake 11/5 active date
  const cleaned = dateStr.replace(/[（(]\s*原[^）)]*?[）)]/g, "");
  const tokens = cleaned.split(/[、,，]/);
  const items = [];
  for (const raw of tokens) {
    const t = raw.trim();
    const m = t.match(/(\d{1,2})\s*\/\s*(\d{1,2})(.*)/);
    if (!m) continue;
    const month = parseInt(m[1], 10);
    const day = parseInt(m[2], 10);
    const suffix = m[3].replace(/^[（(]|[)）]$/g, "").trim();
    const cancelled = /停課|取消|暫停/.test(suffix);
    items.push({ month, day, note: suffix, cancelled });
  }
  return items;
}

export default async () => {
  try {
    const res = await fetch(LOGIN_URL, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        Accept: "text/html,application/xhtml+xml",
      },
    });

    if (!res.ok) {
      return Response.json(
        { ok: false, error: `選課系統回應狀態碼 ${res.status}` },
        { status: 502 }
      );
    }

    const html = await res.text();

    // Extract the 課程公告 card body
    const cardMatch = html.match(
      /<div class="card-header[^"]*">\s*課程公告\s*<\/div>\s*<div class="card-body">([\s\S]*?)<\/div>\s*<\/div>/i
    );
    const cardBodyHtml = cardMatch ? cardMatch[1].trim() : "";

    // Extract 課程異動公告 bullet items
    const changeAnnouncements = [];
    const ulMatch = cardBodyHtml.match(/<ul[^>]*>([\s\S]*?)<\/ul>/i);
    if (ulMatch) {
      const liRegex = /<li[^>]*>([\s\S]*?)<\/li>/gi;
      let liMatch;
      while ((liMatch = liRegex.exec(ulMatch[1])) !== null) {
        const text = stripTags(liMatch[1]).replace(/\n+/g, " ｜ ");
        if (text) changeAnnouncements.push(text);
      }
    }

    // Parse course schedule lines from both <li> and <p> inside 課程公告
    const courseSchedules = {};

    if (ulMatch) {
      const liRegex = /<li[^>]*>([\s\S]*?)<\/li>/gi;
      let liMatch;
      while ((liMatch = liRegex.exec(ulMatch[1])) !== null) {
        const lines = stripTags(liMatch[1])
          .split("\n")
          .map((s) => s.trim())
          .filter(Boolean);
        if (lines.length >= 2) {
          const cName = normalizeName(lines[0]);
          const parsedDates = parseDateTokens(lines[1]);
          if (cName && parsedDates.length > 0) {
            courseSchedules[cName] = parsedDates;
          }
        }
      }
    }

    const pRegex = /<p[^>]*>([\s\S]*?)<\/p>/gi;
    let pMatch;
    while ((pMatch = pRegex.exec(cardBodyHtml)) !== null) {
      const pText = stripTags(pMatch[1]);
      const colonMatch = pText.match(/^([^：:\n]{2,25})[：:]\s*(.+)$/);
      if (colonMatch) {
        const cName = normalizeName(colonMatch[1]);
        const parsedDates = parseDateTokens(colonMatch[2]);
        if (cName && parsedDates.length > 0) {
          // Merge with any existing notes from <ul>
          const existing = courseSchedules[cName] || [];
          const byMd = new Map();
          for (const item of parsedDates) {
            byMd.set(`${item.month}-${item.day}`, { ...item });
          }
          for (const ex of existing) {
            const k = `${ex.month}-${ex.day}`;
            if (byMd.has(k)) {
              const cur = byMd.get(k);
              if (ex.note && !cur.note) cur.note = ex.note;
              if (ex.cancelled) cur.cancelled = true;
            } else {
              byMd.set(k, { ...ex });
            }
          }
          courseSchedules[cName] = Array.from(byMd.values());
        }
      }
    }

    return Response.json(
      {
        ok: true,
        checkedAt: new Date().toISOString(),
        sourceUrl: LOGIN_URL,
        changeAnnouncements,
        courseSchedules,
        courseCountInAnnouncement: Object.keys(courseSchedules).length,
      },
      {
        headers: {
          "Cache-Control": "no-store, max-age=0",
        },
      }
    );
  } catch (err) {
    return Response.json(
      { ok: false, error: err.message || String(err) },
      { status: 500 }
    );
  }
};
