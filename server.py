import socket
import threading
import json
import struct
import os
import base64
import uuid

# ================= НАСТРОЙКИ =================
HOST = "0.0.0.0"
PORT = 11 # Порт для видео изображения
AUDIO_PORT = 22 # Аудио порт
ACTION_PORT = 33 # Порт для выполнения команд

# Пароли для входа в админ-панель
PASSWORD1 = "Admin1"
PASSWORD2 = "Admin2"

# Токены для подключения к клиентам, CLIENT и AUDIO должны совпадать по значению
CLIENT1 = "client1"
CLIENT2 = "client2"
CLIENT3 = "client3"
CLIENT4 = "client4"
CLIENT5 = "client5"

AUDIO1 = "client1"
AUDIO2 = "client2"
AUDIO3 = "client3"
AUDIO4 = "client4"
AUDIO5 = "client5"

MAX_PACKET = 256 * 1024
MAX_ACTION_PACKET = 96 * 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOUNDS_DIR = os.path.join(BASE_DIR, "sounds")
MEDIA_DIR = os.path.join(BASE_DIR, "media")

MEDIA_EXT = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".mp4", ".avi", ".mkv", ".mov",
    ".webm", ".wmv", ".flv", ".m4v",
}

TOKENS = {
    CLIENT1: "CLIENT1", CLIENT2: "CLIENT2", CLIENT3: "CLIENT3",
    CLIENT4: "CLIENT4", CLIENT5: "CLIENT5",
}
AUDIO_TOKENS = {
    AUDIO1: "AUDIO1", AUDIO2: "AUDIO2", AUDIO3: "AUDIO3",
    AUDIO4: "AUDIO4", AUDIO5: "AUDIO5",
}
PASSWORD = {PASSWORD1: "PASSWORD1", PASSWORD2: "PASSWORD2"}

# Состояние основного сервера
lock = threading.RLock()
admin_lock = threading.Lock()
clients = {}
admin = None

# Состояние аудио-сервера
audio_lock = threading.RLock()
audio_clients = None
audio_admin = None

# Состояние сервера действий
actions_lock = threading.RLock()
actions_clients = None
actions_admins = set()

# Pending запросы «админ → клиент → админ»
actions_pending = {}
actions_pending_lock = threading.Lock()


# ================= УТИЛИТЫ =================
def recv_exact(sock, count):
    data = bytearray()
    while len(data) < count:
        part = sock.recv(count - len(data))
        if not part:
            return None
        data.extend(part)
    return bytes(data)


def recv_line(sock, limit=2048):
    data = bytearray()
    while b"\n" not in data and len(data) < limit:
        part = sock.recv(1)
        if not part:
            return None
        data.extend(part)
    if b"\n" not in data:
        raise ValueError("Handshake line too long")
    return bytes(data).split(b"\n", 1)[0]


def send_json_line(sock, obj):
    sock.sendall(json.dumps(obj).encode("utf-8") + b"\n")


def send_packet(sock, kind, payload):
    sock.sendall(bytes([kind]) + struct.pack("!I", len(payload)) + payload)


def recv_packet(sock):
    header = recv_exact(sock, 5)
    if not header:
        return None, None
    kind = header[0]
    size = struct.unpack("!I", header[1:])[0]
    if size > 8 * 1024 * 1024:
        raise ValueError("Packet too large")
    payload = recv_exact(sock, size)
    return (kind, payload) if payload is not None else (None, None)


def _reply(conn, obj):
    try:
        data = json.dumps(obj).encode("utf-8")
        conn.sendall(struct.pack("!I", len(data)) + data)
    except OSError:
        pass


# ================= ОСНОВНОЙ СЕРВЕР =================
def update_client_list():
    with lock:
        ids = sorted(clients)
    data = json.dumps({"event": "clients", "clients": ids}).encode()
    with admin_lock:
        target = admin
        if target:
            try:
                send_packet(target, 1, data)
            except OSError:
                pass


def handle_main(conn, address):
    global admin
    role = client_id = None
    try:
        conn.settimeout(15)
        line = recv_line(conn)
        if not line:
            return
        hello = json.loads(line.decode().strip())
        role, token = hello.get("role"), hello.get("token")

        if role in ("clients", "sender"):
            client_id = TOKENS.get(token)
            if not client_id:
                send_json_line(conn, {"ok": False, "error": "Invalid client token"})
                return
            with lock:
                if client_id in clients:
                    send_json_line(conn, {"ok": False, "error": "Client already connected"})
                    return
                clients[client_id] = conn
        elif role in ("admin", "viewer", "receiver"):
            if token not in PASSWORD:
                send_json_line(conn, {"ok": False, "error": "Invalid admin password"})
                return
            with admin_lock:
                if admin is not None:
                    send_json_line(conn, {"ok": False, "error": "Admin already connected"})
                    return
                admin = conn
        else:
            send_json_line(conn, {"ok": False, "error": "Invalid role"})
            return

        conn.settimeout(None)
        send_json_line(conn, {"ok": True})
        print(f"[+] {role} подключен к основному серверу {address[0]}" +
              (f" ({client_id})" if client_id else ""))

        if role in ("admin", "viewer", "receiver"):
            update_client_list()
            while True:
                kind, payload = recv_packet(conn)
                if kind is None:
                    break
        else:
            update_client_list()
            while True:
                kind, payload = recv_packet(conn)
                if kind is None:
                    break
                if kind != 2 or not payload:
                    continue
                id_size = payload[0]
                if len(payload) <= 1 + id_size:
                    continue
                frame_id = payload[1:1 + id_size].decode("utf-8", "ignore")
                if frame_id != client_id:
                    continue
                with admin_lock:
                    target = admin
                    if target:
                        try:
                            send_packet(target, 2, payload)
                        except OSError:
                            pass
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[!] [Main] {address}: {exc}")
    finally:
        with lock:
            if role in ("clients", "sender") and client_id and clients.get(client_id) is conn:
                del clients[client_id]
        with admin_lock:
            if role in ("admin", "viewer", "receiver") and admin is conn:
                admin = None
        try:
            conn.close()
        except OSError:
            pass
        if role in ("clients", "sender"):
            update_client_list()
        print(f"[-] [Main] {role or 'unknown'} отключен от сервера")


# ================= АУДИО СЕРВЕР =================
def handle_audio(conn, addr):
    global audio_clients, audio_admin
    role = None
    slot = None
    stop_keepalive = threading.Event()
    try:
        conn.settimeout(10)
        line = recv_line(conn)
        if line is None:
            return
        hello = json.loads(line.decode("utf-8"))
        role, token = hello.get("role"), hello.get("token")

        if role in ("clients", "sender"):
            slot = "clients"
            authorized = token in AUDIO_TOKENS
        elif role in ("admin", "receiver"):
            slot = "admin"
            authorized = token in PASSWORD
        else:
            authorized = False

        if not authorized:
            send_json_line(conn, {"ok": False, "error": "Invalid role or token"})
            return

        with audio_lock:
            if slot == "clients":
                if audio_clients is not None:
                    send_json_line(conn, {"ok": False, "error": "Sender already connected"})
                    return
                audio_clients = conn
            else:
                if audio_admin is not None:
                    send_json_line(conn, {"ok": False, "error": "Receiver already connected"})
                    return
                audio_admin = conn

        send_json_line(conn, {"ok": True})
        conn.settimeout(None)
        print(f"[+] [Audio] {role} подключен с {addr[0]}:{addr[1]}")

        if slot == "clients":
            while True:
                header = recv_exact(conn, 4)
                if header is None:
                    break
                size = struct.unpack("!I", header)[0]
                if size <= 0 or size > MAX_PACKET:
                    raise ValueError(f"Invalid packet size: {size}")
                payload = recv_exact(conn, size)
                if payload is None:
                    break
                with audio_lock:
                    target = audio_admin
                if target is not None:
                    try:
                        target.sendall(header + payload)
                    except OSError:
                        pass
        else:
            silence = b"\x00" * 1280
            silence_pkt = struct.pack("!I", len(silence)) + silence

            def _keepalive():
                while not stop_keepalive.wait(0.2):
                    with audio_lock:
                        if audio_clients is not None:
                            continue
                    try:
                        conn.sendall(silence_pkt)
                    except OSError:
                        break

            threading.Thread(target=_keepalive, daemon=True).start()
            while conn.recv(4096):
                pass
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[!] [Audio] {addr}: {exc}")
    finally:
        stop_keepalive.set()
        with audio_lock:
            if slot == "clients" and audio_clients is conn:
                audio_clients = None
            if slot == "admin" and audio_admin is conn:
                audio_admin = None
        try:
            conn.close()
        except OSError:
            pass
        print(f"[-] [Audio] {role or 'unknown'} отключен")


# ================= ACTIONS СЕРВЕР =================
def _list_sound_files():
    try:
        if not os.path.isdir(SOUNDS_DIR):
            return []
        return sorted(
            f for f in os.listdir(SOUNDS_DIR)
            if f.lower().endswith((".mp3", ".wav"))
        )
    except OSError:
        return []


def _list_media_files():
    try:
        if not os.path.isdir(MEDIA_DIR):
            return []
        return sorted(
            f for f in os.listdir(MEDIA_DIR)
            if os.path.splitext(f.lower())[1] in MEDIA_EXT
        )
    except OSError:
        return []


def handle_actions(conn, addr):
    global actions_clients
    role = None
    slot = None
    try:
        conn.settimeout(10)
        line = recv_line(conn)
        if line is None:
            return
        hello = json.loads(line.decode("utf-8"))
        role, token = hello.get("role"), hello.get("token")

        if role in ("clients", "sender"):
            slot = "clients"
            authorized = token in TOKENS
        elif role in ("admin", "receiver"):
            slot = "admin"
            authorized = token in PASSWORD
        else:
            authorized = False

        if not authorized:
            send_json_line(conn, {"ok": False, "error": "Invalid role or token"})
            return

        with actions_lock:
            if slot == "clients":
                if actions_clients is not None:
                    send_json_line(conn, {"ok": False, "error": "Client already connected"})
                    return
                actions_clients = conn
            else:
                actions_admins.add(conn)

        send_json_line(conn, {"ok": True})
        conn.settimeout(None)
        print(f"[+] [Actions] {role} подключен с {addr[0]}:{addr[1]}")

        if slot == "admin":
            while True:
                header = recv_exact(conn, 4)
                if header is None:
                    break
                size = struct.unpack("!I", header)[0]
                if size <= 0 or size > MAX_ACTION_PACKET:
                    raise ValueError(f"Invalid packet size: {size}")
                payload = recv_exact(conn, size)
                if payload is None:
                    break
                try:
                    cmd = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue

                action = cmd.get("action")

                # ---------- список звуков ----------
                if action == "list_sounds":
                    _reply(conn, {"ok": True, "sounds": _list_sound_files()})
                    continue

                # ---------- воспроизвести mp3 на клиенте ----------
                if action == "play_sound":
                    safe = os.path.basename(str(cmd.get("value") or ""))
                    path = os.path.join(SOUNDS_DIR, safe)
                    if not safe or not os.path.isfile(path):
                        _reply(conn, {"ok": False, "error": "File not found"})
                        continue
                    try:
                        with open(path, "rb") as fp:
                            raw = fp.read()
                    except OSError as e:
                        _reply(conn, {"ok": False, "error": f"read: {e}"})
                        continue
                    if len(raw) > MAX_ACTION_PACKET * 3 // 4:
                        _reply(conn, {"ok": False, "error": "File too large"})
                        continue
                    fwd = json.dumps({
                        "action": "play_sound",
                        "value": safe,
                        "data": base64.b64encode(raw).decode("ascii"),
                    }).encode("utf-8")
                    with actions_lock:
                        target = actions_clients
                    if target is None:
                        _reply(conn, {"ok": False, "error": "Client not connected"})
                        continue
                    try:
                        target.sendall(struct.pack("!I", len(fwd)) + fwd)
                        _reply(conn, {"ok": True})
                    except OSError:
                        _reply(conn, {"ok": False, "error": "Client send failed"})
                    continue

                # ---------- список медиа ----------
                if action == "list_media":
                    _reply(conn, {"ok": True, "media": _list_media_files()})
                    continue

                # ---------- открыть медиа на клиенте ----------
                if action == "play_media":
                    value_obj = cmd.get("value") or {}
                    name = os.path.basename(str(value_obj.get("name") or ""))
                    fullscreen = bool(value_obj.get("fullscreen"))
                    path = os.path.join(MEDIA_DIR, name)
                    if not name or not os.path.isfile(path):
                        _reply(conn, {"ok": False, "error": "File not found"})
                        continue
                    try:
                        with open(path, "rb") as fp:
                            raw = fp.read()
                    except OSError as e:
                        _reply(conn, {"ok": False, "error": f"read: {e}"})
                        continue
                    if len(raw) > MAX_ACTION_PACKET * 3 // 4:
                        _reply(conn, {"ok": False, "error": "File too large"})
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    fwd = json.dumps({
                        "action": "open_media",
                        "value": name,
                        "ext": ext,
                        "fullscreen": fullscreen,
                        "data": base64.b64encode(raw).decode("ascii"),
                    }).encode("utf-8")
                    with actions_lock:
                        target = actions_clients
                    if target is None:
                        _reply(conn, {"ok": False, "error": "Client not connected"})
                        continue
                    try:
                        target.sendall(struct.pack("!I", len(fwd)) + fwd)
                        _reply(conn, {"ok": True})
                    except OSError:
                        _reply(conn, {"ok": False, "error": "Client send failed"})
                    continue

                # ---------- список процессов (запрос-ответ) ----------
                if action == "list_processes":
                    request_id = str(uuid.uuid4())
                    evt = threading.Event()
                    slot_obj = {"event": evt, "response": None}
                    with actions_pending_lock:
                        actions_pending[request_id] = slot_obj

                    fwd = json.dumps({
                        "action": "list_processes",
                        "request_id": request_id,
                    }).encode("utf-8")
                    with actions_lock:
                        target = actions_clients
                    if target is None:
                        with actions_pending_lock:
                            actions_pending.pop(request_id, None)
                        _reply(conn, {"ok": False, "error": "Client not connected"})
                        continue
                    try:
                        target.sendall(struct.pack("!I", len(fwd)) + fwd)
                    except OSError:
                        with actions_pending_lock:
                            actions_pending.pop(request_id, None)
                        _reply(conn, {"ok": False, "error": "Client send failed"})
                        continue

                    if evt.wait(15.0):
                        with actions_pending_lock:
                            result = actions_pending.pop(request_id, {}).get("response")
                        if result is not None:
                            _reply(conn, {"ok": True, "processes": result})
                        else:
                            _reply(conn, {"ok": False, "error": "Empty client response"})
                    else:
                        with actions_pending_lock:
                            actions_pending.pop(request_id, None)
                        _reply(conn, {"ok": False, "error": "Timeout waiting for client"})
                    continue

                # ---------- релей ПОЛНОГО cmd клиенту ----------
                # mute_mic / mute_system / reproduce_sound
                # mouse_move / mouse_click / mouse_wheel / key
                # machine_restart / machine_shutdown
                # kill_process / run_process
                fwd = json.dumps(cmd, separators=(",", ":")).encode("utf-8")
                with actions_lock:
                    target = actions_clients
                if target is not None:
                    try:
                        target.sendall(struct.pack("!I", len(fwd)) + fwd)
                    except OSError:
                        pass
        else:
            # Клиент в actions: принимает команды и отвечает на list_processes
            while True:
                header = recv_exact(conn, 4)
                if header is None:
                    break
                size = struct.unpack("!I", header)[0]
                if size <= 0 or size > MAX_ACTION_PACKET:
                    continue
                payload = recv_exact(conn, size)
                if payload is None:
                    break
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if msg.get("action") == "process_list_response":
                    req_id = msg.get("request_id")
                    if req_id:
                        with actions_pending_lock:
                            slot_obj = actions_pending.get(req_id)
                        if slot_obj is not None:
                            slot_obj["response"] = msg.get("processes", [])
                            slot_obj["event"].set()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[!] [Actions] {addr}: {exc}")
    finally:
        with actions_lock:
            if slot == "clients" and actions_clients is conn:
                actions_clients = None
            if slot == "admin":
                actions_admins.discard(conn)
        try:
            conn.close()
        except OSError:
            pass
        print(f"[-] [Actions] {role or 'unknown'} отключен")


# ================= ЗАПУСК =================
def run_main_server():
    print(f"Основной сервер запущен на {HOST}:{PORT}")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(20)
    try:
        while True:
            conn, address = server.accept()
            threading.Thread(target=handle_main, args=(conn, address), daemon=True).start()
    finally:
        server.close()


def run_audio_server():
    print(f"Аудио сервер запущен на {HOST}:{AUDIO_PORT}")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, AUDIO_PORT))
    server.listen(10)
    try:
        while True:
            conn, address = server.accept()
            threading.Thread(target=handle_audio, args=(conn, address), daemon=True).start()
    finally:
        server.close()


def run_actions_server():
    print(f"Сервер для действий запущен на {HOST}:{ACTION_PORT}")
    print(f"Папка звуков: {SOUNDS_DIR}")
    print(f"Папка медиа:  {MEDIA_DIR}")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, ACTION_PORT))
    server.listen(10)
    try:
        while True:
            conn, address = server.accept()
            threading.Thread(target=handle_actions, args=(conn, address), daemon=True).start()
    finally:
        server.close()


def main():
    threading.Thread(target=run_main_server, daemon=True).start()
    threading.Thread(target=run_audio_server, daemon=True).start()
    try:
        run_actions_server()
    except KeyboardInterrupt:
        print("\nОстановка сервера...")


if __name__ == "__main__":
    main()