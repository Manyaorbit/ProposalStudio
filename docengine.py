"""Reads a Word sample, splits it into sections, and rewrites chosen sections
while keeping the sample's own paragraph styles, fonts, colours and spacing.

How formatting is kept: new paragraphs are *clones* of paragraphs that already
exist in the sample (a body paragraph, a sub-heading, a bullet). Only the text
changes. Everything not rewritten (fixed sections, tables, images, headers,
footers, page setup) is left exactly as it was in the file.
"""
import copy
import io
import re
import zipfile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph

W_P, W_TBL, W_SDT = qn("w:p"), qn("w:tbl"), qn("w:sdt")
W_R, W_T, W_TAB, W_BR = qn("w:r"), qn("w:t"), qn("w:tab"), qn("w:br")
W_RPR, W_PPR, W_B, W_BCS = qn("w:rPr"), qn("w:pPr"), qn("w:b"), qn("w:bCs")
W_VAL, W_TYPE = qn("w:val"), qn("w:type")
W_SECTPR, W_NUMPR, W_OUTLINE = qn("w:sectPr"), qn("w:numPr"), qn("w:outlineLvl")
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

SPECIAL_TAGS = {qn(t) for t in ("w:drawing", "w:pict", "w:object", "w:fldChar",
                                "w:fldSimple", "w:instrText", "w:sectPr")}

MAX_ZIP_ENTRIES = 3000
MAX_UNCOMPRESSED = 200 * 1024 * 1024

FIXED_HINTS = re.compile(
    r"\b(about|who we are|our (company|team|story|approach|experience|credentials|values|"
    r"methodology|partners?)|why (us|choose)|team|credentials?|certifications?|terms|conditions|"
    r"legal|confidential\w*|disclaimer|contact|appendix|annexure|company (overview|profile)|"
    r"case stud\w+|methodology|payment|warrant\w+|privacy|assumptions)\b", re.I)
VARIABLE_HINTS = re.compile(
    r"\b(client|customer|your|understanding|requirements?|needs?|objectives?|scope|solution|"
    r"proposal for|executive summary|background|challenges?|opportunit\w+|benefits?|recommend\w*|"
    r"deliverables?|timeline|investment|pricing|approach for|overview of)\b", re.I)


class DocError(Exception):
    """A problem with the uploaded document that is safe to show to the user."""


# --------------------------------------------------------------------------- validation / loading

def validate_docx_bytes(data: bytes):
    if not data[:2] == b"PK":
        raise DocError("That doesn't look like a .docx file (password-protected Word files aren't supported).")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        infos = zf.infolist()
    except zipfile.BadZipFile:
        raise DocError("That .docx file is damaged or isn't a real Word document.")
    if len(infos) > MAX_ZIP_ENTRIES:
        raise DocError("That document has too many internal parts.")
    names = {i.filename for i in infos}
    if "word/document.xml" not in names or "[Content_Types].xml" not in names:
        raise DocError("That file isn't a valid Word document.")
    total = 0
    for i in infos:
        n = i.filename
        if n.startswith("/") or ".." in n.split("/") or "\\" in n:
            raise DocError("That document contains unsafe internal paths.")
        if n.lower().endswith("vbaproject.bin"):
            raise DocError("Documents containing macros are not accepted.")
        if i.flag_bits & 0x1:
            raise DocError("Encrypted documents are not supported.")
        total += i.file_size
        if i.file_size > 1024 * 1024 and i.compress_size and i.file_size / i.compress_size > 200:
            raise DocError("That document looks like a compression bomb and was rejected.")
    if total > MAX_UNCOMPRESSED:
        raise DocError("That document is too large once unpacked.")


def p_text(p) -> str:
    out = []
    for d in p.iter():
        if d.tag == W_T:
            out.append(d.text or "")
        elif d.tag in (W_TAB, W_BR):
            out.append(" ")
    return "".join(out).strip()


def run_text(r) -> str:
    out = []
    for d in r:
        if d.tag == W_T:
            out.append(d.text or "")
        elif d.tag in (W_TAB, W_BR):
            out.append(" ")
    return "".join(out)


def run_is_bold(r) -> bool:
    rpr = r.find(W_RPR)
    if rpr is None:
        return False
    b = rpr.find(W_B)
    return b is not None and b.get(W_VAL) not in ("0", "false", "off")


def is_special_p(p) -> bool:
    for d in p.iter():
        if d.tag in SPECIAL_TAGS:
            return True
        if d.tag == W_BR and d.get(W_TYPE) == "page":
            return True
    return False


def has_image(el) -> bool:
    return any(d.tag in (qn("w:drawing"), qn("w:pict")) for d in el.iter())


def is_boldline(p) -> bool:
    runs = [r for r in p.iter(W_R) if run_text(r).strip()]
    return bool(runs) and all(run_is_bold(r) for r in runs)


def is_bullet(par: Paragraph) -> bool:
    ppr = par._p.pPr
    if ppr is not None and ppr.find(W_NUMPR) is not None:
        return True
    return any("list" in n for n in _style_names(par))


def _style_names(par: Paragraph):
    names, st = [], None
    try:
        st = par.style
    except Exception:
        return names
    while st is not None and len(names) < 8:
        names.append((st.name or "").lower())
        st = st.base_style
    return names


def heading_level(par: Paragraph):
    for n in _style_names(par):
        m = re.match(r"heading (\d)$", n)
        if m:
            return int(m.group(1))
    ppr = par._p.pPr
    if ppr is not None:
        ol = ppr.find(W_OUTLINE)
        if ol is not None:
            try:
                v = int(ol.get(W_VAL))
                if v < 9:
                    return v + 1
            except (TypeError, ValueError):
                pass
    return None


def p_markup(p) -> str:
    """Paragraph text with **bold** around partly-bold lead-ins (e.g. 'Label: text')."""
    segs = []
    for r in p.iter(W_R):
        t = run_text(r)
        if not t:
            continue
        b = run_is_bold(r)
        if segs and segs[-1][1] == b:
            segs[-1][0] += t
        else:
            segs.append([t, b])
    if not segs:
        return ""
    if all(b for _, b in segs):
        return re.sub(r"\s+", " ", "".join(t for t, _ in segs)).strip()
    out = []
    for t, b in segs:
        if b and t.strip():
            lead, trail = t[: len(t) - len(t.lstrip())], t[len(t.rstrip()):]
            out.append(f"{lead}**{t.strip()}**{trail}")
        else:
            out.append(t)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def parse_inline(text: str):
    """'a **b** c' -> [('a ', False), ('b', True), (' c', False)]"""
    parts = re.split(r"(\*\*[^*]+?\*\*)", text)
    out = []
    for part in parts:
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], True))
        else:
            out.append((part.replace("**", ""), False))
    return out or [("", False)]


# --------------------------------------------------------------------------- parsing into sections

class Section:
    def __init__(self, idx, title, heading_el):
        self.id = f"s{idx}"
        self.title = title
        self.heading = heading_el
        self.body = []
        self.mode = "aligned" if idx == 0 else "structured"

    def texts(self):
        return [p_text(e) for e in self.body if e.tag == W_P and not is_special_p(e) and p_text(e)]


class Parsed:
    def __init__(self, doc):
        self.doc = doc
        self.sections = []
        self.top = None
        self._parse()

    # -- classification
    def _levels(self, paras):
        return {id(el): heading_level(par) for el, par in paras}

    def _parse(self):
        doc = self.doc
        body = doc.element.body
        elems = [c for c in body.iterchildren() if c.tag in (W_P, W_TBL, W_SDT)]
        paras = [(e, Paragraph(e, doc)) for e in elems if e.tag == W_P]
        self._par = {id(e): p for e, p in paras}
        levels = self._levels(paras)
        self._levels_map = levels
        found = [v for v in levels.values() if v is not None]
        styled = bool(found)
        self.top = min(found) if styled else None

        def is_top(el):
            if el.tag != W_P or not p_text(el):
                return False
            if styled:
                return levels.get(id(el)) == self.top
            par = self._par[id(el)]
            t = p_text(el)
            return (3 <= len(t) <= 80 and not is_special_p(el) and not is_bullet(par)
                    and not t.endswith((".", ",", ";"))
                    and (is_boldline(el) or (t.isupper() and any(c.isalpha() for c in t) and len(t) <= 60)))

        front = Section(0, "Opening / cover page", None)
        current = front
        sections = [front]
        n = 0
        for el in elems:
            if is_top(el):
                n += 1
                current = Section(n, p_text(el), el)
                sections.append(current)
            else:
                current.body.append(el)

        has_front = any((e.tag == W_P and (p_text(e) or is_special_p(e))) or e.tag != W_P for e in front.body)
        self.sections = sections if has_front else sections[1:]

        # Global fallback prototypes (first paragraph of each kind anywhere in the document)
        self.protos = {}
        for el, par in paras:
            if is_special_p(el) or not p_text(el):
                continue
            k = self.classify(el)
            self.protos.setdefault(k, el)

    def classify(self, el) -> str:
        par = self._par.get(id(el)) or Paragraph(el, self.doc)
        lvl = self._levels_map.get(id(el))
        if lvl is not None and (self.top is None or lvl > self.top):
            return "sub"
        if is_bullet(par):
            return "bullet"
        t = p_text(el)
        if is_boldline(el) and len(t) <= 90 and not t.endswith((".", ",", ";")):
            return "sub"
        return "body"

    # -- metadata for the UI
    def metas(self):
        out = []
        for s in self.sections:
            texts = s.texts()
            joined = " ".join(texts)
            title = s.title
            if s.heading is None:
                suggest_fixed = False
            else:
                suggest_fixed = bool(FIXED_HINTS.search(title)) and not VARIABLE_HINTS.search(title)
            out.append({
                "id": s.id,
                "title": title,
                "mode": s.mode,
                "words": len(joined.split()),
                "preview": (joined[:200] + "…") if len(joined) > 200 else joined,
                "has_table": any(e.tag == W_TBL for e in s.body),
                "has_image": any(has_image(e) for e in s.body),
                "suggest_fixed": suggest_fixed,
            })
        return out

    def section(self, sid):
        for s in self.sections:
            if s.id == sid:
                return s
        raise DocError("Unknown section.")

    # -- text <-> document
    def markup(self, sec: Section) -> str:
        if sec.mode == "aligned":
            return "\n".join(sec.texts())
        lines = []
        for el in sec.body:
            if el.tag != W_P or is_special_p(el):
                continue
            t = p_markup(el)
            if not t:
                continue
            kind = self.classify(el)
            if kind == "sub":
                lines.append("## " + t.replace("**", ""))
            elif kind == "bullet":
                lines.append("- " + t)
            else:
                lines.append(t)
        return "\n".join(lines)

    def _proto(self, sec: Section, kind: str):
        for el in sec.body:
            if el.tag == W_P and not is_special_p(el) and p_text(el) and self.classify(el) == kind:
                return el
        return self.protos.get(kind)

    def apply(self, sec: Section, text: str):
        lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
        if sec.mode == "aligned":
            self._apply_aligned(sec, lines)
        else:
            self._apply_structured(sec, lines)

    def _apply_aligned(self, sec, lines):
        targets = [e for e in sec.body if e.tag == W_P and not is_special_p(e) and p_text(e)]
        last = None
        for i, el in enumerate(targets):
            if i < len(lines):
                _set_text(el, el, strip_marker(lines[i]), inline=False)
                last = el
            else:
                el.getparent().remove(el)
        if last is None:
            return
        for extra in lines[len(targets):]:
            new = _clone_with_text(last, strip_marker(extra), inline=False)
            last.addnext(new)
            last = new

    def _apply_structured(self, sec, lines):
        if sec.heading is None:
            return
        items = []
        for ln in lines:
            m = re.match(r"^#{1,4}\s+(.*)$", ln)
            if m:
                items.append(("sub", m.group(1).strip()))
                continue
            m = re.match(r"^(?:[-*•]\s+)(.*)$", ln.strip())
            if m:
                items.append(("bullet", m.group(1).strip()))
                continue
            items.append(("body", ln.strip()))

        protos = {k: self._proto(sec, k) for k in ("sub", "bullet", "body")}
        if protos["body"] is None:
            raise DocError("The sample has no ordinary paragraph to copy the formatting from.")

        # Remove the old text paragraphs; keep tables, images, fields, page/section breaks.
        for el in list(sec.body):
            if el.tag == W_P and not is_special_p(el):
                el.getparent().remove(el)

        anchor = sec.heading
        for kind, txt in items:
            if not txt:
                continue
            if kind == "sub":
                if protos["sub"] is not None:
                    new = _clone_with_text(protos["sub"], txt.replace("**", ""), inline=False, keep_bold=True)
                else:
                    new = _clone_with_text(protos["body"], f"**{txt}**", inline=True)
            elif kind == "bullet":
                if protos["bullet"] is not None:
                    new = _clone_with_text(protos["bullet"], txt, inline=True)
                else:
                    new = _clone_with_text(protos["body"], "• " + txt, inline=True)
            else:
                new = _clone_with_text(protos["body"], txt, inline=True)
            anchor.addnext(new)
            anchor = new


def strip_marker(line: str) -> str:
    line = re.sub(r"^#{1,4}\s+", "", line.strip())
    line = re.sub(r"^[-*•]\s+", "", line)
    return line.replace("**", "")


# --------------------------------------------------------------------------- paragraph building

def _dominant_rpr(p, drop_bold: bool):
    """rPr of the longest run in a paragraph (so a bold lead-in doesn't win)."""
    best, best_len = None, -1
    for r in p.iter(W_R):
        t = run_text(r)
        if len(t.strip()) > best_len:
            best, best_len = r, len(t.strip())
    if best is None:
        return None
    rpr = best.find(W_RPR)
    if rpr is None:
        return None
    rpr = copy.deepcopy(rpr)
    if drop_bold:
        for tag in (W_B, W_BCS):
            for b in rpr.findall(tag):
                rpr.remove(b)
    return rpr


def _set_bold(rpr, bold: bool):
    for tag in (W_B, W_BCS):
        for b in rpr.findall(tag):
            rpr.remove(b)
    if not bold:
        return
    b = OxmlElement("w:b")
    idx = 0
    for i, ch in enumerate(rpr):
        if ch.tag in (qn("w:rStyle"), qn("w:rFonts")):
            idx = i + 1
    rpr.insert(idx, b)


def _make_run(rpr, text, bold):
    r = OxmlElement("w:r")
    rp = copy.deepcopy(rpr) if rpr is not None else OxmlElement("w:rPr")
    if bold:
        _set_bold(rp, True)
    if len(rp):
        r.append(rp)
    t = OxmlElement("w:t")
    t.text = text
    t.set(XML_SPACE, "preserve")
    r.append(t)
    return r


def _fill(new, proto, text, inline, keep_bold):
    ppr = new.find(W_PPR)
    if ppr is not None:
        for s in ppr.findall(W_SECTPR):
            ppr.remove(s)
    for ch in list(new):
        if ch.tag != W_PPR:
            new.remove(ch)
    whole_bold = is_boldline(proto)
    rpr = _dominant_rpr(proto, drop_bold=(not keep_bold and not whole_bold))
    segs = parse_inline(text) if inline else [(text, False)]
    for seg, bold in segs:
        if seg:
            new.append(_make_run(rpr, seg, bold))


def _clone_with_text(proto, text, inline=True, keep_bold=False):
    new = copy.deepcopy(proto)
    _fill(new, proto, text, inline, keep_bold)
    return new


def _set_text(el, proto, text, inline=True):
    snapshot = copy.deepcopy(proto)
    _fill(el, snapshot, text, inline, keep_bold=True)


# --------------------------------------------------------------------------- public API

def load(data: bytes) -> Parsed:
    try:
        doc = Document(io.BytesIO(data))
    except Exception:
        raise DocError("That Word document couldn't be opened. Try re-saving it as .docx from Word.")
    return Parsed(doc)


def header_footer_notes(doc) -> list:
    snippets = []
    try:
        for s in doc.sections:
            for part in (s.header, s.footer, s.first_page_header, s.first_page_footer):
                try:
                    if part.is_linked_to_previous:
                        continue
                    txt = " ".join(p.text.strip() for p in part.paragraphs if p.text.strip())
                except Exception:
                    continue
                if txt and txt not in snippets:
                    snippets.append(txt)
    except Exception:
        pass
    if not snippets:
        return []
    shown = "; ".join(f"“{t[:50]}”" for t in snippets[:3])
    return [f"Headers/footers are copied exactly as in your sample ({shown}). Check them for customer-specific text."]


def analyze(data: bytes):
    parsed = load(data)
    metas = parsed.metas()
    notes = header_footer_notes(parsed.doc)
    if len(metas) <= 1 and not any(m["id"] != "s0" for m in metas):
        notes.append("No section headings were found. Use Word's Heading styles (or bold headings) in the sample "
                     "so it can be split into sections.")
    if any(m["has_table"] for m in metas):
        notes.append("Tables are never rewritten. If a section you want rewritten contains a table, "
                     "it stays as in the sample, so edit it in Word afterwards.")
    return parsed, metas, notes


def section_markup(data: bytes, sid: str):
    parsed = load(data)
    sec = parsed.section(sid)
    return sec.title, sec.mode, parsed.markup(sec)


def build(data: bytes, drafts: dict) -> bytes:
    """Return a new .docx where each section in `drafts` has been rewritten."""
    parsed = load(data)
    for sec in parsed.sections:
        if sec.id in drafts:
            parsed.apply(sec, drafts[sec.id])
    doc = parsed.doc
    cp = doc.core_properties
    for attr in ("title", "subject", "keywords", "comments"):
        try:
            setattr(cp, attr, "")
        except Exception:
            pass
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
