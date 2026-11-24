import re
from html import escape
from unicodedata import combining, east_asian_width


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


def table_cells(line):
    return [cell.strip().replace(r"\|", "|")
            for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def display_width(value):
    return sum(2 if east_asian_width(char) in {"W", "F"} else 1
               for char in value if not combining(char))


def table_lines(rows, alignment):
    widths = [max(display_width(cell) for cell in column) for column in zip(*rows)]

    def cell_text(cell, width, column):
        padding = width - display_width(cell)
        left = padding if column.endswith(":") else 0
        if column.startswith(":") and column.endswith(":"):
            left = padding // 2
        return " " * left + cell + " " * (padding - left)

    for index, row in enumerate(rows):
        if index == 1:
            yield "-+-".join("-" * width for width in widths)
        yield " | ".join(cell_text(cell, width, column)
                         for cell, width, column in zip(row, widths, alignment))


def telegram_html(markdown: str):
    """Yield complete HTML messages with bounded rendered length."""
    lines = markdown.splitlines()
    chunks = []
    size = 0
    in_code = False
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not in_code and "|" in line and index < len(lines):
            header, alignment = table_cells(line), table_cells(lines[index])
            if len(header) > 1 and len(header) == len(alignment) and all(map(DIVIDER.fullmatch, alignment)):
                if chunks and any(part.strip() for part in chunks):
                    yield "\n".join(chunks)
                chunks, size = [], 0
                rows = [header]
                index += 1
                while index < len(lines) and "|" in lines[index]:
                    cells = table_cells(lines[index])
                    if len(cells) != len(header):
                        break
                    rows.append(cells)
                    index += 1
                table = []
                length = 0
                for row in table_lines(rows, alignment):
                    for start in range(0, max(1, len(row)), 1500):
                        part = row[start:start + 1500]
                        units = len(part.encode("utf-16-le")) // 2 + 1
                        if table and length + units > 3500:
                            yield "<pre>" + escape("\n".join(table)) + "</pre>"
                            table, length = [], 0
                        table.append(part)
                        length += units
                if table:
                    yield "<pre>" + escape("\n".join(table)) + "</pre>"
                continue
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
