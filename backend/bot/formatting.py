import re
from html import escape


INLINE = re.compile(
    r"`([^`\n]+)`|\[([^\]\n]+)\]\((https?://[^\s<>]+?)\)|"
    r"\*\*(.+?)\*\*|__(.+?)__|\*([^*\n]+)\*|(?<!\w)_([^_\n]+)_(?!\w)|~~(.+?)~~"
)
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
DIVIDER = re.compile(r":?-{3,}:?")
LIST_MARKER = re.compile(r"^(\s*)[-*+]\s+")


def render_inline(line):
    pieces = []
    cursor = 0
    for match in INLINE.finditer(line):
        pieces.append(escape(line[cursor:match.start()]))
        code, label, url, bold, strong, italic, emphasis, strike = match.groups()
        if code:
            pieces.append(f"<code>{escape(code)}</code>")
        elif label:
            pieces.append(f'<a href="{escape(url, quote=True)}">{render_inline(label)}</a>')
        else:
            tag = "b" if bold or strong else "s" if strike else "i"
            pieces.append(f"<{tag}>{render_inline(bold or strong or italic or emphasis or strike)}</{tag}>")
        cursor = match.end()
    pieces.append(escape(line[cursor:]))
    return "".join(pieces)


def telegram_html(markdown: str):
    """Yield complete HTML messages with bounded rendered length."""
    lines = markdown.splitlines()
    chunks = []
    size = 0
    in_code = False
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        heading = HEADING.fullmatch(line) if not in_code else None
        if heading:
            line = heading[1]
        if not in_code:
            if re.fullmatch(r"\s*(?:-{3,}|\*{3,}|_{3,})\s*", line):
                line = "────────"
            line = LIST_MARKER.sub(r"\1• ", line)
        quoted = not in_code and line.startswith("> ")
        if quoted:
            line = line[2:]
        if not in_code and line.count("|") >= 2:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(DIVIDER.fullmatch(cell) for cell in cells):
                continue
            line = "• " + " — ".join(cells)
        for start in range(0, max(1, len(line)), 1500):
            part = line[start:start + 1500]
            if in_code:
                rendered = f"<code>{escape(part)}</code>" if part else ""
            else:
                rendered = render_inline(part)
                if heading and rendered:
                    rendered = f"<b>{rendered}</b>"
                if quoted and rendered:
                    rendered = f"<blockquote>{rendered}</blockquote>"
            length = len(part.encode("utf-16-le")) // 2 + 4
            if chunks and size + length > 3500:
                yield "\n".join(chunks)
                chunks, size = [], 0
            chunks.append(rendered)
            size += length
    if chunks and any(part.strip() for part in chunks):
        yield "\n".join(chunks)
