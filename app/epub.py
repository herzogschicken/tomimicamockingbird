"""EPUB parsing: container.xml -> OPF -> manifest, spine, TOC -> blocks per spine item.

Output (epub.json):
  { title, author, sections: [ {idx, href, title, word_count, blocks: [...]} ] }
Block types: h1..h6, p, li (text) and img (src = file name in images/).
"""
import json
import posixpath
import re
import warnings
import zipfile
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup, NavigableString, Tag

try:
    from bs4 import XMLParsedAsHTMLWarning
    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except ImportError:
    pass

HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = HEADINGS | {
    "p", "li", "blockquote", "div", "pre", "dd", "dt", "td", "th", "figcaption",
    "section", "article", "aside", "header", "footer", "figure", "table", "tbody",
    "thead", "tr", "ul", "ol", "dl", "hr", "nav", "main", "body",
}
SKIP_TAGS = {"script", "style", "head", "title", "noscript"}
IMG_TAGS = {"img", "image"}
WS = re.compile(r"\s+")


def _norm(text):
    return WS.sub(" ", text.replace("­", "")).strip()


def _resolve(base_dir, href):
    return posixpath.normpath(posixpath.join(base_dir, unquote(href.split("#")[0])))


def _xml(z, name):
    return BeautifulSoup(z.read(name), "xml")


def _has_block_descendant(tag):
    return tag.find(lambda t: isinstance(t, Tag) and t.name in BLOCK_TAGS) is not None


class _Extractor:
    def __init__(self, z, names, img_dir, img_map):
        self.z, self.names, self.img_dir, self.img_map = z, names, img_dir, img_map

    def run(self, href):
        soup = BeautifulSoup(self.z.read(href), "lxml")
        for br in soup.find_all("br"):
            br.replace_with(" ")
        self.base = posixpath.dirname(href)
        self.blocks = []
        body = soup.body or soup
        self._walk(body)
        return self.blocks

    def _img(self, el):
        src = el.get("src") or el.get("xlink:href") or el.get("href")
        if not src or src.startswith("data:"):
            return
        full = _resolve(self.base, src)
        if full not in self.names:
            return
        if full not in self.img_map:
            ext = posixpath.splitext(full)[1].lower() or ".img"
            fname = f"img{len(self.img_map):04d}{ext}"
            (self.img_dir / fname).write_bytes(self.z.read(full))
            self.img_map[full] = fname
        self.blocks.append({"type": "img", "src": self.img_map[full], "alt": el.get("alt", "")})

    def _emit_text(self, kind, text):
        text = _norm(text)
        if text:
            self.blocks.append({"type": kind, "text": text})

    def _walk(self, el):
        run = []  # inline content collected between block children

        def flush():
            if run:
                self._emit_text("p", "".join(run))
                run.clear()

        for child in el.children:
            if isinstance(child, NavigableString):
                if type(child) is NavigableString:  # skip comments, CDATA, doctype
                    run.append(str(child))
                continue
            if not isinstance(child, Tag) or child.name in SKIP_TAGS:
                continue
            name = child.name
            if name in IMG_TAGS:
                flush()
                self._img(child)
            elif name in BLOCK_TAGS:
                flush()
                if _has_block_descendant(child):
                    self._walk(child)
                else:
                    for im in child.find_all(list(IMG_TAGS)):
                        self._img(im)
                    kind = name if name in HEADINGS else ("li" if name == "li" else "p")
                    self._emit_text(kind, child.get_text(""))
            else:
                # inline element (span, a, em, svg wrapper...)
                imgs = child.find_all(list(IMG_TAGS))
                if imgs and not _norm(child.get_text("")):
                    flush()
                    for im in imgs:
                        self._img(im)
                elif _has_block_descendant(child):
                    flush()
                    self._walk(child)
                else:
                    run.append(child.get_text(""))
        flush()


def _read_toc(z, names, manifest, spine_el):
    toc = {}
    nav = next((m for m in manifest.values() if "nav" in m["props"].split()), None)
    if nav and nav["href"] in names:
        soup = BeautifulSoup(z.read(nav["href"]), "lxml")
        navs = soup.find_all("nav")
        navel = next((n for n in navs if any("toc" in str(v) for v in n.attrs.values())),
                     navs[0] if navs else None)
        if navel:
            base = posixpath.dirname(nav["href"])
            for a in navel.find_all("a", href=True):
                label = _norm(a.get_text(" "))
                if label:
                    toc.setdefault(_resolve(base, a["href"]), label)
    ncx_id = spine_el.get("toc") if spine_el else None
    ncx = manifest.get(ncx_id) if ncx_id else None
    ncx = ncx or next((m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None)
    if ncx and ncx["href"] in names:
        nsoup = _xml(z, ncx["href"])
        base = posixpath.dirname(ncx["href"])
        for np_ in nsoup.find_all("navPoint"):
            label, content = np_.find("navLabel"), np_.find("content")
            if label and content and content.get("src"):
                text = _norm(label.get_text(" "))
                if text:
                    toc.setdefault(_resolve(base, content["src"]), text)
    return toc


def parse_epub(epub_path, out_dir):
    out_dir = Path(out_dir)
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(epub_path) as z:
        names = set(z.namelist())
        if "META-INF/container.xml" not in names:
            raise ValueError("Not a valid EPUB: META-INF/container.xml missing")
        rootfile = _xml(z, "META-INF/container.xml").find("rootfile")
        if not rootfile or not rootfile.get("full-path"):
            raise ValueError("Not a valid EPUB: no rootfile in container.xml")
        opf_path = rootfile["full-path"]
        opf_dir = posixpath.dirname(opf_path)
        opf = _xml(z, opf_path)

        t, a = opf.find("title"), opf.find("creator")
        title = _norm(t.get_text()) if t else None
        author = _norm(a.get_text()) if a else None

        manifest = {}
        for it in opf.find("manifest").find_all("item"):
            if it.get("id") and it.get("href"):
                manifest[it["id"]] = {
                    "href": _resolve(opf_dir, it["href"]),
                    "type": it.get("media-type", ""),
                    "props": it.get("properties", ""),
                }
        spine_el = opf.find("spine")
        spine = []
        if spine_el:
            for ref in spine_el.find_all("itemref"):
                m = manifest.get(ref.get("idref"))
                if m and m["href"] in names:
                    spine.append({**m, "linear": ref.get("linear", "yes") != "no"})

        toc = _read_toc(z, names, manifest, spine_el)

        img_map = {}
        extractor = _Extractor(z, names, img_dir, img_map)
        sections = []
        for item in spine:
            if "html" not in item["type"] and not item["href"].endswith((".xhtml", ".html", ".htm")):
                continue
            blocks = extractor.run(item["href"])
            words = sum(len(b["text"].split()) for b in blocks if "text" in b)
            first_h = next((b["text"] for b in blocks if b["type"] in HEADINGS), None)
            sections.append({
                "idx": len(sections),
                "href": item["href"],
                "title": toc.get(item["href"]) or first_h or posixpath.basename(item["href"]),
                "linear": item["linear"],
                "word_count": words,
                "blocks": blocks,
            })

    result = {"title": title, "author": author, "sections": sections}
    (out_dir / "epub.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result
