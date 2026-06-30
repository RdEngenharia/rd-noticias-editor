"""watermark_gui.py — RD Noticias Video Watermark Editor"""

import os
import sys
import queue
import threading
import datetime
from collections import deque


def _setup_ffmpeg():
    """Use imageio-ffmpeg bundled binary when available (dev + PyInstaller exe)."""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            os.environ.setdefault("FFMPEG_BINARY", exe)
    except Exception:
        pass

_setup_ffmpeg()


def _create_desktop_shortcut():
    """Cria atalho na área de trabalho na primeira execução do exe."""
    if not getattr(sys, "frozen", False):
        return
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    lnk = os.path.join(desktop, "RD Noticias Editor.lnk")
    if os.path.exists(lnk):
        return
    exe = sys.executable
    work = os.path.dirname(exe)
    ps = (
        f'$s=(New-Object -COM WScript.Shell).CreateShortcut("{lnk}");'
        f'$s.TargetPath="{exe}";'
        f'$s.WorkingDirectory="{work}";'
        f'$s.IconLocation="{exe}";'
        f'$s.Description="RD Noticias — Editor de Video";'
        f'$s.Save()'
    )
    import subprocess
    subprocess.run(
        ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
        creationflags=0x08000000,
    )

_create_desktop_shortcut()

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# ── Optional audio deps (checked at runtime) ─────────────────────────────────
try:
    import sounddevice as _sd
    _HAS_SD = True
except Exception:
    _HAS_SD = False

try:
    import noisereduce as _nr
    _HAS_NR = True
except Exception:
    _HAS_NR = False

try:
    from scipy import signal as _sig
    from scipy.io import wavfile as _wav
    from scipy.ndimage import uniform_filter1d as _uf1
    _HAS_SCIPY = True
except Exception:
    _HAS_SCIPY = False

try:
    import librosa as _librosa
    _HAS_LIBROSA = True
except Exception:
    _HAS_LIBROSA = False

SAMPLERATE = 44100

# ── Recorder ──────────────────────────────────────────────────────────────────

class Recorder:
    def __init__(self):
        self._frames: list = []
        self._stream = None
        self._lock = threading.Lock()
        self.active = False

    def start(self):
        if not _HAS_SD:
            raise RuntimeError("sounddevice não instalado.\nRode: pip install sounddevice")
        self._frames = []
        self.active = True
        self._stream = _sd.InputStream(
            samplerate=SAMPLERATE, channels=1, dtype="float32",
            callback=self._cb,
        )
        self._stream.start()

    def _cb(self, indata, frames, time, status):
        with self._lock:
            self._frames.append(indata.copy())

    def stop_and_save(self, path: str) -> bool:
        self.active = False
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        with self._lock:
            frames = list(self._frames)
        if not frames:
            return False
        data = np.concatenate(frames).flatten()
        data_i16 = np.clip(data * 32767, -32768, 32767).astype(np.int16)
        _wav.write(path, SAMPLERATE, data_i16)
        return True


# ── Audio processing (scipy/numpy — Python 3.14 safe, sem pydub/audioop) ─────

def _enhance_voice(input_path: str, output_path: str, log_fn=None):
    """Noise reduction + voice EQ + dynamic compression."""
    if not _HAS_SCIPY:
        raise RuntimeError("scipy não instalado.")
    if not _HAS_NR:
        raise RuntimeError("noisereduce não instalado.")

    def _log(m):
        if log_fn:
            log_fn(m)

    _log("  → Carregando áudio…")
    rate, data = _wav.read(input_path)

    if data.dtype == np.int16:
        data_f = data.astype(np.float32) / 32767.0
    elif data.dtype == np.int32:
        data_f = data.astype(np.float32) / 2**31
    else:
        data_f = data.astype(np.float32)

    if data_f.ndim > 1:
        data_f = data_f.mean(axis=1)

    # 1. Redução de ruído de fundo
    _log("  → Redução de ruído…")
    data_f = _nr.reduce_noise(y=data_f, sr=rate, stationary=False, prop_decrease=0.80)

    # 2. Filtro passa-alta 80 Hz (remove rumble e vibração de fundo)
    nyq = rate / 2.0
    b, a = _sig.butter(4, 80.0 / nyq, btype="high")
    data_f = _sig.filtfilt(b, a, data_f)

    # 3. Equalização: realce de presença de voz 1.5 kHz–5 kHz (~+3 dB)
    _log("  → Equalização de voz (1.5–5 kHz)…")
    b2, a2 = _sig.butter(2, [1500.0 / nyq, 5000.0 / nyq], btype="band")
    presence = _sig.filtfilt(b2, a2, data_f)
    data_f = data_f + 0.4 * presence

    # 4. Compressão de dinâmica (soft-knee vetorizado, ratio 4:1)
    _log("  → Compressão de dinâmica…")
    env = _uf1(np.abs(data_f), size=int(rate * 0.025)) + 1e-8
    threshold = np.percentile(env, 65)
    gain = np.where(env > threshold, (threshold / env) ** (1.0 - 1.0 / 4.0), 1.0)
    data_f = data_f * gain * 1.6  # makeup gain compensatório

    # 5. Normalizar para -3 dBFS
    peak = np.max(np.abs(data_f))
    if peak > 0:
        data_f = data_f * (10 ** (-3.0 / 20.0)) / peak

    out = np.clip(data_f * 32767, -32768, 32767).astype(np.int16)
    _wav.write(output_path, rate, out)
    _log(f"  → Melhoria aplicada: {os.path.basename(output_path)}")


def _apply_pitch_shift(input_path: str, output_path: str, n_steps: float, log_fn=None):
    """Pitch shift sem alterar a velocidade da fala (phase vocoder do librosa)."""
    def _log(m):
        if log_fn:
            log_fn(m)

    _log(f"  → Carregando áudio para pitch shift…")
    rate, data = _wav.read(input_path)

    if data.dtype == np.int16:
        data_f = data.astype(np.float32) / 32767.0
    elif data.dtype == np.int32:
        data_f = data.astype(np.float32) / 2**31
    else:
        data_f = data.astype(np.float32)

    if data_f.ndim > 1:
        data_f = data_f.mean(axis=1)

    _log(f"  → Pitch shift ({n_steps:+.0f} semitons) — aguarde…")
    data_shifted = _librosa.effects.pitch_shift(
        data_f, sr=rate, n_steps=n_steps, res_type="kaiser_fast"
    )

    peak = np.max(np.abs(data_shifted))
    if peak > 0:
        data_shifted = data_shifted * (10 ** (-3.0 / 20.0)) / peak

    out = np.clip(data_shifted * 32767, -32768, 32767).astype(np.int16)
    _wav.write(output_path, rate, out)
    _log(f"  → Voz de Narrador aplicada: {os.path.basename(output_path)}")


def _mix_audio(narration_path: str, jingle_path: str, output_path: str,
               jingle_db: float = -12.0, log_fn=None):
    """Mix narração + vinheta (vinheta em volume reduzido)."""
    from moviepy import AudioFileClip

    def _log(m):
        if log_fn:
            log_fn(m)

    rate, narr = _wav.read(narration_path)
    narr_f = narr.astype(np.float32) / 32767.0
    if narr_f.ndim > 1:
        narr_f = narr_f.mean(axis=1)

    _log("  → Carregando vinheta para mixagem…")
    ac = AudioFileClip(jingle_path)
    jingle_arr = ac.to_soundarray(fps=rate).astype(np.float32)
    ac.close()
    if jingle_arr.ndim > 1:
        jingle_arr = jingle_arr.mean(axis=1)

    jingle_arr *= 10 ** (jingle_db / 20.0)

    n = max(len(narr_f), len(jingle_arr))
    narr_f      = np.pad(narr_f,      (0, n - len(narr_f)))
    jingle_arr  = np.pad(jingle_arr,  (0, n - len(jingle_arr)))

    _log(f"  → Mixando (vinheta a {jingle_db} dB)…")
    mixed = narr_f + jingle_arr
    peak = np.max(np.abs(mixed))
    if peak > 0.95:
        mixed = mixed / peak * 0.95

    out = np.clip(mixed * 32767, -32768, 32767).astype(np.int16)
    _wav.write(output_path, rate, out)


# ── Image / watermark utils ───────────────────────────────────────────────────

def _flood_fill_transparent(img: Image.Image, threshold: int = 230) -> Image.Image:
    img = img.convert("RGBA")
    data = np.array(img, dtype=np.uint8)
    h, w = data.shape[:2]
    visited = np.zeros((h, w), dtype=bool)
    q: deque = deque()

    for r, c in [(0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)]:
        px = data[r, c]
        if px[0] > threshold and px[1] > threshold and px[2] > threshold and not visited[r, c]:
            q.append((r, c))
            visited[r, c] = True

    while q:
        r, c = q.popleft()
        data[r, c, 3] = 0
        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
            rr, cc = r + dr, c + dc
            if 0 <= rr < h and 0 <= cc < w and not visited[rr, cc]:
                px = data[rr, cc]
                if px[0] > threshold and px[1] > threshold and px[2] > threshold:
                    visited[rr, cc] = True
                    q.append((rr, cc))

    return Image.fromarray(data)


def _prepare_logo(logo_path: str, target_w: int) -> np.ndarray:
    img = Image.open(logo_path).convert("RGBA")
    img = _flood_fill_transparent(img)
    ow, oh = img.size
    img = img.resize((target_w, max(1, int(oh * target_w / ow))), Image.LANCZOS)
    return np.array(img)


def _wm_pos(key: str, vw, vh, ww, wh, margin):
    return {
        "Inferior Direito":  (vw - ww - margin, vh - wh - margin),
        "Inferior Esquerdo": (margin,            vh - wh - margin),
        "Superior Direito":  (vw - ww - margin, margin),
        "Superior Esquerdo": (margin,            margin),
    }[key]


# ── Worker thread ─────────────────────────────────────────────────────────────

def _run_processing(params: dict, log_q: queue.Queue):
    try:
        from moviepy import AudioFileClip, CompositeVideoClip, ImageClip, VideoFileClip

        def log(m): log_q.put(("log", m))

        log("Carregando vídeo…")
        video = VideoFileClip(params["input"])
        duration = video.duration

        log("Preparando marca d'água…")
        wm_arr = _prepare_logo(params["logo"], max(32, int(video.w * params["logo_pct"] / 100)))
        wh, ww = wm_arr.shape[:2]
        x, y = _wm_pos(params["position"], video.w, video.h, ww, wh, params["margin"])
        wm_clip = ImageClip(wm_arr).with_position((x, y)).with_duration(duration)

        # ── Determine final audio path ────────────────────────────────
        narration  = params.get("narration", "")
        jingle     = params.get("audio", "")
        enhance    = params.get("enhance", False)
        audio_mode = params.get("audio_mode", "replace")
        final_audio = None

        if narration and os.path.exists(narration):
            processed = narration
            base = os.path.splitext(narration)[0]

            if enhance:
                log("Aplicando melhoria de voz profissional…")
                enhanced = base + "_enhanced.wav"
                _enhance_voice(narration, enhanced, log_fn=log)
                processed = enhanced

            if params.get("narrator", False):
                log("Aplicando Voz de Narrador (pitch shift)…")
                pitched = base + "_pitched.wav"
                _apply_pitch_shift(processed, pitched,
                                   float(params.get("pitch_steps", -3)),
                                   log_fn=log)
                processed = pitched

            if audio_mode == "mix" and jingle and os.path.exists(jingle):
                log("Mixando narração com vinheta…")
                mixed = base + "_mixed.wav"
                _mix_audio(processed, jingle, mixed, jingle_db=-12.0, log_fn=log)
                final_audio = mixed
            else:
                final_audio = processed

        elif jingle and os.path.exists(jingle):
            log(f"Usando vinheta: {os.path.basename(jingle)}")
            final_audio = jingle
        else:
            log("Nenhum áudio selecionado — exportando sem trilha sonora.")

        # ── Composite & export ────────────────────────────────────────
        log("Compondo vídeo…")
        result = CompositeVideoClip([video.without_audio(), wm_clip])

        if final_audio:
            na = AudioFileClip(final_audio)
            if na.duration > duration:
                na = na.with_duration(duration)
            result = result.with_audio(na)
            result.write_videofile(params["output"], codec="libx264", audio_codec="aac", logger=None)
            na.close()
        else:
            result.write_videofile(params["output"], codec="libx264", audio=False, logger=None)

        for c in (wm_clip, video, result):
            c.close()

        log_q.put(("done", f"Concluído!\nSalvo em: {params['output']}"))

    except Exception:
        import traceback
        log_q.put(("error", traceback.format_exc()))


# ── GUI constants ─────────────────────────────────────────────────────────────

BG        = "#0d1117"
PANEL     = "#161b22"
ACCENT    = "#1f6feb"
TEXT      = "#e6edf3"
MUTED     = "#8b949e"
SUCCESS   = "#3fb950"
ERROR     = "#f85149"
WARNING   = "#e3b341"
REC_RED   = "#f85149"
FONT_UI   = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
FONT_MONO = ("Consolas", 9)


# ── Reusable file-picker row ──────────────────────────────────────────────────

class FileRow(tk.Frame):
    def __init__(self, parent, label, var, filetypes=None, save=False, **kw):
        super().__init__(parent, bg=PANEL, **kw)
        self._var = var
        self._filetypes = filetypes or [("Todos", "*.*")]
        self._save = save

        tk.Label(self, text=label, bg=PANEL, fg=MUTED, font=FONT_UI,
                 width=18, anchor="w").pack(side="left", padx=(0, 6))
        tk.Entry(self, textvariable=var, bg=BG, fg=TEXT, insertbackground=TEXT,
                 relief="flat", font=FONT_UI, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground="#30363d",
                 ).pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(self, text="…", command=self._browse, bg=ACCENT, fg="white",
                  relief="flat", font=FONT_BOLD, cursor="hand2", padx=8,
                  ).pack(side="left", padx=(6, 0))

    def _browse(self):
        if self._save:
            p = filedialog.asksaveasfilename(defaultextension=".mp4",
                                             filetypes=[("Vídeo MP4", "*.mp4")])
        else:
            p = filedialog.askopenfilename(filetypes=self._filetypes)
        if p:
            self._var.set(p)


# ── Main App ──────────────────────────────────────────────────────────────────

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RD Noticias — Editor de Vídeo")
        self.configure(bg=BG)
        self.resizable(True, False)
        self.minsize(680, 0)

        # File vars
        self._var_input    = tk.StringVar()
        self._var_output   = tk.StringVar()
        self._var_logo     = tk.StringVar()
        self._var_audio    = tk.StringVar()

        # Config vars
        self._var_size     = tk.IntVar(value=18)
        self._var_position = tk.StringVar(value="Inferior Direito")

        # Recording vars
        self._var_narration  = tk.StringVar()
        self._var_enhance    = tk.BooleanVar(value=True)
        self._var_narrator   = tk.BooleanVar(value=False)
        self._var_pitch_st   = tk.StringVar(value="-3  (narrador)")
        self._var_audio_mode = tk.StringVar(value="replace")

        # State
        self._log_queue: queue.Queue = queue.Queue()
        self._processing = False
        self._recorder   = Recorder()
        self._blink_id   = None
        self._blink_on   = False

        # Auto-fill default jingle
        default_jingle = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "monume-breaking-news-547918.mp3")
        if os.path.exists(default_jingle):
            self._var_audio.set(default_jingle)

        self._build()

    # ── UI construction ───────────────────────────────────────────────────────

    def _build(self):
        PAD = dict(padx=16, pady=6)

        # Header
        hdr = tk.Frame(self, bg=ACCENT)
        hdr.pack(fill="x")
        tk.Label(hdr, text="RD Noticias  —  Editor de Vídeo",
                 bg=ACCENT, fg="white", font=("Segoe UI", 13, "bold"),
                 ).pack(side="left", padx=16, pady=10)

        # ── Section: Files ────────────────────────────────────────────
        sf = self._section("Arquivos")
        sf.pack(fill="x", **PAD)
        FileRow(sf, "Vídeo de entrada:", self._var_input,
                filetypes=[("Vídeo", "*.mp4 *.mov *.avi *.mkv *.webm"), ("Todos", "*.*")],
                ).pack(fill="x", padx=12, pady=(10, 4))
        FileRow(sf, "Vídeo de saída:", self._var_output, save=True,
                ).pack(fill="x", padx=12, pady=4)
        FileRow(sf, "Logo / Marca d'água:", self._var_logo,
                filetypes=[("Imagem", "*.png *.jpg *.jpeg *.webp"), ("Todos", "*.*")],
                ).pack(fill="x", padx=12, pady=4)
        FileRow(sf, "Vinheta (MP3/WAV):", self._var_audio,
                filetypes=[("Áudio", "*.mp3 *.wav *.aac"), ("Todos", "*.*")],
                ).pack(fill="x", padx=12, pady=(4, 12))

        # ── Section: Config ───────────────────────────────────────────
        sc = self._section("Configurações da Marca d'Água")
        sc.pack(fill="x", **PAD)

        row_sz = tk.Frame(sc, bg=PANEL)
        row_sz.pack(fill="x", padx=12, pady=(10, 4))
        tk.Label(row_sz, text="Tamanho da logo:", bg=PANEL, fg=MUTED,
                 font=FONT_UI, width=20, anchor="w").pack(side="left")
        self._lbl_size = tk.Label(row_sz, text="18%", bg=PANEL, fg=TEXT,
                                  font=FONT_BOLD, width=5)
        self._lbl_size.pack(side="right")
        ttk.Scale(row_sz, from_=5, to=40, variable=self._var_size,
                  orient="horizontal", command=self._on_size,
                  ).pack(side="left", fill="x", expand=True, padx=(6, 0))

        row_pos = tk.Frame(sc, bg=PANEL)
        row_pos.pack(fill="x", padx=12, pady=(4, 12))
        tk.Label(row_pos, text="Posição:", bg=PANEL, fg=MUTED,
                 font=FONT_UI, width=20, anchor="w").pack(side="left")
        for opt in ("Inferior Direito", "Inferior Esquerdo", "Superior Direito", "Superior Esquerdo"):
            tk.Radiobutton(row_pos, text=opt, variable=self._var_position, value=opt,
                           bg=PANEL, fg=TEXT, selectcolor=ACCENT,
                           activebackground=PANEL, font=FONT_UI,
                           ).pack(side="left", padx=5)

        # ── Section: Narration ────────────────────────────────────────
        sn = self._section("Gravação de Narração")
        sn.pack(fill="x", **PAD)

        # Row 1: buttons + status
        row_rec = tk.Frame(sn, bg=PANEL)
        row_rec.pack(fill="x", padx=12, pady=(10, 4))

        self._btn_start = tk.Button(
            row_rec, text="⬤  Iniciar Gravação", command=self._start_rec,
            bg="#238636", fg="white", relief="flat", font=FONT_BOLD,
            cursor="hand2", padx=10, pady=6,
        )
        self._btn_start.pack(side="left", padx=(0, 8))

        self._btn_stop = tk.Button(
            row_rec, text="■  Parar e Salvar", command=self._stop_rec,
            bg="#30363d", fg=MUTED, relief="flat", font=FONT_BOLD,
            cursor="hand2", padx=10, pady=6, state="disabled",
        )
        self._btn_stop.pack(side="left")

        self._lbl_rec_status = tk.Label(
            row_rec, text="● Aguardando microfone", bg=PANEL, fg=MUTED, font=FONT_UI,
        )
        self._lbl_rec_status.pack(side="left", padx=14)

        # Row 2: recorded file path
        row_nf = tk.Frame(sn, bg=PANEL)
        row_nf.pack(fill="x", padx=12, pady=2)
        tk.Label(row_nf, text="Arquivo gravado:", bg=PANEL, fg=MUTED,
                 font=FONT_UI, width=20, anchor="w").pack(side="left")
        tk.Entry(row_nf, textvariable=self._var_narration, bg=BG, fg=TEXT,
                 insertbackground=TEXT, relief="flat", font=FONT_UI,
                 highlightthickness=1, highlightcolor=ACCENT,
                 highlightbackground="#30363d", state="readonly",
                 readonlybackground=BG,
                 ).pack(side="left", fill="x", expand=True, ipady=4)

        # Row 3: enhancement checkbox
        row_enh = tk.Frame(sn, bg=PANEL)
        row_enh.pack(fill="x", padx=12, pady=(8, 2))
        tk.Checkbutton(
            row_enh, text="Aplicar melhoria de voz profissional",
            variable=self._var_enhance, bg=PANEL, fg=TEXT,
            selectcolor=PANEL, activebackground=PANEL,
            font=FONT_BOLD, cursor="hand2",
        ).pack(side="left")
        tk.Label(row_enh,
                 text="  (redução de ruído · equalização · compressão de dinâmica)",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 9),
                 ).pack(side="left")

        # Row 3b: Voz de Narrador
        row_nar = tk.Frame(sn, bg=PANEL)
        row_nar.pack(fill="x", padx=12, pady=(2, 4))
        tk.Checkbutton(
            row_nar, text="Voz de Narrador  (pitch shift)",
            variable=self._var_narrator, bg=PANEL, fg=TEXT,
            selectcolor=PANEL, activebackground=PANEL,
            font=FONT_BOLD, cursor="hand2",
        ).pack(side="left")
        tk.Label(row_nar, text="     Tom:", bg=PANEL, fg=MUTED, font=FONT_UI,
                 ).pack(side="left")
        ttk.Combobox(
            row_nar, textvariable=self._var_pitch_st,
            values=["-2  (sutil)", "-3  (narrador)", "-4  (grave)"],
            state="readonly", width=17,
        ).pack(side="left", padx=(4, 0))
        tk.Label(row_nar,
                 text="   velocidade da fala preservada",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 9),
                 ).pack(side="left", padx=(8, 0))

        # Row 4: audio mode
        row_mode = tk.Frame(sn, bg=PANEL)
        row_mode.pack(fill="x", padx=12, pady=(6, 12))
        tk.Label(row_mode, text="Usar narração como:", bg=PANEL, fg=MUTED,
                 font=FONT_UI, width=20, anchor="w").pack(side="left")
        tk.Radiobutton(row_mode, text="Substituir vinheta",
                       variable=self._var_audio_mode, value="replace",
                       bg=PANEL, fg=TEXT, selectcolor=ACCENT,
                       activebackground=PANEL, font=FONT_UI,
                       ).pack(side="left", padx=(0, 16))
        tk.Radiobutton(row_mode, text="Mixar com vinheta  (vinheta a −12 dB)",
                       variable=self._var_audio_mode, value="mix",
                       bg=PANEL, fg=TEXT, selectcolor=ACCENT,
                       activebackground=PANEL, font=FONT_UI,
                       ).pack(side="left")

        # ── Section: Log ──────────────────────────────────────────────
        sl = self._section("Log")
        sl.pack(fill="both", expand=True, **PAD)

        self._txt_log = tk.Text(sl, height=7, bg=BG, fg=MUTED, font=FONT_MONO,
                                relief="flat", state="disabled", wrap="word")
        sb = ttk.Scrollbar(sl, command=self._txt_log.yview)
        self._txt_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y", padx=(0, 4), pady=6)
        self._txt_log.pack(fill="both", expand=True, padx=8, pady=6)

        # ── Footer ────────────────────────────────────────────────────
        footer = tk.Frame(self, bg=BG)
        footer.pack(fill="x", padx=16, pady=(0, 16))

        self._progress = ttk.Progressbar(footer, mode="indeterminate")
        self._progress.pack(fill="x", pady=(0, 8))

        self._btn_process = tk.Button(
            footer, text="▶  PROCESSAR VÍDEO",
            command=self._process, bg=SUCCESS, fg="white",
            font=("Segoe UI", 11, "bold"), relief="flat",
            cursor="hand2", pady=10,
        )
        self._btn_process.pack(fill="x")

    def _section(self, title: str) -> tk.LabelFrame:
        return tk.LabelFrame(self, text=f"  {title}  ", bg=PANEL, fg=MUTED,
                             font=FONT_UI, bd=1, relief="groove",
                             labelanchor="nw")

    # ── Recording ─────────────────────────────────────────────────────────────

    def _start_rec(self):
        if not _HAS_SD:
            messagebox.showerror("Erro",
                "sounddevice não está instalado.\nRode: pip install sounddevice")
            return
        try:
            self._recorder.start()
        except Exception as e:
            messagebox.showerror("Erro ao iniciar gravação", str(e))
            return

        self._btn_start.config(state="disabled", bg="#30363d", fg=MUTED)
        self._btn_stop.config(state="normal", bg=REC_RED, fg="white")
        self._blink_rec()

    def _blink_rec(self):
        if not self._recorder.active:
            return
        self._blink_on = not self._blink_on
        dot = "⬤" if self._blink_on else "○"
        self._lbl_rec_status.config(
            text=f"{dot} Gravando…", fg=REC_RED if self._blink_on else "#7a1f1f",
        )
        self._blink_id = self.after(600, self._blink_rec)

    def _stop_rec(self):
        if self._blink_id:
            self.after_cancel(self._blink_id)
            self._blink_id = None

        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, f"narration_{ts}.wav")

        saved = self._recorder.stop_and_save(path)

        self._btn_stop.config(state="disabled", bg="#30363d", fg=MUTED)
        self._btn_start.config(state="normal", bg="#238636", fg="white")

        if saved:
            self._var_narration.set(path)
            self._lbl_rec_status.config(text="✓ Gravação salva", fg=SUCCESS)
            self._log(f"Narração salva: {os.path.basename(path)}", SUCCESS)
        else:
            self._lbl_rec_status.config(text="⚠ Nenhum áudio capturado", fg=WARNING)

    # ── Video processing ──────────────────────────────────────────────────────

    def _process(self):
        if self._processing:
            return

        inp  = self._var_input.get().strip()
        out  = self._var_output.get().strip()
        logo = self._var_logo.get().strip()

        if not inp or not os.path.exists(inp):
            messagebox.showerror("Erro", "Selecione um vídeo de entrada válido.")
            return
        if not out:
            messagebox.showerror("Erro", "Defina o arquivo de saída.")
            return
        if not logo or not os.path.exists(logo):
            messagebox.showerror("Erro", "Selecione uma imagem de logo válida.")
            return

        narration = self._var_narration.get().strip()
        enhance   = self._var_enhance.get()
        narrator  = self._var_narrator.get()

        if enhance and narration and not _HAS_NR:
            messagebox.showwarning(
                "noisereduce não instalado",
                "A melhoria de voz requer noisereduce.\n"
                "Rode: pip install noisereduce\n\n"
                "Processando sem melhoria de voz.",
            )
            enhance = False

        if narrator and narration and not _HAS_LIBROSA:
            messagebox.showwarning(
                "librosa não instalado",
                "A Voz de Narrador requer librosa.\n"
                "Rode: pip install librosa\n\n"
                "Processando sem pitch shift.",
            )
            narrator = False

        self._processing = True
        self._btn_process.config(state="disabled", bg=MUTED)
        self._progress.start(12)
        self._txt_log.configure(state="normal")
        self._txt_log.delete("1.0", "end")
        self._txt_log.configure(state="disabled")

        params = {
            "input":      inp,
            "output":     out,
            "logo":       logo,
            "audio":      self._var_audio.get().strip(),
            "narration":  narration,
            "enhance":      enhance,
            "narrator":     narrator,
            "pitch_steps":  float(self._var_pitch_st.get().split()[0]),
            "audio_mode":   self._var_audio_mode.get(),
            "logo_pct":   int(self._var_size.get()),
            "position":   self._var_position.get(),
            "margin":     30,
        }

        threading.Thread(target=_run_processing, args=(params, self._log_queue),
                         daemon=True).start()
        self._poll()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _poll(self):
        try:
            while True:
                kind, msg = self._log_queue.get_nowait()
                if kind == "log":
                    self._log(msg)
                elif kind == "done":
                    self._log(msg, SUCCESS)
                    self._finish(ok=True, msg=msg)
                    return
                elif kind == "error":
                    self._log(msg, ERROR)
                    self._finish(ok=False, msg=msg)
                    return
        except queue.Empty:
            pass
        self.after(150, self._poll)

    def _finish(self, ok: bool, msg: str):
        self._processing = False
        self._progress.stop()
        self._btn_process.config(state="normal", bg=SUCCESS)
        if ok:
            messagebox.showinfo("Concluído", msg)
        else:
            messagebox.showerror("Erro no processamento", msg)

    def _log(self, msg: str, color: str = MUTED):
        self._txt_log.configure(state="normal")
        tag = f"c_{color.replace('#', '')}"
        self._txt_log.insert("end", f"› {msg}\n", tag)
        self._txt_log.tag_config(tag, foreground=color)
        self._txt_log.see("end")
        self._txt_log.configure(state="disabled")

    def _on_size(self, _=None):
        self._lbl_size.config(text=f"{int(self._var_size.get())}%")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
