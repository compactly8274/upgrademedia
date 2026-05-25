import hashlib
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

VIDEO_EXTENSIONS = {'.mkv', '.mp4', '.avi', '.m4v', '.mov', '.ts', '.wmv', '.flv', '.m2ts'}
ENGLISH = {'eng', 'en', 'english', 'und', ''}

WEIGHTS = {
    'resolution':     8,
    'visual_density': 10,
    'video_codec':    5,
    'audio_channels': 4,
    'audio_codec':    3,
    'dynamic_range':  4,
}
TOTAL_WEIGHT = sum(WEIGHTS.values())  # 34


def probe_file(path: str) -> dict | None:
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return None
        return json.loads(result.stdout)
    except Exception as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return None


def compute_hash(path: str) -> str:
    chunk = 1 * 1024 * 1024  # 1 MB sample from start + end — enough for duplicate detection, avoids NAS thrash
    h = hashlib.sha256()
    size = os.path.getsize(path)
    with open(path, 'rb') as f:
        h.update(f.read(min(chunk, size)))
        if size > chunk * 2:
            f.seek(-chunk, 2)
            h.update(f.read(chunk))
    h.update(str(size).encode())
    return h.hexdigest()[:16]


def is_english(stream: dict) -> bool:
    lang = ((stream.get('tags') or {}).get('language') or '').lower().strip()
    return lang in ENGLISH


def is_forced(stream: dict) -> bool:
    return bool((stream.get('disposition') or {}).get('forced', 0))


# ── Scoring components ────────────────────────────────────────────────────────

def _score_resolution(w: int, h: int) -> float:
    px = w * h
    if px >= 3840 * 2160: return 100.0
    if px >= 1920 * 1080: return 75.0
    if px >= 1280 * 720:  return 50.0
    if px >= 854 * 480:   return 25.0
    return 10.0


def _score_video_codec(codec: str) -> float:
    return {
        'hevc': 100.0, 'av1': 100.0, 'vp9': 80.0,
        'h264': 70.0,  'avc': 70.0,
        'mpeg4': 40.0, 'mpeg2video': 20.0,
    }.get(codec.lower(), 50.0)


def _score_visual_density(w: int, h: int, video_bitrate_kbps: float, codec: str) -> float:
    if not all([video_bitrate_kbps, w, h]):
        return 50.0
    bpp = (video_bitrate_kbps * 1000) / (w * h)
    ref = {'hevc': 1.5, 'av1': 1.2, 'h264': 3.0, 'vp9': 2.0}.get(codec.lower(), 3.0)
    ratio = bpp / ref
    if ratio < 0.3:   return 30.0
    if ratio < 0.7:   return 60.0
    if ratio <= 1.5:  return 100.0
    if ratio <= 3.0:  return 75.0
    if ratio <= 6.0:  return 50.0
    return 20.0


def _detect_hdr(video_stream: dict) -> str:
    for sd in (video_stream.get('side_data_list') or []):
        sdt = sd.get('side_data_type', '')
        if 'DOVI' in sdt or 'Dolby Vision' in sdt:
            return 'Dolby Vision'
        if 'HDR10+' in sdt:
            return 'HDR10+'
    ct = video_stream.get('color_transfer', '')
    if ct in ('smpte2084', 'smpte-2084'):
        return 'HDR10'
    if ct == 'arib-std-b67':
        return 'HLG'
    return ''


def _score_dynamic_range(hdr: str) -> float:
    return {
        'dolby vision': 100.0, 'hdr10+': 95.0,
        'hdr10': 90.0, 'hlg': 85.0, '': 50.0,
    }.get(hdr.lower(), 50.0)


def _score_audio_codec(codec: str) -> float:
    return {
        'truehd': 100.0, 'mlp': 100.0, 'flac': 100.0,
        'dts': 85.0, 'eac3': 80.0, 'ac3': 65.0,
        'aac': 60.0, 'mp3': 40.0, 'mp2': 30.0,
    }.get(codec.lower(), 50.0)


def _score_audio_channels(ch: int) -> float:
    if ch >= 8: return 100.0
    if ch >= 6: return 85.0
    if ch >= 4: return 70.0
    if ch >= 2: return 50.0
    return 25.0


# ── Public API ────────────────────────────────────────────────────────────────

def score_file(probe: dict) -> tuple[float, dict]:
    streams = probe.get('streams', [])
    fmt = probe.get('format', {})
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video:
        return 0.0, {}

    w = int(video.get('width', 0))
    h = int(video.get('height', 0))
    codec = (video.get('codec_name') or '').lower()

    video_br = int(video.get('bit_rate', 0) or 0) / 1000
    if not video_br:
        video_br = int(fmt.get('bit_rate', 0) or 0) / 1000 * 0.85

    hdr = _detect_hdr(video)
    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    best_audio = max(audio_streams, key=lambda s: int(s.get('channels', 0)), default={})

    breakdown = {
        'resolution':     _score_resolution(w, h),
        'visual_density': _score_visual_density(w, h, video_br, codec),
        'video_codec':    _score_video_codec(codec),
        'audio_channels': _score_audio_channels(int(best_audio.get('channels', 0)) if best_audio else 0),
        'audio_codec':    _score_audio_codec((best_audio.get('codec_name') or '').lower()),
        'dynamic_range':  _score_dynamic_range(hdr),
    }
    raw = sum(breakdown[k] * WEIGHTS[k] for k in breakdown) / TOTAL_WEIGHT
    return round(raw, 1), {k: round(v, 1) for k, v in breakdown.items()}


def analyse_streams(probe: dict) -> dict:
    streams = probe.get('streams', [])
    non_eng_audio = [s for s in streams if s.get('codec_type') == 'audio' and not is_english(s)]
    non_eng_subs  = [s for s in streams if s.get('codec_type') == 'subtitle'
                     and not is_english(s) and not is_forced(s)]
    audio_langs = list({((s.get('tags') or {}).get('language') or '') for s in streams
                        if s.get('codec_type') == 'audio'})
    sub_langs   = list({((s.get('tags') or {}).get('language') or '') for s in streams
                        if s.get('codec_type') == 'subtitle'})
    return {
        'non_english_audio': len(non_eng_audio),
        'non_english_subs':  len(non_eng_subs),
        'audio_langs': audio_langs,
        'sub_langs':   sub_langs,
    }


def scan_file(path: str) -> dict | None:
    probe = probe_file(path)
    if not probe:
        return None
    streams = probe.get('streams', [])
    fmt = probe.get('format', {})
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    if not video:
        return None

    audio_streams = [s for s in streams if s.get('codec_type') == 'audio']
    best_audio = max(audio_streams, key=lambda s: int(s.get('channels', 0)), default={})
    quality_score, breakdown = score_file(probe)
    stream_info = analyse_streams(probe)
    hdr = _detect_hdr(video)

    return {
        'file_hash':          compute_hash(path),
        'duration_seconds':   float(fmt.get('duration', 0) or 0),
        'video_codec':        (video.get('codec_name') or ''),
        'width':              int(video.get('width', 0)),
        'height':             int(video.get('height', 0)),
        'video_bitrate_kbps': int(video.get('bit_rate', 0) or 0) / 1000,
        'hdr_type':           hdr,
        'audio_codec':        (best_audio.get('codec_name') or '') if best_audio else '',
        'audio_channels':     int(best_audio.get('channels', 0)) if best_audio else 0,
        'non_english_audio':  stream_info['non_english_audio'],
        'non_english_subs':   stream_info['non_english_subs'],
        'audio_langs':        json.dumps(stream_info['audio_langs']),
        'sub_langs':          json.dumps(stream_info['sub_langs']),
        'quality_score':      quality_score,
        'score_breakdown':    json.dumps(breakdown),
    }


def build_strip_command(path: str, probe: dict) -> list | None:
    streams = probe.get('streams', [])
    maps = []
    stripped = False
    for i, s in enumerate(streams):
        t = s.get('codec_type')
        if t == 'video':
            maps += ['-map', f'0:{i}']
        elif t == 'audio':
            if is_english(s):
                maps += ['-map', f'0:{i}']
            else:
                stripped = True
        elif t == 'subtitle':
            if is_english(s) or is_forced(s):
                maps += ['-map', f'0:{i}']
            else:
                stripped = True
        else:
            maps += ['-map', f'0:{i}']  # keep data/attachment streams

    if not stripped:
        return None

    tmp = path + '.stripping.mkv'
    return ['ffmpeg', '-i', path, '-y'] + maps + ['-c', 'copy', tmp]


def walk_media_paths(media_paths: list[str]) -> list[str]:
    found = []
    for root in media_paths:
        root = root.strip()
        if not root or not os.path.isdir(root):
            log.warning("Media path not found or not a directory: %r", root)
            continue
        for dirpath, _dirs, files in os.walk(root):
            for fname in files:
                if Path(fname).suffix.lower() in VIDEO_EXTENSIONS:
                    found.append(os.path.join(dirpath, fname))
    return found
