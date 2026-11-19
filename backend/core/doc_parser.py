import asyncio
from pathlib import Path


def _parse_document(path: Path) -> str:
    match path.suffix.lower():
        case ".pdf":
            from pypdf import PdfReader

            with path.open("rb") as stream:
                return "\n\n".join(page.extract_text() or "" for page in PdfReader(stream).pages)
        case ".docx":
            from docx import Document
            from docx.table import Table

            chunks = []
            for block in Document(path).iter_inner_content():
                if isinstance(block, Table):
                    chunks.extend("\t".join(cell.text for cell in row.cells) for row in block.rows)
                else:
                    chunks.append(block.text)
            return "\n".join(chunks)
        case ".txt" | ".md":
            data = path.read_bytes()
            if data.startswith((b"\xff\xfe", b"\xfe\xff")):
                return data.decode("utf-16")
            try:
                return data.decode("utf-8-sig")
            except UnicodeDecodeError:
                return data.decode("cp1251")
        case _:
            raise ValueError("Поддерживаются документы .pdf, .docx, .txt и .md.")


async def parse_document(path: str | Path) -> str:
    return (await asyncio.to_thread(_parse_document, Path(path))).strip()
