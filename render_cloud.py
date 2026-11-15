import os
import re
import sys
import glob
import json
import random
import urllib.parse
import textwrap
import requests
import subprocess

# Auto-install required packages if missing in workflow YAML
try:
    import soundfile as sf
    import numpy as np
    from kokoro_onnx import Kokoro
except ImportError:
    subprocess.run([
        sys.executable, "-m", "pip", "install",
        "kokoro-onnx", "soundfile", "numpy", "fonttools", "edge-tts", "gTTS"
    ], check=True)
    import soundfile as sf
    import numpy as np
    from kokoro_onnx import Kokoro

# 1. Auto-sanitize CLOUDINARY_URL BEFORE importing cloudinary
raw_cloud = os.environ.get("CLOUDINARY_URL", "").strip()
if raw_cloud.startswith("CLOUDINARY_URL="):
    raw_cloud = raw_cloud.split("CLOUDINARY_URL=", 1)[1].strip()
os.environ["CLOUDINARY_URL"] = raw_cloud

import cloudinary
import cloudinary.uploader

try:
    if raw_cloud.startswith("cloudinary://") and "@" in raw_cloud:
        creds, cloud_name = raw_cloud.replace("cloudinary://", "", 1).split("@", 1)
        api_key, api_secret = creds.split(":", 1)
        cloudinary.config(
            cloud_name=cloud_name.strip(),
            api_key=api_key.strip(),
            api_secret=api_secret.strip(),
            secure=True
        )
except Exception as e:
    print(f"Cloudinary URL parse warning: {e}")

# 2. Load payload sent from Google Sheets
raw_payload = os.environ.get("PAYLOAD_JSON", "{}")
try:
    payload = json.loads(raw_payload) if raw_payload else {}
except Exception:
    payload = {}

row_id = str(payload.get("row_id", "1")).strip()
title = str(payload.get("title", "Dépaysement")).strip()
niche = str(payload.get("niche", "soul searching")).strip()
overlay_text = str(payload.get(
    "overlay_text",
    "There's a word in French… Dépaysement. It's the dizzy feeling of being somewhere unfamiliar, far from everything that tells you who you are."
)).strip()
search_keyword = str(payload.get("search_keyword", "lone traveler")).strip()
fallback_keyword = str(payload.get("fallback_keyword", "rain")).strip()
caption = str(payload.get(
    "caption",
    "Dépaysement | French\nThe dizzy feeling of being far from everything familiar. #wordsoftheworld #reels"
)).strip()

PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
FREESOUND_API_KEY = os.environ.get("FREESOUND_API_KEY", "").strip()
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL", "").strip()

VO_START_DELAY = 2.0        # Voiceover starts at 2.0 seconds
EXTRA_TAIL_SECONDS = 4.0    # Extra 4.0 seconds of calm ASMR + Piano after voiceover finishes
FADE_DURATION = 1.5         # Smooth 1.5s fade-out at the very end

# Supports both classic nature niches and the 11 emotional niches in words_of_the_world_video_sheet.xlsx
NICHE_AUDIO_MAP = {
    "rain": "gentle rain falling window ambient asmr",
    "ocean": "calm ocean waves beach water ambient",
    "water": "gentle stream river flowing water nature",
    "forest": "forest wind birds leaves nature ambient",
    "fire": "cozy fireplace crackling wood fire asmr",
    "night": "night crickets calm wind ambient",
    "snow": "soft winter wind snow ambient",
    "soul searching": "gentle night wind rain window ambient asmr",
    "relationship": "cozy fireplace warm rain ambient asmr",
    "healing": "gentle stream forest birds nature ambient",
    "gratitude": "morning forest birds gentle breeze ambient",
    "longing": "gentle rain falling window dark night ambient",
    "motivation": "calm ocean waves wind nature ambient",
    "love": "soft fireplace crackling gentle rain ambient",
    "self-love": "gentle rain leaves forest breeze ambient",
    "hope": "morning birds gentle water stream ambient",
    "nostalgia": "vinyl crackle gentle rain window ambient",
    "solitude": "quiet night wind crickets calm ambient"
}

# Native Kokoro-82M Language & Voice Mapping (All inside voices-v1.0.bin)
LANGUAGE_MAP = {
    "french":     {"lang": "fr-fr", "native_voice": "ff_siwis"},
    "spanish":    {"lang": "es",    "native_voice": "ef_dora"},
    "italian":    {"lang": "it",    "native_voice": "if_sara"},
    "japanese":   {"lang": "ja",    "native_voice": "jf_alpha"},
    "portuguese": {"lang": "pt-br", "native_voice": "pf_dora"},
    "hindi":      {"lang": "hi",    "native_voice": "hf_alpha"},
}


def ensure_assets():
    """Downloads Kokoro-82M ONNX models, Poetic Serif Font, and builds Merged Hindi+Latin Serif Font."""
    model_path = "kokoro-v1.0.int8.onnx"
    voices_path = "voices-v1.0.bin"
    font_path = "PoetrySerif.ttf"

    if not os.path.exists(model_path) or os.path.getsize(model_path) < 10_000_000:
        print("Downloading Kokoro-82M int8 model...")
        r = requests.get(
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx",
            timeout=120
        )
        with open(model_path, "wb") as f:
            f.write(r.content)

    if not os.path.exists(voices_path) or os.path.getsize(voices_path) < 1_000_000:
        print("Downloading Kokoro-82M voices pack...")
        r = requests.get(
            "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
            timeout=60
        )
        with open(voices_path, "wb") as f:
            f.write(r.content)

    if not os.path.exists(font_path) or os.path.getsize(font_path) < 40_000:
        font_urls = [
            "https://raw.githubusercontent.com/google/fonts/main/ofl/crimsontext/CrimsonText-Regular.ttf",
            "https://raw.githubusercontent.com/google/fonts/main/ofl/lora/Lora%5Bwght%5D.ttf",
            "https://raw.githubusercontent.com/google/fonts/main/ofl/cormorantgaramond/CormorantGaramond%5Bwght%5D.ttf"
        ]
        for f_url in font_urls:
            try:
                r = requests.get(f_url, timeout=25)
                if r.status_code == 200 and len(r.content) > 40_000:
                    with open(font_path, "wb") as f:
                        f.write(r.content)
                    print(f"Downloaded poetic serif font successfully from {f_url}")
                    break
            except Exception as e:
                print(f"Font attempt skipped: {e}")

    # Build a merged Latin + Devanagari serif font so mixed English + Hindi lines never show boxes
    if not os.path.exists("MergedHindiSerif.ttf"):
        try:
            from fontTools.merge import Merger
            latin_base = "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf"
            dev_base = "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf"
            if os.path.exists(latin_base) and os.path.exists(dev_base):
                merger = Merger()
                merged = merger.merge([latin_base, dev_base])
                merged.save("MergedHindiSerif.ttf")
                print("Created MergedHindiSerif.ttf (Latin + Devanagari) successfully.")
        except Exception as e:
            print(f"Merged Hindi font build skipped: {e}")


def get_font_for_text(line_text):
    """
    Selects the right serif font per line so Latin, Japanese Kanji/Kana, and Hindi Devanagari
    all render cleanly without missing glyph boxes.
    """
    # 1. Check for Japanese (Hiragana, Katakana, CJK Kanji)
    if re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', line_text):
        cjk_candidates = [
            "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc"
        ]
        for p in cjk_candidates:
            if os.path.exists(p):
                return p

    # 2. Check for Hindi Devanagari
    if re.search(r'[\u0900-\u097f]', line_text):
        if os.path.exists("MergedHindiSerif.ttf"):
            return "MergedHindiSerif.ttf"
        dev_path = "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf"
        if os.path.exists(dev_path):
            return dev_path

    # 3. Default Classic Poetic Book Serif for Latin / European accents
    latin_candidates = [
        "PoetrySerif.ttf",
        "/usr/share/fonts/truetype/ebgaramond/EBGaramond12-Regular.ttf",
        "/usr/share/fonts/opentype/ebgaramond/EBGaramond12-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
    ]
    for p in latin_candidates:
        if os.path.exists(p) and os.path.getsize(p) > 20_000:
            return p
    return "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"


def detect_target_language(title_str, overlay_str, caption_str):
    """Detects which of the 6 native languages is featured in this row."""
    combined = f"{overlay_str}\n{caption_str}".lower()
    for lang_name, cfg in LANGUAGE_MAP.items():
        if (
            f"in {lang_name}" in combined
            or f"| {lang_name}" in combined
            or f"{lang_name} has a word" in combined
            or f"#{lang_name}words" in combined
        ):
            return lang_name, cfg["lang"], cfg["native_voice"]

    if re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', f"{title_str} {overlay_str}"):
        return "japanese", "ja", "jf_alpha"
    if re.search(r'[\u0900-\u097f]', f"{title_str} {overlay_str}"):
        return "hindi", "hi", "hf_alpha"

    return "english", "en-us", "af_heart"


def extract_phonetic_hint(title_str, caption_str):
    """
    Extracts parenthesized reading from Caption if present, e.g. '木漏れ日 (komorebi) | Japanese' -> 'komorebi',
    used only if Japanese Kanji phonemizer needs a phonetic fallback.
    """
    first_line = caption_str.splitlines()[0] if caption_str else ""
    m = re.search(r'\(([^)]+)\)', first_line)
    if m:
        return m.group(1).strip()
    return title_str


def prepare_poetic_lines(title_text, body_text):
    """
    Formats:
      1) Title at the very top in Yellow (#FFD700) and larger font (fontsize=58)
      2) Overlay_Text stanzas below in soft white (#f5f5f3) and fontsize=45
    Returns a list of dicts: {fname, rel_y, fontsize, fontcolor, fontfile} and total_block_height.
    """
    items = []
    current_y = 0
    file_idx = 0

    # --- 1. TITLE BLOCK (First on video, Yellow, Bigger than overlay_text) ---
    clean_title = (title_text or "").strip()
    if clean_title:
        title_wrapped = textwrap.wrap(clean_title, width=22) or [clean_title]
        title_line_height = 72
        title_bottom_gap = 55  # Extra breathing room between yellow Title and body text

        for t_line in title_wrapped:
            fname = f"quote_line_{file_idx}.txt"
            with open(fname, "w", encoding="utf-8", newline="\n") as f:
                f.write(t_line)
            items.append({
                "fname": fname,
                "rel_y": current_y,
                "fontsize": 58,           # Bigger than overlay_text (45)
                "fontcolor": "#FFD700",   # Warm Golden Yellow
                "fontfile": get_font_for_text(t_line)
            })
            current_y += title_line_height
            file_idx += 1

        current_y += title_bottom_gap

    # --- 2. OVERLAY_TEXT BLOCK (Poetic stanzas below Title) ---
    cleaned = (body_text or "").replace("\r\n", "\n").replace("\u2028", "\n").replace("\u2029", "\n").strip()
    if not cleaned:
        cleaned = "Peace comes from within.\nDo not seek it without."

    raw_lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
    if len(raw_lines) == 1 and ". " in cleaned:
        stanzas = [s.strip() for s in re.split(r'(?<=[.!?])\s+', cleaned) if s.strip()]
    else:
        stanzas = [s.strip() for s in re.split(r'\n\s*\n', cleaned) if s.strip()]

    line_height = 58
    stanza_gap = 34

    for s_idx, stanza in enumerate(stanzas):
        for raw_line in stanza.split("\n"):
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            wrapped = textwrap.wrap(raw_line, width=30)
            for w_line in wrapped:
                fname = f"quote_line_{file_idx}.txt"
                with open(fname, "w", encoding="utf-8", newline="\n") as f:
                    f.write(w_line)
                items.append({
                    "fname": fname,
                    "rel_y": current_y,
                    "fontsize": 45,           # Standard poetic body size
                    "fontcolor": "#f5f5f3",   # Soft off-white
                    "fontfile": get_font_for_text(w_line)
                })
                current_y += line_height
                file_idx += 1

        if s_idx < len(stanzas) - 1:
            current_y += stanza_gap

    total_height = max(current_y - line_height, 60)
    return items, total_height


def get_duration(filename):
    try:
        out = subprocess.check_output([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", filename
        ], text=True).strip()
        return float(out)
    except Exception:
        return 10.0


def synthesize_kokoro_segment(kokoro, text_seg, voice_vec, lang_code, speed=0.83, fallback_text=None):
    """Synthesizes a single phrase with Kokoro-82M, with safe fallback for Kanji/non-Latin tokens."""
    try:
        samples, sr = kokoro.create(text_seg, voice=voice_vec, speed=speed, lang=lang_code)
        if samples is not None and len(samples) > 400:
            return samples, sr
    except Exception as e:
        print(f"Direct Kokoro phoneme warning for '{text_seg}' [{lang_code}]: {e}")

    if fallback_text and fallback_text != text_seg:
        try:
            # Pronounce using phonetic reading with native vowel rules (e.g. Italian/Spanish/Japanese)
            phonetic_lang = "it" if lang_code == "ja" else lang_code
            samples, sr = kokoro.create(fallback_text, voice=voice_vec, speed=speed, lang=phonetic_lang)
            if samples is not None and len(samples) > 400:
                return samples, sr
        except Exception as e2:
            print(f"Phonetic fallback warning: {e2}")

    samples, sr = kokoro.create(fallback_text or text_seg, voice=voice_vec, speed=speed, lang="en-us")
    return samples, sr


def generate_kokoro_asmr_voiceover(title_word, text, caption_text):
    """
    Keeps the exact same 55% af_nicole + 45% af_heart English ASMR voice quality throughout,
    while switching Kokoro-82M to the native language phonemizer (fr-fr, es, it, pt-br, hi, ja)
    whenever pronouncing the foreign Title word.
    """
    chunks = re.split(r'(?<=[.!?])\s+|\n+', text.replace("\r", ""))
    phrases = [c.strip() for c in chunks if c.strip()]
    if not phrases:
        phrases = ["Peace comes from within."]

    lang_name, native_lang_code, native_voice_name = detect_target_language(title_word, text, caption_text)
    phonetic_hint = extract_phonetic_hint(title_word, caption_text)
    print(f"Detected row language: {lang_name.upper()} (Kokoro lang='{native_lang_code}', Title='{title_word}')")

    raw_gen = False
    try:
        kokoro = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
        try:
            v_nicole = kokoro.get_voice_style("af_nicole")
            v_heart = kokoro.get_voice_style("af_heart")
            english_asmr_vec = (v_nicole * 0.55) + (v_heart * 0.45)

            # Keep 80% of the exact English ASMR voice + 20% native voice vector for authentic native articulation
            if native_lang_code != "en-us":
                v_native = kokoro.get_voice_style(native_voice_name)
                native_asmr_vec = (english_asmr_vec * 0.80) + (v_native * 0.20)
            else:
                native_asmr_vec = english_asmr_vec
            print("Using 55% af_nicole + 45% af_heart ASMR voice vector with native pronunciation switching.")
        except Exception:
            english_asmr_vec = "af_nicole"
            native_asmr_vec = "af_nicole"

        audio_segments = []
        sample_rate = 24000
        clean_title = (title_word or "").strip()

        for i, phrase in enumerate(phrases):
            # Check if this sentence introduces the foreign Title word (e.g. "There's a word in French… Dépaysement.")
            if clean_title and clean_title in phrase and native_lang_code != "en-us":
                parts = phrase.split(clean_title, 1)
                prefix_en = re.sub(r'[….:\-–—]+\s*$', '', parts[0]).strip()
                suffix_en = re.sub(r'^[.!?…,\s]+', '', parts[1]).strip()

                # 1. Speak the English lead-in ("There's a word in French") in English ASMR
                if prefix_en:
                    s_pre, sample_rate = synthesize_kokoro_segment(
                        kokoro, prefix_en + ".", english_asmr_vec, "en-us", speed=0.83
                    )
                    audio_segments.append(s_pre)
                    audio_segments.append(np.zeros(int(sample_rate * 0.38), dtype=np.float32))

                # 2. Speak the Title word in its NATIVE language with the same ASMR voice quality
                s_word, sample_rate = synthesize_kokoro_segment(
                    kokoro, clean_title, native_asmr_vec, native_lang_code, speed=0.80, fallback_text=phonetic_hint
                )
                audio_segments.append(s_word)

                # 3. Speak any trailing words in the sentence in English
                if suffix_en:
                    audio_segments.append(np.zeros(int(sample_rate * 0.35), dtype=np.float32))
                    s_suf, sample_rate = synthesize_kokoro_segment(
                        kokoro, suffix_en, english_asmr_vec, "en-us", speed=0.83
                    )
                    audio_segments.append(s_suf)
            else:
                # Standard English sentence (or pure native sentence)
                s_seg, sample_rate = synthesize_kokoro_segment(
                    kokoro, phrase, english_asmr_vec, "en-us", speed=0.83
                )
                audio_segments.append(s_seg)

            # 1.10-second meditative silence gap between sentences
            if i < len(phrases) - 1:
                pause_samples = np.zeros(int(sample_rate * 1.10), dtype=np.float32)
                audio_segments.append(pause_samples)

        full_audio = np.concatenate(audio_segments)
        sf.write("voice_raw.wav", full_audio, sample_rate)

        if os.path.exists("voice_raw.wav") and os.path.getsize("voice_raw.wav") > 1000:
            print("Generated blended Kokoro-82M bilingual ASMR voiceover successfully!")
            raw_gen = True
    except Exception as e:
        print(f"Kokoro-82M error ({e}), falling back to Edge-TTS...")

    if not raw_gen:
        try:
            import edge_tts
            import asyncio
            communicate = edge_tts.Communicate(text, voice="en-GB-RyanNeural", rate="-12%", pitch="-2Hz")
            asyncio.run(communicate.save("voice_raw.wav"))
            raw_gen = True
        except Exception as e2:
            print(f"Fallback TTS failed: {e2}")

    if raw_gen:
        try:
            # 12ms Binaural Ribbon-Mic Mastering Chain
            subprocess.run([
                "ffmpeg", "-y", "-i", "voice_raw.wav",
                "-af", (
                    "aformat=sample_rates=44100:channel_layouts=stereo,"
                    "highpass=f=65,"
                    "equalizer=f=130:width_type=o:w=1.5:g=3.0,"
                    "equalizer=f=6200:width_type=o:w=1.5:g=-4.0,"
                    "lowpass=f=8000,"
                    "adelay=0|12,"
                    "afade=t=in:st=0:d=0.15,"
                    "volume=2.1"
                ),
                "-ar", "44100", "-ac", "2", "voice.mp3"
            ], check=True)
            return True
        except Exception as e:
            print(f"Mastering fallback: {e}")
            subprocess.run([
                "ffmpeg", "-y", "-i", "voice_raw.wav",
                "-af", "volume=2.0", "-ar", "44100", "-ac", "2", "voice.mp3"
            ], check=True)
            return True

    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=10.0", "voice.mp3"
    ], check=True)
    return False


def detect_niche_keyword():
    niche_lower = niche.lower().strip()
    if niche_lower in NICHE_AUDIO_MAP:
        return niche_lower
    combined = f"{niche} {search_keyword} {fallback_keyword}".lower()
    for key in NICHE_AUDIO_MAP:
        if key in combined:
            return key
    return "rain"


def download_visual_background(target_duration):
    if PIXABAY_API_KEY:
        try:
            queries = [
                urllib.parse.quote(search_keyword),
                urllib.parse.quote(fallback_keyword),
                "calm+nature+rain"
            ]
            hits = []
            for q in queries:
                if not q:
                    continue
                url = (
                    f"https://pixabay.com/api/videos/?key={PIXABAY_API_KEY}"
                    f"&q={q}&orientation=vertical&per_page=15&safesearch=true"
                )
                r = requests.get(url, timeout=20)
                hits = r.json().get("hits", []) if r.status_code == 200 else []
                if hits:
                    break

            if hits:
                chosen = random.choice(hits)
                vids = chosen.get("videos", {})
                v_url = (
                    vids.get("large", {}).get("url")
                    or vids.get("medium", {}).get("url")
                    or vids.get("small", {}).get("url")
                )
                if v_url:
                    vid_data = requests.get(v_url, timeout=60).content
                    if len(vid_data) > 50_000:
                        with open("raw_video.mp4", "wb") as f:
                            f.write(vid_data)
                        print("Downloaded HD video from Pixabay successfully.")
                        return
        except Exception as e:
            print(f"Pixabay skipped: {e}")

    try:
        prompt = urllib.parse.quote(
            f"cinematic 35mm kodak film dark moody relaxing aesthetic {search_keyword} nature photography, 8k, no text"
        )
        seed = random.randint(1, 999999)
        img_url = f"https://image.pollinations.ai/prompt/{prompt}?width=1080&height=1920&seed={seed}&nologo=true"
        r = requests.get(img_url, timeout=35)
        if r.status_code == 200 and len(r.content) > 10_000:
            with open("bg_scene.jpg", "wb") as f:
                f.write(r.content)
            subprocess.run([
                "ffmpeg", "-y", "-loop", "1", "-i", "bg_scene.jpg",
                "-vf", "scale=1080:1920,fps=30",
                "-t", f"{target_duration:.2f}",
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "raw_video.mp4"
            ], check=True)
            return
    except Exception as e:
        print(f"Pollinations fallback skipped: {e}")

    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"color=c=#0d1b2a:s=1080x1920:r=30:d={target_duration:.2f}",
        "-vf", "noise=alls=8:allf=t+u",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "raw_video.mp4"
    ], check=True)


def fetch_asmr_audio(matched_key, target_duration):
    if FREESOUND_API_KEY:
        try:
            q_str = NICHE_AUDIO_MAP.get(matched_key, f"{search_keyword} ambient")
            query = urllib.parse.quote(q_str)
            url = (
                f"https://freesound.org/apiv2/search/text/"
                f"?query={query}&filter=duration:[15 TO 180]&fields=id,name,previews"
                f"&page_size=8&token={FREESOUND_API_KEY}"
            )
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    chosen = random.choice(results)
                    mp3_url = chosen.get("previews", {}).get("preview-hq-mp3")
                    if mp3_url:
                        audio_data = requests.get(mp3_url, timeout=20).content
                        if len(audio_data) > 5000:
                            with open("asmr.mp3", "wb") as f:
                                f.write(audio_data)
                            return
        except Exception as e:
            print(f"Freesound ASMR fallback triggered: {e}")

    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi",
        "-i", f"anoisesrc=d={target_duration:.2f}:c=pink:r=44100:a=0.10",
        "-af", "bandpass=f=900:width_type=h:w=700,tremolo=f=0.4:d=0.35,extrastereo=m=2.0",
        "asmr.mp3"
    ], check=True)


def fetch_relaxing_piano(target_duration):
    """Automatically detects your uploaded custom piano.mp3 before falling back to Freesound."""
    for filename in ["piano.mp3", "bgm.mp3", "audio/piano.mp3", "audio/default.mp3"]:
        if os.path.exists(filename) and os.path.getsize(filename) > 10_000:
            print(f"Using custom uploaded track: {filename}")
            return filename

    excluded = {"asmr.mp3", "voice.mp3", "voice_raw.mp3", "cloud_piano.mp3", "synth_piano.mp3", "pause_gap.mp3"}
    custom_candidates = []
    for pattern in ["*.mp3", "music/*.mp3", "audio/*.mp3"]:
        for f in glob.glob(pattern):
            if os.path.basename(f) not in excluded and os.path.getsize(f) > 10_000:
                custom_candidates.append(f)
    if custom_candidates:
        chosen_custom = random.choice(custom_candidates)
        print(f"Using custom uploaded music track: {chosen_custom}")
        return chosen_custom

    if FREESOUND_API_KEY:
        try:
            query = urllib.parse.quote("soft relaxing piano calm ambient instrumental")
            url = (
                f"https://freesound.org/apiv2/search/text/"
                f"?query={query}&filter=duration:[20 TO 240]&fields=id,name,previews"
                f"&page_size=10&token={FREESOUND_API_KEY}"
            )
            r = requests.get(url, timeout=15)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    chosen = random.choice(results)
                    mp3_url = chosen.get("previews", {}).get("preview-hq-mp3")
                    if mp3_url:
                        audio_data = requests.get(mp3_url, timeout=20).content
                        if len(audio_data) > 5000:
                            with open("cloud_piano.mp3", "wb") as f:
                                f.write(audio_data)
                            return "cloud_piano.mp3"
        except Exception as e:
            print(f"Freesound Piano fallback triggered: {e}")

    subprocess.run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=261.63:duration={target_duration:.2f}",
        "-f", "lavfi", "-i", f"sine=frequency=329.63:duration={target_duration:.2f}",
        "-f", "lavfi", "-i", f"sine=frequency=392.00:duration={target_duration:.2f}",
        "-f", "lavfi", "-i", f"sine=frequency=493.88:duration={target_duration:.2f}",
        "-filter_complex", (
            "[0:a]tremolo=f=0.25:d=0.6[a0];"
            "[1:a]tremolo=f=0.33:d=0.6[a1];"
            "[2:a]tremolo=f=0.20:d=0.6[a2];"
            "[3:a]tremolo=f=0.15:d=0.7[a3];"
            "[a0][a1][a2][a3]amix=inputs=4:duration=first,"
            "lowpass=f=650,aecho=0.8:0.88:120|240:0.4|0.3,volume=0.18"
        ),
        "synth_piano.mp3"
    ], check=True)
    return "synth_piano.mp3"


def render_ffmpeg(piano_file, vo_duration, total_duration):
    raw_vid_duration = get_duration("raw_video.mp4")
    if raw_vid_duration < total_duration and raw_vid_duration > 0.5:
        pts_multiplier = (total_duration + 0.5) / raw_vid_duration
        print(f"Video is {raw_vid_duration:.2f}s (< {total_duration:.2f}s). Stretching speed (setpts={pts_multiplier:.4f}*PTS).")
    else:
        pts_multiplier = 1.0

    piano_dur = get_duration(piano_file)
    max_offset = max(0.0, piano_dur - total_duration - 2.0)
    piano_start = random.uniform(0.0, max_offset) if max_offset > 4.0 else 0.0

    line_items, total_height = prepare_poetic_lines(title, overlay_text)
    fade_start = max(total_duration - FADE_DURATION, total_duration - 2.0)
    text_fade_start = max(fade_start - 0.3, 4.0)
    text_fade_end = text_fade_start + 1.3

    drawtext_filters = []
    for idx, item in enumerate(line_items):
        start_t = 0.5 + (idx * 0.35)
        fade_in_end = start_t + 0.85
        line_alpha = (
            f"if(lt(t,{start_t:.2f}),0,"
            f"if(lt(t,{fade_in_end:.2f}),(t-{start_t:.2f})/0.85,"
            f"if(lt(t,{text_fade_start:.2f}),1,"
            f"if(lt(t,{text_fade_end:.2f}),({text_fade_end:.2f}-t)/1.3,0))))"
        )
        y_offset = f"(h-{total_height})/2-70+{item['rel_y']}"
        drawtext_filters.append(
            f"drawtext=fontfile={item['fontfile']}:"
            f"textfile={item['fname']}:"
            f"fontcolor={item['fontcolor']}:"
            f"fontsize={item['fontsize']}:"
            f"x=76:"
            f"y={y_offset}:"
            f"shadowcolor=black@0.85:shadowx=2:shadowy=3:"
            f"enable='gte(t,{start_t:.2f})':"
            f"alpha='{line_alpha}'"
        )

    drawtext_filters_str = ",".join(drawtext_filters)
    vo_start = VO_START_DELAY
    vo_end = VO_START_DELAY + vo_duration

    v_filter = (
        f"[0:v]setpts={pts_multiplier:.4f}*PTS,"
        f"scale=1080:1920:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,setsar=1,fps=30,"
        f"eq=brightness=-0.05:contrast=1.08:saturation=0.82,"
        f"colorbalance=rs=-0.02:bs=0.03:rh=0.03:gh=0.01:bh=-0.02,"
        f"noise=alls=4:allf=t+u,"
        f"vignette=PI/4,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.46:t=fill,"
        f"{drawtext_filters_str},"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={fade_start:.2f}:d={FADE_DURATION}[v]"
    )

    a_filter = (
        f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"lowpass=f=3000,volume='if(between(t,{vo_start:.2f},{vo_end:.2f}),0.16,0.35)':eval=frame[piano];"
        f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"volume='if(between(t,{vo_start:.2f},{vo_end:.2f}),0.20,0.42)':eval=frame[asmr];"
        f"[3:a]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"adelay=2000|2000,apad=whole_dur={total_duration:.2f}[voice];"
        f"[piano][asmr][voice]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,"
        f"afade=t=in:st=0:d=1.0,"
        f"afade=t=out:st={fade_start:.2f}:d={FADE_DURATION}[a]"
    )

    filter_complex = f"{v_filter};{a_filter}"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", "raw_video.mp4",
        "-ss", f"{piano_start:.2f}", "-stream_loop", "-1", "-i", piano_file,
        "-stream_loop", "-1", "-i", "asmr.mp3",
        "-i", "voice.mp3",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-t", f"{total_duration:.2f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "final_output.mp4"
    ], check=True)


def upload_video_with_fallback():
    try:
        res = cloudinary.uploader.upload_large(
            "final_output.mp4",
            resource_type="video",
            folder="auto_relaxing_videos"
        )
        video_url = res.get("secure_url")
        if video_url:
            print(f"Uploaded to Cloudinary successfully: {video_url}")
            return video_url
    except Exception as e:
        print(f"Cloudinary upload skipped ({e}). Uploading to backup free MP4 host...")

    try:
        with open("final_output.mp4", "rb") as f:
            r = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "72h"},
                files={"fileToUpload": ("final_output.mp4", f, "video/mp4")},
                timeout=60
            )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            return r.text.strip()
    except Exception as e2:
        print(f"Litterbox backup failed: {e2}")

    with open("final_output.mp4", "rb") as f:
        r = requests.post(
            "https://catbox.moe/user/api.php",
            data={"reqtype": "fileupload"},
            files={"fileToUpload": ("final_output.mp4", f, "video/mp4")},
            timeout=60
        )
    return r.text.strip()


def main():
    ensure_assets()

    # Step 1: Generate Voiceover FIRST (with native Title pronunciation) so we know its exact length
    generate_kokoro_asmr_voiceover(title, overlay_text, caption)
    vo_duration = get_duration("voice.mp3")

    # Step 2: Calculate Dynamic Total Video Duration = 2.0s intro + full voiceover + 4.0s extra tail
    total_duration = max(12.0, round(VO_START_DELAY + vo_duration + EXTRA_TAIL_SECONDS, 2))
    print(f"Voiceover length: {vo_duration:.2f}s -> Dynamic Total Video Duration set to: {total_duration:.2f}s (includes +4.0s tail)")

    # Step 3: Download/generate visual & audio tracks matched to total_duration
    matched_niche = detect_niche_keyword()
    download_visual_background(total_duration)
    fetch_asmr_audio(matched_niche, total_duration)
    piano_file = fetch_relaxing_piano(total_duration)

    # Step 4: Render final video with Yellow Title header, left-aligned poetic layout, and dynamic duration
    render_ffmpeg(piano_file, vo_duration, total_duration)
    video_url = upload_video_with_fallback()

    if MAKE_WEBHOOK_URL and MAKE_WEBHOOK_URL.startswith("http"):
        resp = requests.post(
            MAKE_WEBHOOK_URL,
            json={
                "row_id": row_id,
                "title": title,
                "video_url": video_url,
                "caption": caption
            },
            timeout=20
        )
        print(f"Sent webhook to Make.com successfully! Status: {resp.status_code}")
    else:
        print("Warning: MAKE_WEBHOOK_URL is not set or invalid; skipped Make webhook.")


if __name__ == "__main__":
    main()
def upload_video_with_fallback():
    # 1. Primary: Standard Cloudinary Video Upload
    try:
        res = cloudinary.uploader.upload(
            "final_output.mp4",
            resource_type="video",
            folder="auto_relaxing_videos",
            timeout=120
        )
        video_url = res.get("secure_url")
        if video_url and video_url.startswith("http"):
            print(f"Uploaded to Cloudinary successfully: {video_url}")
            return video_url
    except Exception as e:
        print(f"Cloudinary upload skipped ({e}). Uploading to backup free MP4 host...")

    # 2. Backup 1: Litterbox (72h direct MP4 link)
    try:
        with open("final_output.mp4", "rb") as f:
            r = requests.post(
                "https://litterbox.catbox.moe/resources/internals/api.php",
                data={"reqtype": "fileupload", "time": "72h"},
                files={"fileToUpload": ("final_output.mp4", f, "video/mp4")},
                timeout=60
            )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            url = r.text.strip()
            print(f"Uploaded to Litterbox successfully: {url}")
            return url
    except Exception as e2:
        print(f"Litterbox backup failed: {e2}")

    # 3. Backup 2: tmpfiles.org (Direct download URL conversion for Make.com)
    try:
        with open("final_output.mp4", "rb") as f:
            r = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": ("final_output.mp4", f, "video/mp4")},
                timeout=60
            )
        if r.status_code == 200:
            raw_url = r.json().get("data", {}).get("url", "")
            if raw_url.startswith("http"):
                direct_url = raw_url.replace("tmpfiles.org/", "tmpfiles.org/dl/", 1)
                print(f"Uploaded to tmpfiles.org successfully: {direct_url}")
                return direct_url
    except Exception as e3:
        print(f"tmpfiles backup failed: {e3}")

    # 4. Backup 3: Catbox.moe
    try:
        with open("final_output.mp4", "rb") as f:
            r = requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": ("final_output.mp4", f, "video/mp4")},
                timeout=60
            )
        if r.status_code == 200 and r.text.strip().startswith("http"):
            url = r.text.strip()
            print(f"Uploaded to Catbox successfully: {url}")
            return url
    except Exception as e4:
        print(f"Catbox backup failed: {e4}")

    return ""


def main():
    ensure_assets()

    # Step 1: Generate Voiceover FIRST (with native Title pronunciation) so we know its exact length
    generate_kokoro_asmr_voiceover(title, overlay_text, caption)
    vo_duration = get_duration("voice.mp3")

    # Step 2: Calculate Dynamic Total Video Duration = 2.0s intro + full voiceover + 4.0s extra tail
    total_duration = max(12.0, round(VO_START_DELAY + vo_duration + EXTRA_TAIL_SECONDS, 2))
    print(f"Voiceover length: {vo_duration:.2f}s -> Dynamic Total Video Duration set to: {total_duration:.2f}s (includes +4.0s tail)")

    # Step 3: Download/generate visual & audio tracks matched to total_duration
    matched_niche = detect_niche_keyword()
    download_visual_background(total_duration)
    fetch_asmr_audio(matched_niche, total_duration)
    piano_file = fetch_relaxing_piano(total_duration)

    # Step 4: Render final video with Yellow Title header, left-aligned poetic layout, and dynamic duration
    render_ffmpeg(piano_file, vo_duration, total_duration)
    video_url = upload_video_with_fallback()
    print(f"Final Hosted Video URL: {video_url}")

    # Step 5: Send payload to Make.com Webhook
    if not MAKE_WEBHOOK_URL or not MAKE_WEBHOOK_URL.startswith("http"):
        raise RuntimeError(
            f"MAKE_WEBHOOK_URL is missing or invalid in GitHub Secrets! Current value starts with: '{MAKE_WEBHOOK_URL[:12]}'"
        )
    if not video_url or not video_url.startswith("http"):
        raise RuntimeError("Video upload failed across all hosts; no valid video_url to send to Make.com!")

    webhook_payload = {
        "row_id": row_id,
        "title": title,
        "video_url": video_url,
        "caption": caption
    }
    resp = requests.post(
        MAKE_WEBHOOK_URL,
        json=webhook_payload,
        headers={"Content-Type": "application/json"},
        timeout=30
    )
    print(f"Sent webhook to Make.com! Status: {resp.status_code} | Response: {resp.text}")
    resp.raise_for_status()


if __name__ == "__main__":
    main()
