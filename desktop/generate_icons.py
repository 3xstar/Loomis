from pathlib import Path

from PIL import Image, ImageDraw


UI = Path(__file__).with_name("ui")
ICON_SIZES = (16, 32, 48, 64, 128, 256)


def load_mark(filename: str, color: str) -> Image.Image:
    with Image.open(UI / filename) as source:
        alpha = source.convert("RGBA").getchannel("A")
        alpha = alpha.crop(alpha.getbbox())
    mark = Image.new("RGBA", alpha.size, color)
    mark.putalpha(alpha)
    return mark


def center(canvas: Image.Image, mark: Image.Image, size: int) -> None:
    mark = mark.copy()
    mark.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas.alpha_composite(mark, ((canvas.width - mark.width) // 2, (canvas.height - mark.height) // 2))


def squircle(draw: ImageDraw.ImageDraw, radius: int, color: str) -> None:
    for y in range(512 - radius, 512 + radius):
        half_width = int(radius * (1 - (abs(y + 0.5 - 512) / radius) ** 4) ** 0.25)
        draw.line((512 - half_width, y, 511 + half_width, y), fill=color)


def main() -> None:
    light = load_mark("loomis-color-2048.png", "#09090b")
    dark = load_mark("loomis-inverse-2048.png", "#ffffff")
    for theme, mark in (("light", light), ("dark", dark)):
        canvas = Image.new("RGBA", (128, 128))
        center(canvas, mark, 128)
        canvas.save(UI / f"logo-{theme}.png")

    badge = Image.new("RGBA", (1024, 1024))
    draw = ImageDraw.Draw(badge)
    squircle(draw, 478, "#ffffff")
    squircle(draw, 465, "#09090b")
    center(badge, dark, 640)
    badge = badge.resize((256, 256), Image.Resampling.LANCZOS)
    badge.save(UI / "favicon.png")
    badge.save(UI / "logo.ico", sizes=[(size, size) for size in ICON_SIZES])


if __name__ == "__main__":
    main()
