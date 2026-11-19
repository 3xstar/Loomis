import re
from html import escape


INLINE = re.compile(r"`([^`\n]+)`|\*\*(.+?)\*\*|__(.+?)__|\*([^*\n]+)\*")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$")
DIVIDER = re.compile(r":?-{3,}:?")


def telegram_html(markdown: str):
    """Yield complete HTML messages with bounded rendered length."""
    lines = markdown.splitlines()
    chunks = []
    size = 0
    in_code = False
    for line in lines:
        if line.lstrip().startswith("```"):
            in_code = not in_code
            continue
        heading = HEADING.fullmatch(line) if not in_code else None
        if heading:
            line = heading[1]
        if not in_code and line.count("|") >= 2:
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if all(DIVIDER.fullmatch(cell) for cell in cells):
                continue
            line = "• " + " — ".join(cells)
        for start in range(0, max(1, len(line)), 1800):
            part = line[start:start + 1800]
            if in_code:
                rendered = f"<code>{escape(part)}</code>" if part else ""
            else:
                pieces = []
                cursor = 0
                for match in INLINE.finditer(part):
                    pieces.append(escape(part[cursor:match.start()].replace("**", "")))
                    body = next(group for group in match.groups() if group is not None)
                    tag = "code" if match[1] is not None else "b" if match[2] or match[3] else "i"
                    pieces.append(escape(body) if heading and tag == "b" else f"<{tag}>{escape(body)}</{tag}>")
                    cursor = match.end()
                pieces.append(escape(part[cursor:].replace("**", "")))
                rendered = "".join(pieces)
                if heading:
                    rendered = f"\n<b> {rendered}</b>"
            length = len(part.encode("utf-16-le")) // 2 + 4
            if chunks and size + length > 3900:
                yield "\n".join(chunks)
                chunks, size = [], 0
            chunks.append(rendered)
            size += length
    if chunks and any(part.strip() for part in chunks):
        yield "\n".join(chunks)
