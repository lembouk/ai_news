#!/usr/bin/env python3
"""Brief IA quotidien -> Discord.

Sans clé IA  : liens + extraits (mode simple).
Avec ANTHROPIC_API_KEY (ou OPENAI_API_KEY) : titres traduits en français, résumés
d'une phrase et section « À ne pas louper ». L'IA ne voit ni ne produit jamais d'URL.

Usage : python ia_tracker.py [--dry-run]"""
import html, json, os, re, sys, time
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

MAX_AGE_H = 36            # ignore les articles plus vieux
CANDIDATES_PER_CAT = 8    # nb d'articles soumis à l'IA par catégorie
MAX_TOP = 5               # "À ne pas louper" : 5 max
MAX_PER_CAT = 3           # liens affichés par catégorie
SEEN_FILE = Path(__file__).with_name("seen.json")
UA = "ia-tracker/2.0 (personal daily brief)"   # Discord refuse l'UA Python par défaut
CAT_COLORS = [0x5865F2, 0x57F287, 0xFEE75C, 0xEB459E, 0xFAA61A]


def gnews(q):
    return "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{q} when:1d", "hl": "en-US", "gl": "US", "ceid": "US:en"})

# Catégorie -> liste de (nom, type, url_ou_requête). Modifie librement.
SOURCES = {
    "🧠 Nouveaux modèles & labos": [
        ("OpenAI", "rss", "https://openai.com/news/rss.xml"),
        ("Google AI", "rss", "https://blog.google/technology/ai/rss/"),
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


# ───────────────────────── Récupération ─────────────────────────
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _short(text, n):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _clean(raw, n=300):
    return _short(html.unescape(re.sub(r"<[^>]+>", " ", raw or "")), n)


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)                                   # RSS
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))       # Atom
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse_feed(data, source):
    """RSS 2.0 ou Atom -> [{title, link, ts, source, snippet}]"""
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
            elif name in ("description", "summary", "content", "encoded"):
                f.setdefault("desc", c.text or "")
        if not f.get("title") or not f.get("link"):
            continue
        ts = _parse_date(f.get("pubDate") or f.get("published") or f.get("updated"))
        out.append({"title": f["title"], "link": f["link"], "ts": ts, "source": source,
                    "snippet": _clean(f.get("desc"))})
    return out


def fetch_rss(name, url):
    items = parse_feed(http_get(url), name)
    if name == "Google News":      # "Titre - Média" -> vrai média en source, extrait inutile
        for it in items:
            m = re.match(r"(.*\S)\s+-\s+([^-]{2,40})$", it["title"])
            if m:
                it["title"], it["source"] = m.group(1), m.group(2).strip()
            it["snippet"] = ""
    return items


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
                    "ts": datetime.fromtimestamp(h["created_at_i"], timezone.utc),
                    "source": name, "snippet": ""})
    return out


def fetch_hf_trending(name, _):
    data = json.loads(http_get("https://huggingface.co/api/models?sort=trendingScore&limit=8"))
    now = datetime.now(timezone.utc)
    return [{"title": f"{m['id']} ({m.get('pipeline_tag') or 'model'}, {m.get('likes', 0)} likes)",
             "link": f"https://huggingface.co/{m['id']}", "ts": now, "source": name,
             "snippet": "Modèle tendance sur Hugging Face."} for m in data]


FETCHERS = {"rss": fetch_rss, "hn": fetch_hn, "hf_trending": fetch_hf_trending}


def norm_title(t):
    return re.sub(r"\W+", " ", t.lower()).strip()


def norm_link(u):
    p = urllib.parse.urlsplit(u)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), "", ""))


def collect(seen, cap):
    """-> {catégorie: [items]} ; chaque item porte ses 'keys' de dédoublonnage."""
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
            it["keys"] = keys
            kept.append(it)
            if len(kept) >= cap:
                break
        if kept:
            result[cat] = kept
    return result


# ───────────────────────── Curation par IA ─────────────────────────
SYSTEM = """Tu es l'éditeur d'une newsletter quotidienne française sur l'IA, pour un lecteur curieux mais pressé.
On te donne des articles candidats (JSON). Réponds UNIQUEMENT par un objet JSON :
{"top":[{"id":0,"titre":"...","resume":"..."}],"autres":[{"id":0,"titre":"...","resume":"..."}]}

Règles :
- "top" : 3 à 5 infos à ne pas rater aujourd'hui : sortie de modèle majeur, annonce importante d'un grand labo, décision politique ou réglementaire marquante, grosse levée de fonds ou rachat, rupture open source. Moins de 3 s'il n'y a rien d'important. Si plusieurs articles couvrent le même événement, n'en garde qu'un.
- "autres" : les meilleurs articles restants, 3 maximum par catégorie ("cat"), sans doublon avec "top" ni entre eux. Ignore pubs, listes « top 10 » et opinions sans fait nouveau.
- "titre" : traduit en français, clair, 90 caractères max. Garde les noms propres et noms de modèles tels quels.
- "resume" : une seule phrase en français, 140 caractères max, factuelle, uniquement à partir du titre et de l'extrait. N'invente rien : si l'extrait est vide, reformule le titre ou laisse "".
- Utilise uniquement les "id" fournis. N'écris jamais d'URL."""


def _llm_request(user):
    """Renvoie le texte de la réponse, ou None si aucune clé IA n'est configurée."""
    if ANTHROPIC_KEY:
        body = {"model": ANTHROPIC_MODEL, "max_tokens": 3000, "system": SYSTEM,
                "messages": [{"role": "user", "content": user}]}
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(), method="POST",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json", "User-Agent": UA})
        out = json.loads(urllib.request.urlopen(req, timeout=90).read())
        return "".join(b.get("text", "") for b in out["content"])
    if OPENAI_KEY:
        body = {"model": OPENAI_MODEL, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": SYSTEM},
                             {"role": "user", "content": user}]}
        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions", data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {OPENAI_KEY}",
                     "content-type": "application/json", "User-Agent": UA})
        out = json.loads(urllib.request.urlopen(req, timeout=90).read())
        return out["choices"][0]["message"]["content"]
    return None


def _json_from(text):
    s, e = text.find("{"), text.rfind("}")
    return json.loads(text[s:e + 1])


def curate(result):
    """-> liste de sections (titre, couleur, items) ou None si l'IA est indisponible."""
    cands = {}
    for cat, items in result.items():
        for it in items:
            cands[len(cands)] = (cat, it)
    payload = [{"id": i, "cat": cat, "source": it["source"], "title": it["title"],
                "extrait": it["snippet"]} for i, (cat, it) in cands.items()]
    try:
        raw = _llm_request(json.dumps(payload, ensure_ascii=False))
        if raw is None:
            return None
        data = _json_from(raw)
    except Exception as e:
        print(f"⚠️  IA indisponible ({type(e).__name__}: {e}) -> mode simple", file=sys.stderr)
        return None

    used = set()

    def take(entries, limit=None):
        out = []
        for e in entries or []:
            try:
                i = int(e["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if i not in cands or i in used:
                continue
            used.add(i)
            cat, it = cands[i]
            out.append({"cat": cat, "titre": _short(e.get("titre") or it["title"], 100),
                        "resume": _short(e.get("resume"), 180),
                        "link": it["link"], "source": it["source"], "keys": it["keys"]})
            if limit and len(out) >= limit:
                break
        return out

    top = take(data.get("top"), MAX_TOP)
    autres = take(data.get("autres"))
    sections = []
    if top:
        sections.append(("🔥 À ne pas louper", 0xED4245, top))
    for n, cat in enumerate(result):
        its = [x for x in autres if x["cat"] == cat][:MAX_PER_CAT]
        if its:
            sections.append((cat, CAT_COLORS[n % len(CAT_COLORS)], its))
    return sections or None


# ───────────────────────── Mode sans IA : classement par signaux ─────────────────────────
STRONG = re.compile(r"\b(GPT[- ]?\d*|OpenAI|Anthropic|Claude|Opus|Sonnet|Haiku|Gemini|Llama|Mistral|"
                    r"DeepSeek|Grok|Qwen)\b", re.I)
RELEASE = re.compile(r"\b(launch\w*|releas\w*|unveil\w*|announc\w*|introduc\w*|debut\w*|"
                     r"open[- ]sourc\w*|ships?|rolls? out|beats?|surpass\w*)\b", re.I)
POLICY = re.compile(r"\b(Trump|White House|Congress|Senate|EU|AI Act|ban\w*|laws?|regulat\w*|lawsuit|"
                    r"sues?|sued|antitrust|executive order|FTC|China|copyright)\b", re.I)
MONEY = re.compile(r"\b(billion|acqui\w*|valuation|IPO)\b", re.I)
PRIMARY = {"OpenAI", "Google AI", "Hugging Face"}     # blogs officiels : source de première main
STOP = {"with", "that", "this", "from", "have", "will", "about", "after", "into", "over", "more",
        "your", "what", "their", "than", "they", "its", "new", "says", "could", "how", "why"}


def _words(title):
    return {w for w in re.findall(r"[a-z0-9]{3,}", title.lower()) if w not in STOP}


def _same_story(a, b):
    inter = len(a & b)
    return inter >= 2 and inter / max(1, min(len(a), len(b))) >= 0.5


def score_items(result):
    """Attribue it['score'] à chaque article (titre + source + reprise par d'autres sources)."""
    flat = [it for items in result.values() for it in items]
    for it in flat:
        it["_w"] = _words(it["title"])
    for it in flat:
        t = it["title"]
        sc = 2 * bool(STRONG.search(t)) + 2 * bool(RELEASE.search(t)) \
            + 2 * bool(POLICY.search(t)) + 1 * bool(MONEY.search(t))
        sc += 1 if it["source"] in PRIMARY else 0
        sc += 0.5 if it["snippet"] else 0          # préfère la version avec extrait
        m = re.search(r"\((\d+) pts\)", t)
        if m:
            sc += min(int(m.group(1)) / 150, 3)
        others = {o["source"] for o in flat if o is not it and o["source"] != it["source"]
                  and _same_story(it["_w"], o["_w"])}
        sc += 2 * min(len(others), 2)                 # même sujet dans d'autres médias
        it["score"] = sc
    return flat


TOP_MIN_SCORE = 4


def simple_sections(result):
    flat = score_items(result)

    def card(it):
        return {"cat": next(c for c, items in result.items() if it in items),
                "titre": _short(it["title"], 100), "resume": _short(it["snippet"], 160),
                "link": it["link"], "source": it["source"], "keys": it["keys"]}

    top, chosen = [], []
    for it in sorted(flat, key=lambda i: -i["score"]):
        if it["score"] < TOP_MIN_SCORE or len(top) >= MAX_TOP:
            break
        if any(_same_story(it["_w"], c["_w"]) for c in chosen):    # même info déjà retenue
            continue
        chosen.append(it)
        top.append(card(it))
    sections = []
    if top:
        sections.append(("🔥 À ne pas louper", 0xED4245, top))
    shown = list(chosen)                                  # histoires déjà affichées (top inclus)
    for n, (cat, items) in enumerate(result.items()):
        its = []
        for i in sorted(items, key=lambda i: -i["score"]):
            if len(its) >= MAX_PER_CAT:
                break
            if i in shown or any(_same_story(i["_w"], c["_w"]) for c in shown):
                continue                                  # même histoire déjà dans le brief
            shown.append(i)
            its.append(card(i))
        if its:
            sections.append((cat, CAT_COLORS[n % len(CAT_COLORS)], its))
    return sections


# ───────────────────────── Discord ─────────────────────────
def _title(t):
    return t.replace("[", "(").replace("]", ")")


def build_embeds(sections):
    embeds = []
    for name, color, items in sections:
        top = name.startswith("🔥")
        blocks, size = [], 0
        for n, it in enumerate(items, 1):
            tail = (it["resume"] + " · " if it["resume"] else "") + f"*{it['source']}*"
            head = f"**{n}.** " if top else "• "
            block = f"{head}[{_title(it['titre'])}]({it['link']})\n{tail}"
            if size + len(block) > 3500:       # garde-fou : un embed reste petit
                break
            blocks.append(block)
            size += len(block) + 2
        if blocks:
            embeds.append({"title": name, "color": color,
                           "description": ("\n\n" if top else "\n").join(blocks)})
    return embeds


def pack_messages(embeds, limit=5000):
    """Discord refuse > 6000 caractères d'embeds par message : on découpe."""
    msgs, cur, size = [], [], 0
    for e in embeds:
        n = len(e["title"]) + len(e["description"])
        if cur and (size + n > limit or len(cur) >= 10):
            msgs.append(cur)
            cur, size = [], 0
        cur.append(e)
        size += n
    if cur:
        msgs.append(cur)
    return msgs


def _post(payload):
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


def send_discord(embeds):
    ok = True
    for i, msg in enumerate(pack_messages(embeds)):
        payload = {"embeds": msg}
        if i == 0:
            payload["content"] = f"☀️ **Brief IA — {datetime.now().strftime('%d/%m/%Y')}**"
        ok = _post(payload) and ok
        time.sleep(1)
    return ok


def main():
    dry = "--dry-run" in sys.argv or not WEBHOOK
    seen_list = json.loads(SEEN_FILE.read_text()) if SEEN_FILE.exists() else []
    use_llm = bool(ANTHROPIC_KEY or OPENAI_KEY)
    result = collect(set(seen_list), CANDIDATES_PER_CAT)
    if not result:
        print("Rien de nouveau aujourd'hui.")
        return

    sections = curate(result) if use_llm else None
    curated = sections is not None
    if not curated:
        sections = simple_sections(result)
        print("Mode sans IA : classement par signaux.")

    # Curation : tous les candidats jugés sont « vus ». Mode simple : seulement ceux affichés.
    pool = [it for items in result.values() for it in items] if curated \
        else [it for _, _, items in sections for it in items]
    new_keys = set().union(*(it["keys"] for it in pool))

    embeds = build_embeds(sections)
    if dry:
        for e in embeds:
            print(f"\n=== {e['title']} ===\n{e['description']}")
        print("\n(dry-run : rien envoyé, seen.json inchangé)")
        return
    if send_discord(embeds):
        SEEN_FILE.write_text(json.dumps((seen_list + sorted(new_keys))[-1500:], indent=0))
        print(f"✅ Envoyé ({'IA' if curated else 'simple'}) : "
              f"{sum(len(i) for _, _, i in sections)} liens")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
