import bencode
import hashlib
import urllib.parse
import urllib.request
import random
import string
import threading
import time
import socket 
import os
from logger import BitTorrentLogger 
import sys
import struct 

PEER_INDEX = sys.argv[1] if len(sys.argv) > 1 else "1"

def get_sequential_peer_id(index):
    prefix = '-AA0001-' 
    
    try:
        num_index = int(index)
    except ValueError:
        num_index = 1
        
    # تبدیل عدد به یک رشته ۱۲ کاراکتری که با صفر پر شده است
    # مثال برای عدد 1: 000000000001
    sequential_part = f"{num_index:012d}"
    
    return prefix + sequential_part

# تولید آیدی ثابت و ترتیبی بر اساس ورودی ترمینال
MY_PEER_ID = get_sequential_peer_id(PEER_INDEX)

WORKSPACE_DIR = f"workspace_{MY_PEER_ID}"
os.makedirs(WORKSPACE_DIR, exist_ok=True)
ACTIVE_TORRENTS = []
GLOBAL_KNOWN_PEERS = set()
ACTIVE_CONNECTIONS = {}
ACTIVE_DOWNLOADS = set()

peer_logger = BitTorrentLogger(f"peer_{MY_PEER_ID}.log", "peers")
PEER_PORT = 6881 

def start_peer_listener():
    global PEER_PORT

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    port = PEER_PORT
    flag = True
    while port <= 6889:
        try:
            server_socket.bind(('127.0.0.1', port))
            print(f"[+] Listener successfully started on port {port}")
            peer_logger.log_event("LISTENER_START", f"Peer listening for direct connections on port {port}")
            PEER_PORT = port
            flag = False
            break
        except OSError:
            print(f"[-] Port {port} is busy, trying next...")
            peer_logger.log_event("PORT_BUSY", f"Port {port} in use. Trying next...")
            port += 1
    
    if (flag):
        print("[-] All Ports are in use!")
        peer_logger.log_event("ERROR", "Failed to start listener. All ports in use.")    
        return
        
    server_socket.listen(5)

    while True:
        try:
            client_socket, addr = server_socket.accept()
            data = client_socket.recv(68)
            
            if not data:
                client_socket.close()
                continue

            if data == b"PING":
                peer_logger.log_event("PING_RECV", f"Received PING from {addr}")
                client_socket.send("PONG".encode('utf-8'))
                client_socket.close()

            elif len(data) >= 20 and data[0] == 19 and data[1:20] == b"BitTorrent protocol":
                received_info_hash = data[28:48]
                received_peer_id = data[48:68].decode('utf-8', errors='ignore')
                
                print(f"      [+] INCOMING HANDSHAKE: Valid connection from {received_peer_id} at {addr}")
                peer_logger.log_event("HANDSHAKE_RECV", f"Handshake verified from {received_peer_id}")
                
                # در جواب، ما هم باید هندشیک خودمون رو بفرستیم
                my_handshake = create_handshake(received_info_hash, MY_PEER_ID)
                client_socket.send(my_handshake)
                
                torrent_name = "Incoming_Torrent" # برای لاگ
                threading.Thread(target=handle_connection, args=(client_socket, addr[0], addr[1], torrent_name, received_info_hash), daemon=True).start()
            else:
                client_socket.close()
                
        except Exception as e:
            peer_logger.log_event("ERROR", f"Listener error: {e}")

def ping_other_peer(ip, port, torrent_name  ):
    try:
        peer_logger.log_event("PING_SEND", f"[{torrent_name}] Attempting to ping peer at {ip}:{port}")        

        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.settimeout(2)
        server_socket.connect((ip, port))
        
        start_time = time.time()

        server_socket.send("PING".encode('utf-8'))
        
        response = server_socket.recv(1024).decode('utf-8')
        if response == "PONG":
            
            end_time = time.time()
            rtt_ms = (end_time - start_time) * 1000

            print(f"      [+] DIRECT CONNECTION SUCCESS: Received PONG from {ip}:{port} with ping {rtt_ms:.2f}")
            peer_logger.log_event("PING_SUCCESS", f"[{torrent_name}] Received PONG from {ip}:{port} | RTT: {rtt_ms:.2f} ms")

            try:
                # فرض می‌کنیم آی‌پی خودمون 127.0.0.1 هست 
                telemetry_url = f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={ip}:{port}&type=PING"
                urllib.request.urlopen(telemetry_url)
            except Exception as e:
                pass

        server_socket.close()
    except Exception as e:
        peer_logger.log_event("PING_FAIL", f"[{torrent_name}] Failed to ping {ip}:{port} - {e}")

def handle_connection(client_socket, peer_ip, peer_port, torrent_name, info_hash):
    print(f"      [*] Starting connection handler for {peer_ip}:{peer_port}")
    ACTIVE_CONNECTIONS[(peer_ip, peer_port)] = client_socket
    
    # متغیرهای وضعیت (State Machine)
    am_choking = True
    am_interested = False
    peer_choking = True
    peer_interested = False
    
    is_seeder = False
    info_dict = None
    real_torrent_name = torrent_name

    for t in ACTIVE_TORRENTS:
        if t['info_hash'] == info_hash:
            if int(t['left']) == 0:
                is_seeder = True
            info_dict = t.get('info_dict')
            if info_dict and 'name' in info_dict:
                real_torrent_name = info_dict['name']
            break

    if info_dict is None:
        print(f"      [-] Error: info_dict not found for hash {info_hash.hex()}")
        client_socket.close()
        return

    # --- محاسبه تعداد و سایز قطعات ---
    piece_length = info_dict.get('piece length', 262144) # معمولاً 256KB
    total_length = 0
    if 'files' in info_dict:
        total_length = sum(f['length'] for f in info_dict['files'])
    else:
        total_length = info_dict.get('length', 0)
        
    # فرمول محاسبه سقف تقسیم (بدون نیاز به کتابخانه math)
    num_pieces = (total_length + piece_length - 1) // piece_length
    
    downloaded_buffer = {} # بافر برای نگهداری قطعات دریافت‌شده
    current_piece_index = 0 # نشانگر قطعه فعلی برای دانلود

    # 1. ارسال Bitfield
    bitfield_payload = b'\xff' if is_seeder else b'\x00'
    msg_len = 1 + len(bitfield_payload)
    bitfield_msg = struct.pack(f'>IB{len(bitfield_payload)}s', msg_len, 5, bitfield_payload)
    
    try:
        client_socket.send(bitfield_msg)
        print(f"      [>] Sent BITFIELD (Seeder: {is_seeder}) to {peer_ip}:{peer_port}")
        
        # 2. اگر فایل را نداریم (Leecher)، اول چک می‌کنیم که آیا قبلاً درخواست ندادیم؟
        if not is_seeder:
            if info_hash in ACTIVE_DOWNLOADS:
                print(f"      [*] Already downloading this torrent. Skipping duplicate request to {peer_ip}:{peer_port}")
            else:
                ACTIVE_DOWNLOADS.add(info_hash)
                interested_msg = struct.pack('>IB', 1, 2)
                client_socket.send(interested_msg)
                am_interested = True
                print(f"      [>] Sent INTERESTED to {peer_ip}:{peer_port}")
                peer_logger.log_event("STATE_CHANGE", f"Sent INTERESTED to {peer_ip}:{peer_port}")

        # حلقه گوش دادن به پیام‌ها
        while True:
            length_prefix = client_socket.recv(4)
            if not length_prefix or len(length_prefix) < 4:
                break
                
            incoming_msg_len = struct.unpack('>I', length_prefix)[0]
            if incoming_msg_len == 0:
                continue # Keep-Alive
                
            msg_id_byte = client_socket.recv(1)
            incoming_msg_id = struct.unpack('>B', msg_id_byte)[0]
            
            payload_len = incoming_msg_len - 1
            payload = client_socket.recv(payload_len) if payload_len > 0 else b''
            
            # 3. پردازش پیام‌های دریافتی بر اساس ID
            # 3. پردازش پیام‌های دریافتی بر اساس ID
            if incoming_msg_id == 5:
                print(f"      [<] Received BITFIELD from {peer_ip}:{peer_port}")
                
            elif incoming_msg_id == 2: # Interested
                print(f"      [<] Received INTERESTED from {peer_ip}:{peer_port}")
                if is_seeder:
                    unchoke_msg = struct.pack('>IB', 1, 1)
                    client_socket.send(unchoke_msg)
                    print(f"      [>] Sent UNCHOKE to {peer_ip}:{peer_port}")
                    
            elif incoming_msg_id == 1: # Unchoke (آماده دانلود)
                print(f"      [<] Received UNCHOKE from {peer_ip}:{peer_port}. Requesting data...")
                
                # پیدا کردن سایز فایل برای درخواست
                file_size = 0
                for t in ACTIVE_TORRENTS:
                    if t['info_hash'] == info_hash:
                        file_size = int(t['left'])
                        break
                
                # برای سادگی شبیه‌سازی، کل فایل را در یک قطعه درخواست می‌کنیم
                # طول پیام: 13 بایت | آیدی: 6 | ایندکس: 0 | شروع: 0 | طول داده: file_size
                req_len = piece_length if (0 < num_pieces - 1) else (total_length - (0 * piece_length))

                req_msg = struct.pack('>IBIII', 13, 6, 0, 0, req_len)
                client_socket.send(req_msg)
                print(f"      [>] Sent REQUEST for piece 0 (size: {req_len} bytes)")
                try:
                    # گزارش ارسال ریکوئست زرد رنگ
                    urllib.request.urlopen(f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={peer_ip}:{peer_port}&type=REQ")
                except:
                    pass

            elif incoming_msg_id == 4: # HAVE (یک کلاینت دیگر قطعه جدیدی دریافت کرده است)
                if payload_len == 4:
                    received_piece_idx = struct.unpack('>I', payload)[0]
                    print(f"      [<] Received HAVE from {peer_ip}:{peer_port} for piece {received_piece_idx}")
                    # در یک کلاینت واقعی، اینجا آرایه Bitfield مربوط به این Peer در رم آپدیت می‌شود
                    # تا در آینده بدانیم می‌توانیم این قطعه را هم از او درخواست کنیم.
                
            elif incoming_msg_id == 6: # Request (سیدر این را دریافت می‌کند)
                if payload_len == 12:
                    piece_index, begin_offset, req_length = struct.unpack('>III', payload)
                    print(f"      [<] Received REQUEST from {peer_ip}:{peer_port} (Index: {piece_index}, Length: {req_length})")
                    
                    file_data = b""
                    try:
                        if 'files' in info_dict:
                            # خواندن تمام فایل‌ها و چسباندن بایت‌های آن‌ها به یکدیگر
                            for f in info_dict['files']:
                                f_path = os.path.join(WORKSPACE_DIR, real_torrent_name, *f['path'])
                                with open(f_path, 'rb') as file_obj:
                                    file_data += file_obj.read()
                        else:
                            file_path = os.path.join(WORKSPACE_DIR, real_torrent_name)
                            with open(file_path, 'rb') as file_obj:
                                file_data = file_obj.read()
                            
                        # --- جداسازی دقیق قطعه درخواستی ---
                        absolute_offset = (piece_index * piece_length) + begin_offset
                        piece_data = file_data[absolute_offset : absolute_offset + req_length]
                        
                        piece_msg_len = 9 + len(piece_data)
                        piece_header = struct.pack('>IBII', piece_msg_len, 7, piece_index, begin_offset)
                        
                        client_socket.send(piece_header + piece_data)
                        print(f"      [>] Sent PIECE {piece_index} to {peer_ip}:{peer_port} ({len(piece_data)} bytes)")
                        try:
                            urllib.request.urlopen(f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={peer_ip}:{peer_port}&type=PIECE")
                        except:
                            pass
                    except Exception as e:
                        print(f"      [-] File read error: {e}")
                        
            elif incoming_msg_id == 7: # Piece (لیچر فایل را دریافت می‌کند)
                piece_index, begin_offset = struct.unpack('>II', payload[:8])
                raw_piece_data = payload[8:]
                
                print(f"      [<] Received PIECE {piece_index} from {peer_ip}:{peer_port} ({len(raw_piece_data)} bytes)")
                
                # ---> 1. استخراج هش اصلی این قطعه از فایل تورنت (هر قطعه 20 بایت است) <---
                expected_hash = info_dict['pieces'][piece_index * 20 : (piece_index + 1) * 20]
                
                # ---> 2. محاسبه هش SHA-1 برای دیتای دریافت شده <---
                import hashlib
                actual_hash = hashlib.sha1(raw_piece_data).digest()
                
                # ---> 3. مقایسه هش‌ها <---
                if expected_hash != actual_hash:
                    print(f"      [-] HASH FAILED for piece {piece_index}! Data is corrupted. Dropping piece.")
                    # اگر کلاینت واقعی بودیم، دوباره Request می‌فرستادیم. اما اینجا فعلاً قطعه را دور می‌اندازیم.
                    continue
                else:
                    print(f"      [+] HASH VERIFIED for piece {piece_index}.")
                    
                    # 4. ذخیره در صورت صحت کامل دیتای باینری
                    downloaded_buffer[piece_index] = raw_piece_data
                    current_piece_index += 1
                    
                    # ---> اضافه کردن بخش ارسال پیام HAVE <---
                    # طول پیام 5 بایت (1 بایت آیدی + 4 بایت ایندکس قطعه)
                    have_msg = struct.pack('>IBI', 5, 4, piece_index)
                    
                    # ما باید این پیام رو به همه کسانی که بهشون وصل هستیم بفرستیم
                    # (در اینجا برای شبیه‌سازی، روی سوکت‌های فعال ارسال می‌کنیم)
                    for (p_ip, p_port), sock in list(ACTIVE_CONNECTIONS.items()):
                        try:
                            sock.send(have_msg)
                            print(f"      [>] Broadcasted HAVE (Piece {piece_index}) to {p_ip}:{p_port}")
                        except:
                            pass

                    # بررسی ادامه دانلود یا اتمام کار
                    if current_piece_index < num_pieces:
                        req_len = piece_length if (current_piece_index < num_pieces - 1) else (total_length - (current_piece_index * piece_length))
                        req_msg = struct.pack('>IBIII', 13, 6, current_piece_index, 0, req_len)
                        client_socket.send(req_msg)
                        print(f"      [>] Sent REQUEST for piece {current_piece_index} (size: {req_len} bytes)")
                        try:
                            urllib.request.urlopen(f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={peer_ip}:{peer_port}&type=REQ")
                        except:
                            pass
                    else:
                        print("      [*] All pieces received! Reconstructing file(s)...")
                        
                        full_data = b"".join(downloaded_buffer[i] for i in range(num_pieces))
                        try:
                            if 'files' in info_dict:
                                current_offset = 0
                                for f in info_dict['files']:
                                    f_len = f['length']
                                    f_data = full_data[current_offset : current_offset + f_len]
                                    current_offset += f_len
                                    
                                    f_path = os.path.join(WORKSPACE_DIR, real_torrent_name, *f['path'])
                                    os.makedirs(os.path.dirname(f_path), exist_ok=True)
                                    with open(f_path, 'wb') as file_obj:
                                        file_obj.write(f_data)
                                print(f"      [+] MULTI-FILE TRANSFER COMPLETE! Saved in '{real_torrent_name}/'")
                            else:
                                file_path = os.path.join(WORKSPACE_DIR, real_torrent_name)
                                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                                with open(file_path, 'wb') as file_obj:
                                    file_obj.write(full_data)
                                print(f"      [+] FILE TRANSFER COMPLETE! Saved to {file_path}")
                                
                            print(f"      [+] Status Upgrade: I am now a SEEDER for this torrent!")
                            for t in ACTIVE_TORRENTS:
                                if t['info_hash'] == info_hash:
                                    t['left'] = 0
                                    break
                            if info_hash in ACTIVE_DOWNLOADS:
                                ACTIVE_DOWNLOADS.remove(info_hash)
                        except Exception as e:
                            print(f"      [-] File write error: {e}")
                
            else:
                print(f"      [<] Received unknown MSG ID: {incoming_msg_id}")
                
    except Exception as e:
        print(f"      [-] Connection dropped with {peer_ip}:{peer_port}: {e}")
    finally:
        client_socket.close()
        if (peer_ip, peer_port) in ACTIVE_CONNECTIONS:
            del ACTIVE_CONNECTIONS[(peer_ip, peer_port)]

def create_handshake(info_hash, peer_id):
    """ساخت پیام 68 بایتی استاندارد هندشیک بیت‌تورنت"""
    pstr = b"BitTorrent protocol"
    pstr_len = bytes([len(pstr)]) # بایت اول برابر 19
    reserved = b'\x00' * 8        # 8 بایت رزرو شده
    
    if isinstance(peer_id, str):
        peer_id_bytes = peer_id.encode('utf-8')
    else:
        peer_id_bytes = peer_id
        
    return pstr_len + pstr + reserved + info_hash + peer_id_bytes

def initiate_handshake(ip, port, info_hash, torrent_name):
    """ارسال هندشیک به Peer جدید برای شروع انتقال فایل"""
    try:
        peer_logger.log_event("HANDSHAKE_SEND", f"[{torrent_name}] Initiating handshake with {ip}:{port}")        

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect((ip, port))
        
        handshake_msg = create_handshake(info_hash, MY_PEER_ID)
        s.send(handshake_msg)
        
        response = s.recv(68)
        if len(response) == 68 and response[0] == 19 and response[1:20] == b"BitTorrent protocol":
            remote_peer_id = response[48:68].decode('utf-8', errors='ignore')
            print(f"      [+] HANDSHAKE SUCCESS: Connected to {remote_peer_id} for {torrent_name}")
            peer_logger.log_event("HANDSHAKE_SUCCESS", f"[{torrent_name}] Handshake successful with {remote_peer_id}")
            
            # اینجا بعداً سوکت رو باز نگه می‌داریم برای ارسال Bitfield
            s.settimeout(None)

            # --- تغییر جدید: سوکت رو نمی‌بندیم و میدیمش به Thread ---
            threading.Thread(target=handle_connection, args=(s, ip, port, torrent_name, info_hash), daemon=True).start()
            return # خروج از تابع تا سوکت بسته نشه
        
        s.close() # فعلا بعد از هندشیک می‌بندیم تا تو فاز بعدی مدیریت سوکت‌ها رو اضافه کنیم
    except Exception as e:
        peer_logger.log_event("HANDSHAKE_FAIL", f"[{torrent_name}] Failed handshake with {ip}:{port} - {e}")



def process_torrent(torrent_path):
    peer_id = MY_PEER_ID
    print(f"\n[*] [Thread {threading.current_thread().name}] Parsing: {torrent_path} with ID {peer_id}")
    
    try:
        with open(torrent_path, 'rb') as f:
            torrent_data = bencode.decode(f.read())
        
        announce_url = torrent_data['announce']
        info_dict = torrent_data['info']
        bencoded_info = bencode.encode(info_dict)
        info_hash = hashlib.sha1(bencoded_info).digest()
        torrent_name = info_dict['name']
        
        # --- منطق تشخیص Seeder و Leecher ---
        total_length = 0
        is_seeder = True
        
        if 'files' in info_dict:
            # حالت چند فایلی (Multi-file)
            root_dir = info_dict['name']
            for f in info_dict['files']:
                total_length += f['length']
                # ساخت مسیر دقیق فایل داخل پوشه اختصاصی این Peer
                file_path = os.path.join(WORKSPACE_DIR, root_dir, *f['path'])
                
                # اگر فایل نبود یا سایزش فرق داشت، پس ما Leecher هستیم
                if not os.path.exists(file_path) or os.path.getsize(file_path) != f['length']:
                    is_seeder = False
        else:
            # حالت تک فایلی (Single-file)
            total_length = info_dict['length']
            file_path = os.path.join(WORKSPACE_DIR, torrent_name)
            
            if not os.path.exists(file_path) or os.path.getsize(file_path) != total_length:
                is_seeder = False

        ############print(50 * "*")
        ############print(is_seeder)
        # تعیین مقدار left بر اساس وضعیت فایل‌ها
        left_bytes = 0 if is_seeder else total_length
        
        status_text = "SEEDER" if is_seeder else "LEECHER"
        print(f"      [*] Status for '{torrent_name}': {status_text} (Left: {left_bytes} bytes)")
        peer_logger.log_event("FILE_CHECK", f"Determined status as {status_text} for {torrent_name}")
        # ------------------------------------

        global ACTIVE_TORRENTS
        ACTIVE_TORRENTS.append({
            'announce_url': announce_url,
            'info_hash': info_hash,
            'left': left_bytes,
            'info_dict': info_dict
        })

        known_peers = set()

        params = {
            'info_hash': info_hash,
            'peer_id': peer_id,
            'port': PEER_PORT, 
            'uploaded': 0,
            'downloaded': 0,
            'left': left_bytes,
            'event': 'started' 
        }
        
        url_params = urllib.parse.urlencode(params)
        request_url = f"{announce_url}?{url_params}"
        
        response = urllib.request.urlopen(request_url)
        tracker_response = bencode.decode(response.read())
        
        print(f"\n[+] [Thread {threading.current_thread().name}] Received peers for {info_dict['name']}")
        peers_list = tracker_response.get('peers', [])
        
        if peers_list:
            for p in peers_list:
                target_ip = p['ip']
                target_port = p['port']

                if target_port == PEER_PORT:
                    continue
                
                known_peers.add((target_ip, target_port))
                GLOBAL_KNOWN_PEERS.add((target_ip, target_port))
                initiate_handshake(target_ip, target_port, info_hash, torrent_name)
        else:
            print("      [-] No other peers in the swarm yet.")

        interval = tracker_response.get('interval', 30)

        while True:
            time.sleep(interval)
            peer_logger.log_event("ANNOUNCE", f"[{info_dict['name']}] Sending periodic update to tracker...")
            
            update_params = {
                'info_hash': info_hash,
                'peer_id': peer_id,
                'port': PEER_PORT, 
                'uploaded': 0,
                'downloaded': 0,
                'left': total_length
            }
            
            update_url = f"{announce_url}?{urllib.parse.urlencode(update_params)}"
            try:
                update_response = urllib.request.urlopen(update_url)
                update_tracker_data = bencode.decode(update_response.read())

                new_peers_list = update_tracker_data.get('peers', [])
                for p in new_peers_list:
                    target_ip = p['ip']
                    target_port = p['port']
                    
                    if target_port == PEER_PORT:
                        continue
                        
                    if (target_ip, target_port) not in known_peers:
                        print(f"      [+] NEW PEER DISCOVERED for {torrent_name}: {target_ip}:{target_port}")
                        peer_logger.log_event("NEW_PEER", f"[{torrent_name}] Discovered new peer {target_ip}:{target_port} from tracker update")
                        
                        known_peers.add((target_ip, target_port))

                        GLOBAL_KNOWN_PEERS.add((target_ip, target_port))

                        initiate_handshake(target_ip, target_port, info_hash, torrent_name)

            except Exception as e:
                peer_logger.log_event("ERROR", f"[{torrent_name}] Periodic announce failed: {e}")
            
    except Exception as e:
        print(f"[-] [Thread {threading.current_thread().name}] Error: {e}")
        peer_logger.log_event("ERROR", f"Torrent process error: {e}")

def start_client():
    # ۱. شروع لیسنر در پس‌زمینه (برای دریافت اتصالات)
    listener_thread = threading.Thread(target=start_peer_listener, daemon=True)
    listener_thread.start()
    time.sleep(1)

    # ۲. اجرای هوشمند تورنت‌ها (فقط Seed شدن اتوماتیک فایل‌های موجود)
    torrent_files = [
        r"Torrent_Handler\my_real_torrent_1.txt",
        r"Torrent_Handler\my_real_torrent_2.txt"
    ]
    print("\n[*] Scanning for existing files to seed...")
    for i, file_path in enumerate(torrent_files):
        if os.path.exists(file_path):
            import bencode
            with open(file_path, 'rb') as f:
                torrent_data = bencode.decode(f.read())
            info_dict = torrent_data['info']
            torrent_name = info_dict['name']

            # بررسی اینکه آیا فایل‌ها روی هارد وجود دارند (Seeder هستیم یا نه)
            is_seeder = True
            if 'files' in info_dict:
                for f in info_dict['files']:
                    f_path = os.path.join(WORKSPACE_DIR, info_dict['name'], *f['path'])
                    if not os.path.exists(f_path) or os.path.getsize(f_path) != f['length']:
                        is_seeder = False
                        break
            else:
                f_path = os.path.join(WORKSPACE_DIR, torrent_name)
                if not os.path.exists(f_path) or os.path.getsize(f_path) != info_dict['length']:
                    is_seeder = False

            if is_seeder:
                print(f"[*] Found complete data for '{torrent_name}'. Auto-starting as SEEDER.")
                t = threading.Thread(target=process_torrent, args=(file_path,), name=f"Torrent-{i+1}", daemon=True)
                t.start()
                time.sleep(1)
            else:
                print(f"[*] Missing data for '{torrent_name}'. Skipped. Use 'download {file_path}' to start.")

    # ۳. شروع حلقه Heartbeat در پس‌زمینه
    def heartbeat_loop():
        while True:
            time.sleep(30)
            if GLOBAL_KNOWN_PEERS:
                print("\n[*] --- Initiating Periodic Heartbeat ---")
                peer_logger.log_event("HEARTBEAT_SYNC", "Periodic ping")
                for peer_ip, peer_port in list(GLOBAL_KNOWN_PEERS):
                    ping_other_peer(peer_ip, peer_port, "Heartbeat")
                    
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    # ۴. محیط کامند لاین برای کنترل دستی
    print("\n[+] Peer is ready! Background tasks (Listener, Pings, Seeding) are running.")
    print("    Commands:")
    print("    download <path_to_torrent_file>  -> Add a new torrent")
    print("    exit                             -> Stop the peer")

    try:
        while True:
            cmd = input("\npeer> ").strip()
            if cmd == "exit":
                break
            elif cmd.startswith("download"):
                parts = cmd.split(maxsplit=1)
                if len(parts) > 1:
                    # حذف کوتیشن‌های احتمالی در ابتدا و انتهای آدرس
                    torrent_path = parts[1].strip('"').strip("'") 
                    
                    if os.path.exists(torrent_path):
                        print(f"[*] Adding new torrent: {torrent_path}")
                        t = threading.Thread(target=process_torrent, args=(torrent_path,), daemon=True)
                        t.start()
                    else:
                        # نشان دادن مسیر مطلقی که پایتون در حال جستجوی آن است
                        abs_path = os.path.abspath(torrent_path)
                        print(f"[-] Error: Torrent file not found!")
                        print(f"    Python is exactly looking here: {abs_path}")
                else:
                    print("[-] Usage: download <path_to_torrent_file>")
    except KeyboardInterrupt:
        pass
        
    print("\n[*] Shutting down peer. Notifying tracker...")
    peer_logger.log_event("SHUTDOWN_INIT", "Keyboard interrupt received. Deregistering from tracker.")

    for torrent in ACTIVE_TORRENTS:
        stop_params = {
            'info_hash': torrent['info_hash'],
            'peer_id': MY_PEER_ID,
            'port': PEER_PORT,
            'uploaded': 0,
            'downloaded': 0,
            'left': torrent['left'],
            'event': 'stopped'
        }
        stop_url = f"{torrent['announce_url']}?{urllib.parse.urlencode(stop_params)}"
        try:
            urllib.request.urlopen(stop_url)
        except Exception as e:
            pass
            
    print("[+] Successfully unregistered from tracker. Goodbye!")

if __name__ == '__main__':
    start_client()