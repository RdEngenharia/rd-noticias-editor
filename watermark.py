import sys
import os
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, VideoFileClip

AUDIO_FILE = "monume-breaking-news-547918.mp3"


def _build_watermark(text, font_path, font_size, pad_x=16, pad_y=10):
    """Desenha texto branco sobre fundo azul e retorna array uint8 RGB."""
    try:
        font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default(size=font_size)
    except Exception:
        font = ImageFont.load_default()

    probe = Image.new("RGB", (1, 1))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    img = Image.new("RGB", (text_w + 2 * pad_x, text_h + 2 * pad_y), (0, 90, 180))
    ImageDraw.Draw(img).text((pad_x - bbox[0], pad_y - bbox[1]), text, font=font, fill=(255, 255, 255))

    return np.array(img)  # dtype uint8, shape (h, w, 3)


def add_watermark(input_path, output_path, text="RD Noticias", margin=30):
    if not os.path.exists(input_path):
        print(f"Erro: arquivo '{input_path}' nao encontrado.")
        sys.exit(1)

    font_path = next(
        (p for p in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf") if os.path.exists(p)),
        None,
    )

    video = VideoFileClip(input_path)
    duration = video.duration
    font_size = max(24, int(video.h * 0.05))

    # Watermark: PIL gera o array uint8 correto; ImageClip usa ele diretamente
    wm_arr = _build_watermark(text, font_path, font_size)
    wm_h, wm_w = wm_arr.shape[:2]
    wm_clip = (
        ImageClip(wm_arr)
        .with_position((video.w - wm_w - margin, video.h - wm_h - margin))
        .with_duration(duration)
    )

    # Audio: descarta o original e carrega o MP3
    audio_path = AUDIO_FILE
    if not os.path.exists(audio_path):
        audio_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), AUDIO_FILE)

    if os.path.exists(audio_path):
        print(f"Audio encontrado: {audio_path}")
        new_audio = AudioFileClip(audio_path)
        if new_audio.duration > duration:
            new_audio = new_audio.with_duration(duration)
    else:
        print(f"AVISO: '{AUDIO_FILE}' nao encontrado. Exportando sem audio.")
        print(f"  Diretorio atual:  {os.getcwd()}")
        print(f"  Pasta do script:  {os.path.dirname(os.path.abspath(__file__))}")
        new_audio = None

    result = CompositeVideoClip([video.without_audio(), wm_clip])

    if new_audio is not None:
        result = result.with_audio(new_audio)
        result.write_videofile(output_path, codec="libx264", audio_codec="aac")
    else:
        result.write_videofile(output_path, codec="libx264", audio=False)

    for clip in (wm_clip, video, result):
        clip.close()
    if new_audio is not None:
        new_audio.close()

    print(f"\nConcluido! Video salvo em: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso:     python watermark.py <video_entrada> <video_saida>")
        print("Exemplo: python watermark.py video.mp4 video_com_marca.mp4")
        sys.exit(1)

    add_watermark(sys.argv[1], sys.argv[2])
