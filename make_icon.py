"""Gera icon.ico para o EXE do RD Noticias Editor."""
from PIL import Image, ImageDraw, ImageFont
import os, sys

def make_icon(output_path):
    sizes = [256, 128, 64, 48, 32, 16]
    frames = []

    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        r = size // 6
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=r,
                                fill=(31, 111, 235, 255))

        font_size = int(size * 0.46)
        font = None
        for candidate in [
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        ]:
            if os.path.exists(candidate):
                font = ImageFont.truetype(candidate, font_size)
                break

        text = "RD"
        if font:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            x = (size - tw) // 2 - bbox[0]
            y = (size - th) // 2 - bbox[1]
            draw.text((x, y), text, fill="white", font=font)
        else:
            draw.text((size // 4, size // 4), text, fill="white")

        frames.append(img)

    frames[0].save(
        output_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=frames[1:],
    )
    print(f"Ícone criado: {output_path}")

if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    make_icon(out)
