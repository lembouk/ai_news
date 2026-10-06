#!/usr/bin/env python3
"""Brief IA quotidien -> Discord. Bibliothèque standard uniquement, aucune clé API
(seulement le webhook Discord). Usage : python ia_tracker.py [--dry-run]"""
import json, os, re, sys, time
import urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
MAX_AGE_H = 36          # ignore les articles plus vieux
PER_CATEGORY = 6        # nb max de liens par catégorie
SEEN_FILE = Path(__file__).with_name("seen.json")
UA = "ia-tracker/1.0 (personal daily brief)"   # Discord refuse l'UA Python par défaut


def gnews(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})

# Catégorie -> liste de (nom, type, url_ou_requête). Modifie librement.
SOURCES = {
    "🧠 Nouveaux modèles & labos": [
        ("OpenAI", "rss", "https://openai.com/news/rss.xml"),
        ("Google DeepMind", "rss", "https://deepmind.google/blog/rss.xml"),
        ("Hugging Face", "rss", "https://huggingface.co/blog/feed.xml"),
        ("Google News", "rss", gnews('"Anthropic" OR "Claude" OR "Gemini" OR "GPT" new model release')),
    ],
    "🔓 Open source": [
        ("HF trending", "hf_trending", ""),
        ("Hacker News", "hn", "open source LLM"),
    ],
    "📰 Actu & tendances tech": [
        ("The Verge", "rss", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml"),
        ("TechCrunch", "rss", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("MIT Tech Review", "rss", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ],
    "💼 Économie & régulation": [
        ("Google News", "rss", gnews('"AI regulation" OR "AI Act" OR "AI law"')),
        ("Google News", "rss", gnews('AI startup funding round')),
    ],
    "🛠️ Cas d'usage": [
        ("Hacker News", "hn", "LLM"),
        ("Hacker News", "hn", "AI agents"),
    ],
}


def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)                       # RSS (RFC 822)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))  # Atom (ISO)
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(data, source):
    """RSS 2.0 ou Atom -> [{title, link, ts, source}]"""
    out = []
    root = ET.fromstring(data)
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        f = {}
        for c in el:
            name = _local(c.tag)
            if name == "link":
                f.setdefault("link", c.get("href") or (c.text or "").strip())
            elif name in ("title", "pubDate", "published", "updated"):
                f.setdefault(name, (c.text or "").strip())
        if not f.get("title") or not f.get("link"):
            continue
        ts = _parse_date(f.get("pubDate") or f.get("published") or f.get("updated"))
        out.append({"title": f["title"], "link": f["link"], "ts": ts, "source": source})
    return out


def fetch_rss(name, url):
    return parse_feed(http_get(url), name)


def fetch_hn(name, query):
    cutoff = int(time.time()) - MAX_AGE_H * 3600
    qs = urllib.parse.urlencode({
        "query": query, "tags": "story", "hitsPerPage": 8,
        "numericFilters": f"created_at_i>{cutoff},points>80"})
    data = json.loads(http_get("https://hn.algolia.com/api/v1/search_by_date?" + qs))
    out = []
    for h in data.get("hits", []):
        link = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append({"title": f"{h['title']} ({h.get('points', 0)} pts)", "link": link,
                    "ts": datetime.fromtimestamp(h["created_at_i"], timezone.utc), "source": name})
    return out


def fetch_hf_trending(name, _):
    data = json.loads(http_get("https://huggingface.co/api/models?sort=trendingScore&limit=8"))
    now = datetime.now(timezone.utc)
    return [{"title": f"{m['id']} · {m.get('pipeline_tag') or 'model'} · ❤ {m.get('likes', 0)}",
             "link": f"https://huggingface.co/{m['id']}", "ts": now, "source": name}
            for m in data]


FETCHERS = {"rss": fetch_rss, "hn": fetch_hn, "hf_trending": fetch_hf_trending}


def norm_title(t):
    t = re.sub(r"\s+-\s+[^-]{2,40}$", "", t)   # retire le " - Média" de Google News
    return re.sub(r"\W+", " ", t.lower()).strip()


def norm_link(u):
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def collect(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_H)
    batch_keys, result = set(), {}
    for cat, sources in SOURCES.items():
        items = []
        for name, kind, arg in sources:
            try:
                items += FETCHERS[kind](name, arg)
            except Exception as e:                      # une source en panne ne bloque pas le reste
                print(f"⚠️  {name} ({kind}) : {e}", file=sys.stderr)
        items.sort(key=lambda i: i["ts"] or datetime.now(timezone.utc), reverse=True)
        kept = []
        for it in items:
            if it["ts"] and it["ts"] < cutoff:
                continue
            keys = {norm_link(it["link"]), "t:" + norm_title(it["title"])}
            if keys & seen or keys & batch_keys:
                continue
            batch_keys |= keys
            kept.append(it)
            if len(kept) >= PER_CATEGORY:
                break
        if kept:
            result[cat] = kept
    return result, batch_keys


def build_embeds(result):
    embeds = []
    for cat, items in result.items():
        lines = []
        for it in items:
            title = it["title"].replace("[", "(").replace("]", ")")
            title = title if len(title) <= 110 else title[:107] + "…"
            lines.append(f"• [{title}]({it['link']}) · *{it['source']}*")
        embeds.append({"title": cat, "description": "\n".join(lines)[:4000], "color": 0x5865F2})
    return embeds


def send_discord(embeds):
    payload = {"content": f"☀️ **Brief IA — {datetime.now().strftime('%d/%m/%Y')}**",
               "embeds": embeds[:10]}
    for attempt in range(2):
        req = urllib.request.Request(
            WEBHOOK, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(float(e.headers.get("Retry-After", 2)))
                continue
            print(f"❌ Discord HTTP {e.code} : {e.read()[:200]}", file=sys.stderr)
            return False
    return False


def main():
    dry = "--dry-run" in sys.argv or not WEBHOOK
    seen_list = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    result, new_keys = collect(set(seen_list))
    if not result:
        print("Rien de nouveau aujourd'hui.")
        return
    embeds = build_embeds(result)
    if dry:
        for e in embeds:
            print(f"\n{e['title']}\n{e['description']}")
        print("\n(dry-run : rien envoyé, seen.json inchangé)")
        return
    if send_discord(embeds):
        SEEN_FILE.write_text(json.dumps((seen_list + sorted(new_keys))[-1500:], indent=0))
        print(f"✅ Envoyé : {sum(len(v) for v in result.values())} liens")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
