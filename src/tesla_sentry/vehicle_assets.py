"""Validate vehicle files, normalize lock audio, and commit USB writes atomically."""
import hashlib
import io
import logging
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import wave
import zlib

from . import archive, config, locking, media, storage

MAX_AUDIO = 20 * 1024 * 1024
MAX_WRAP = 1000000
CHIME_NAME = "LockChime.wav"
_CONVERT = threading.BoundedSemaphore(1)
LOG = logging.getLogger(__name__)


def _wav(data, start=0, duration=5):
    """Read the normalized profile without requiring ffmpeg (also verifies output)."""
    with wave.open(io.BytesIO(data), "rb") as sound:
        if (sound.getnchannels(), sound.getsampwidth(), sound.getframerate(), sound.getcomptype()) != (1, 2, 44100, "NONE"):
            raise ValueError("WAV profile needs conversion")
        sound.setpos(min(int(start * 44100), sound.getnframes()))
        frames = sound.readframes(int(duration * 44100))
    if not frames or len(frames) % 2:
        raise ValueError("没有可用的音频, 请检查起始时间")
    out = io.BytesIO()
    with wave.open(out, "wb") as sound:
        sound.setnchannels(1)
        sound.setsampwidth(2)
        sound.setframerate(44100)
        sound.writeframes(frames)
    return out.getvalue(), len(frames) / 88200.0


def lock_chime(name, data, start=0, duration=5):
    """Return real PCM WAV, <=5 seconds and <1 MB; never rename encoded input."""
    extension = os.path.splitext(name or "")[1].lower()
    if extension not in (".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".webm"):
        raise ValueError("支持 WAV / MP3 / M4A / AAC / FLAC / OGG / OPUS / WEBM")
    if not data or len(data) > MAX_AUDIO:
        raise ValueError("原始音频须为 1 字节到 20 MB")
    try:
        start, duration = float(start), float(duration)
    except (TypeError, ValueError):
        raise ValueError("裁剪时间必须是数字")
    if not math.isfinite(start) or not math.isfinite(duration) or not 0 <= start <= 3600 or not 0.1 <= duration <= 5:
        raise ValueError("起始时间为 0–3600 秒, 时长为 0.1–5 秒")
    try:
        return _wav(data, start, duration)
    except (wave.Error, EOFError, ValueError):
        pass
    if not _CONVERT.acquire(blocking=False):
        raise ValueError("正在转换另一个音频, 请稍后重试")
    try:
        with tempfile.TemporaryDirectory(prefix="tsf-chime-") as work:
            source, target = os.path.join(work, "source" + extension), os.path.join(work, CHIME_NAME)
            with open(source, "wb") as stream:
                stream.write(data)
            try:
                result = subprocess.run([
                    media.FFMPEG_BIN, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                    "-protocol_whitelist", "file,pipe", "-ss", str(start), "-i", source,
                    "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", str(duration),
                    "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", "-threads", "1",
                    "-map_metadata", "-1", "-f", "wav", target,
                ], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=45)
            except FileNotFoundError:
                raise ValueError("设备缺少 ffmpeg; 请安装 ffmpeg, 或上传 44.1kHz/16-bit/单声道 PCM WAV")
            except subprocess.TimeoutExpired:
                raise ValueError("音频转换超时, 请上传更短的源文件")
            if result.returncode or not os.path.isfile(target):
                raise ValueError("无法解码音频, 请检查文件格式及裁剪起点")
            with open(target, "rb") as stream:
                converted = stream.read(1000000)
            if len(converted) >= 1000000:
                raise ValueError("转换后的 WAV 超过 1 MB")
            try:
                return _wav(converted, duration=duration)
            except (wave.Error, EOFError, ValueError):
                raise ValueError("转换后的 WAV 无效或没有声音数据")
    finally:
        _CONVERT.release()


@locking.archive_locked
def queue_chime(data):
    dest = archive.safe_archive_dir(os.path.join(config.settings.audio_queue, CHIME_NAME))
    if not dest:
        raise ValueError("音频队列路径无效")
    storage.atomic_write(dest, data)


@locking.archive_locked
def queued_audio(name):
    name = archive.safe_name(name)
    path = archive.safe_archive_path(os.path.join(config.settings.audio_queue, name)) if name else None
    if not path or os.path.getsize(path) > MAX_AUDIO:
        raise ValueError("队列文件不存在或超过 20 MB")
    with open(path, "rb") as stream:
        return stream.read(MAX_AUDIO + 1)


@locking.archive_locked
def flush_audio(mount):
    """Caller owns the USB lease. Hold archive lock through copy AND queue removal."""
    count = 0
    queue = archive.safe_archive_dir(config.settings.audio_queue)
    done = archive.safe_archive_dir(config.settings.audio_done)
    if not queue or not done:
        raise ValueError("音频队列路径无效")
    os.makedirs(queue, exist_ok=True)
    os.makedirs(done, exist_ok=True)
    for name in sorted(os.listdir(queue)):
        if not archive.safe_name(name) or not name.lower().endswith((".wav", ".mp3")):
            continue
        source = os.path.join(queue, name)
        if os.path.islink(source) or not os.path.isfile(source):
            continue
        target = os.path.join(mount, name)
        if os.path.islink(target):
            raise ValueError("USB 音频目标不可为符号链接")
        try:
            with open(source, "rb") as stream:
                data = stream.read(MAX_AUDIO + 1)
            if len(data) > MAX_AUDIO:
                raise ValueError("队列文件超过 20 MB")
            if name == CHIME_NAME:
                data, _duration = lock_chime(name, data)
            storage.atomic_write(target, data, mode=None)
            storage.atomic_write(os.path.join(done, name), data)
            os.unlink(source)
            storage.sync_directory(queue)
            count += 1
        except (OSError, ValueError) as error:
            LOG.warning("Audio %s remains queued: %s", name, error)
    return count


def validate_wrap(data):
    """Check PNG structure, CRCs, dimensions and bounded image data without Pillow."""
    if not data or len(data) > MAX_WRAP:
        raise ValueError("涂装 PNG 不得超过 1 MB (1,000,000 字节)")
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("车机涂装只支持 PNG; 改扩展名不会转换图片")
    pos, width, height, compressed, ended = 8, 0, 0, bytearray(), False
    depth = color = interlace = 0
    palette, idat_started, idat_closed = False, False, False
    while pos + 12 <= len(data):
        size, kind = struct.unpack_from(">I4s", data, pos)
        end = pos + 12 + size
        if end > len(data):
            raise ValueError("PNG 文件不完整")
        if not re.fullmatch(b"[A-Za-z]{4}", kind) or kind[2] & 32:
            raise ValueError("PNG 数据块类型无效")
        chunk = data[pos + 8:end - 4]
        if zlib.crc32(kind + chunk) & 0xffffffff != struct.unpack_from(">I", data, end - 4)[0]:
            raise ValueError("PNG 校验失败")
        if pos == 8 and kind != b"IHDR":
            raise ValueError("PNG 缺少图像头")
        if kind == b"IHDR":
            if width or size != 13:
                raise ValueError("PNG 图像头无效")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", chunk)
            if width != height or not 512 <= width <= 1024:
                raise ValueError("涂装须为 512×512 到 1024×1024 的正方形, 请使用对应车型模板")
            depths = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
            if depth not in depths.get(color, ()) or compression or filtering or interlace not in (0, 1):
                raise ValueError("PNG 编码无效")
        elif kind == b"PLTE":
            if palette or idat_started or color in (0, 4) or not size or size % 3 or size > 768:
                raise ValueError("PNG 调色板无效")
            if color == 3 and size // 3 > 2 ** depth:
                raise ValueError("PNG 调色板过大")
            palette = True
        elif kind == b"IDAT":
            if idat_closed or (color == 3 and not palette):
                raise ValueError("PNG 像素块顺序或调色板无效")
            idat_started = True
            compressed.extend(chunk)
        elif kind == b"acTL":
            raise ValueError("请上传静态 PNG, 不支持动画涂装")
        elif kind == b"IEND":
            if size or end != len(data):
                raise ValueError("PNG 结束标记无效")
            ended = True
            break
        elif not kind[0] & 32:
            raise ValueError("PNG 包含不支持的关键数据块")
        if idat_started and kind != b"IDAT":
            idat_closed = True
        pos = end
    if not width or not compressed or not ended:
        raise ValueError("PNG 文件不完整")
    decoder = zlib.decompressobj()
    try:
        pixels = decoder.decompress(bytes(compressed), 9 * 1024 * 1024)
    except zlib.error:
        raise ValueError("PNG 像素数据损坏")
    if not decoder.eof or decoder.unused_data or not pixels:
        raise ValueError("PNG 像素数据无效或过大")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    # Adam7 stores seven passes, each with its own scanline filter byte.
    passes = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
              (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)) if interlace else ((0, 0, 1, 1),)
    offset = 0
    for x, y, dx, dy in passes:
        pw, ph = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if not pw or not ph:
            continue
        row = (pw * channels * depth + 7) // 8 + 1
        end = offset + row * ph
        if end > len(pixels) or any(pixels[i] > 4 for i in range(offset, end, row)):
            raise ValueError("PNG 扫描行无效")
        offset = end
    if offset != len(pixels):
        raise ValueError("PNG 像素长度无效")
    return width, height


def wrap_name(name):
    original = os.path.splitext(os.path.basename((name or "").replace("\\", "/")))[0]
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "_", original).strip(" _-") or "wrap"
    if cleaned != original or len(cleaned) > 26:
        cleaned = cleaned[:17] + "-" + hashlib.sha256(original.encode()).hexdigest()[:8]
    return cleaned + ".png"  # <=30 characters including extension


def write_wrap(mount, name, data, overwrite=False):
    """Caller holds USB management lock; create Wraps only after successful validation."""
    width, height = validate_wrap(data)
    folder = os.path.join(mount, "Wraps")
    if os.path.islink(folder):
        raise ValueError("Wraps 不可为符号链接")
    os.makedirs(folder, exist_ok=True)
    name = wrap_name(name)
    matches = [f for f in os.listdir(folder) if f.casefold() == name.casefold()]
    if matches:
        if not overwrite:
            raise FileExistsError("已存在同名涂装, 是否替换？")
        name = matches[0]  # exFAT is case insensitive; avoid duplicate entries on other hosts
    elif len([f for f in os.listdir(folder) if f.lower().endswith(".png")]) >= 10:
        raise ValueError("USB 最多 10 个涂装, 请先删除一个")
    target = os.path.join(folder, name)
    if os.path.islink(target) or os.path.isdir(target):
        raise ValueError("涂装目标路径无效")
    storage.atomic_write(target, data, mode=None)
    return name, width, height
