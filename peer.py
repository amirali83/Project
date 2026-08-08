import bencode
import hashlib
import urllib.parse
import urllib.request
import threading
import time
import socket 
import os
from logger import BitTorrentLogger 
import sys
import struct 

### read terminal to see what has been started
PEER_INDEX = sys.argv[1] if len(sys.argv) > 1 else "1"

def get_sequential_peer_id(index):
    prefix = '-AA0001-' 
    
    try:
        num_index = int(index)
    except ValueError:
        num_index = 1
        
    sequential_part = f"{num_index:012d}"
    
    return prefix + sequential_part

### create ID
MY_PEER_ID = get_sequential_peer_id(PEER_INDEX)

### Create Work Space
WORKSPACE_DIR = f"workspace_{MY_PEER_ID}"
os.makedirs(WORKSPACE_DIR, exist_ok=True)
os.makedirs(os.path.join(WORKSPACE_DIR, "Transferring_These_Files"), exist_ok=True)
ACTIVE_TORRENTS = []
GLOBAL_KNOWN_PEERS = set()
ACTIVE_CONNECTIONS = {}
ACTIVE_DOWNLOADS = set()

peer_logger = BitTorrentLogger(f"peer_{MY_PEER_ID}.log", "peers")
PEER_PORT = 6881 

def recv_all(sock, n):
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            break
        data.extend(packet)
    return bytes(data)

def start_peer_listener():
    global PEER_PORT

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    ### Find empty socket 
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
            ### when another peer connects 
            client_socket, addr = server_socket.accept()
            ### reads the first 68 byte (the original handshake protocol that we tried to implement)
            data = client_socket.recv(68)
            
            if not data:
                client_socket.close()
                continue
            
            ### if the data type is ping
            if data == b"PING":
                peer_logger.log_event("PING_RECV", f"Received PING from {addr}")
                client_socket.send("PONG".encode('utf-8'))
                client_socket.close()

            ### if the data type is handshake
            elif len(data) >= 20 and data[0] == 19 and data[1:20] == b"BitTorrent protocol":
                received_info_hash = data[28:48]
                received_peer_id = data[48:68].decode('utf-8', errors='ignore')
                
                print(f"      [+] INCOMING HANDSHAKE: Valid connection from {received_peer_id} at {addr}")
                peer_logger.log_event("HANDSHAKE_RECV", f"Handshake verified from {received_peer_id}")
                
                ### send our own handshake 
                my_handshake = create_handshake(received_info_hash, MY_PEER_ID)
                client_socket.send(my_handshake)
                
                ### create new thread
                torrent_name = "Incoming_Torrent"
                threading.Thread(target=handle_connection, args=(client_socket, addr[0], addr[1], torrent_name, received_info_hash), daemon=True).start()
            else:
                client_socket.close()
                
        except Exception as e:
            peer_logger.log_event("ERROR", f"Listener error: {e}")

def ping_other_peer(ip, port, torrent_name):
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
                telemetry_url = f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={ip}:{port}&type=PING"
                urllib.request.urlopen(telemetry_url)
            except Exception as e:
                pass

        server_socket.close()
    except Exception as e:
        peer_logger.log_event("PING_FAIL", f"[{torrent_name}] Failed to ping {ip}:{port} - {e}")

def handle_connection(client_socket, peer_ip, peer_port, torrent_name, info_hash):
    print(f"      [*] Starting connection handler for {peer_ip}:{peer_port}")
    ### add connection to the active ones
    ACTIVE_CONNECTIONS[(peer_ip, peer_port)] = client_socket
    
    am_choking = True
    am_interested = False
    peer_choking = True
    peer_interested = False
    
    is_seeder = False
    info_dict = None
    real_torrent_name = torrent_name

    ### check and see if we are seeder or leeacher
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

    ### calculating number of pieces
    piece_length = info_dict.get('piece length', 262144) 
    total_length = 0
    if 'files' in info_dict:
        total_length = sum(f['length'] for f in info_dict['files'])
    else:
        total_length = info_dict.get('length', 0)
        
    num_pieces = (total_length + piece_length - 1) // piece_length
    
    downloaded_buffer = {}
    current_piece_index = 0

    ### send bitfield to say what pieces we have
    ### bit 1 -> we have it, 0 -> we do not have it 
    bitfield_payload = b'\xff' if is_seeder else b'\x00'
    msg_len = 1 + len(bitfield_payload)
    bitfield_msg = struct.pack(f'>IB{len(bitfield_payload)}s', msg_len, 5, bitfield_payload)
    
    try:
        client_socket.send(bitfield_msg)
        print(f"      [>] Sent BITFIELD (Seeder: {is_seeder}) to {peer_ip}:{peer_port}")
        
        ### If we are leecher
        if not is_seeder:
            ### check see if we have started downloading
            if info_hash in ACTIVE_DOWNLOADS:
                print(f"      [*] Already downloading this torrent. Skipping duplicate request to {peer_ip}:{peer_port}")
            else:
                ACTIVE_DOWNLOADS.add(info_hash)
                interested_msg = struct.pack('>IB', 1, 2)
                client_socket.send(interested_msg)
                am_interested = True
                print(f"      [>] Sent INTERESTED to {peer_ip}:{peer_port}")
                peer_logger.log_event("STATE_CHANGE", f"Sent INTERESTED to {peer_ip}:{peer_port}")

        ### listen to all messages
        while True:
            ### first read the first 4 bytes (lenght)
            length_prefix = recv_all(client_socket, 4)
            if not length_prefix or len(length_prefix) < 4:
                break
                
            ### if message is empty
            incoming_msg_len = struct.unpack('>I', length_prefix)[0]
            if incoming_msg_len == 0:
                continue

            ### read another byte (message type)
            msg_id_byte = client_socket.recv(1)
            incoming_msg_id = struct.unpack('>B', msg_id_byte)[0]
            
            ### read the rest (pay load)
            payload_len = incoming_msg_len - 1
            payload = recv_all(client_socket, payload_len) if payload_len > 0 else b''
            
            ### type of messages 
            ### id = 1 -> unchoke     , peer has let us download
            ### id = 2 -> interested  , peer wants to download from us 
            ### id = 4 -> have        , peer has a new piece
            ### id = 5 -> bitfield    , get peers bitfield
            ### id = 6 -> request     , upload piece for peer
            ### id = 7 -> piece       , download from peer

            if incoming_msg_id == 5:
                print(f"      [<] Received BITFIELD from {peer_ip}:{peer_port}")
                
            elif incoming_msg_id == 2:
                print(f"      [<] Received INTERESTED from {peer_ip}:{peer_port}")
                if is_seeder:
                    unchoke_msg = struct.pack('>IB', 1, 1)
                    client_socket.send(unchoke_msg)
                    print(f"      [>] Sent UNCHOKE to {peer_ip}:{peer_port}")
                    
            elif incoming_msg_id == 1:
                print(f"      [<] Received UNCHOKE from {peer_ip}:{peer_port}. Requesting data...")
                
                ### get piece 
                req_len = piece_length if (0 < num_pieces - 1) else (total_length - (0 * piece_length))

                req_msg = struct.pack('>IBIII', 13, 6, 0, 0, req_len)
                client_socket.send(req_msg)
                print(f"      [>] Sent REQUEST for piece 0 (size: {req_len} bytes)")
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={peer_ip}:{peer_port}&type=REQ")
                except:
                    pass

            elif incoming_msg_id == 4:
                if payload_len == 4:
                    received_piece_idx = struct.unpack('>I', payload)[0]
                    print(f"      [<] Received HAVE from {peer_ip}:{peer_port} for piece {received_piece_idx}")
                    ### should be updated in parallel download
                
            elif incoming_msg_id == 6:
                if payload_len == 12:
                    piece_index, begin_offset, req_length = struct.unpack('>III', payload)
                    print(f"      [<] Received REQUEST from {peer_ip}:{peer_port} (Index: {piece_index}, Length: {req_length})")
                    
                    file_data = b""
                    try:
                        ### make file data in binary
                        if 'files' in info_dict:
                            for f in info_dict['files']:
                                f_path = os.path.join(WORKSPACE_DIR, real_torrent_name, *f['path'])
                                with open(f_path, 'rb') as file_obj:
                                    file_data += file_obj.read()
                        else:
                            file_path = os.path.join(WORKSPACE_DIR, real_torrent_name)
                            with open(file_path, 'rb') as file_obj:
                                file_data = file_obj.read()
                            
                        ### choose the part we should send
                        absolute_offset = (piece_index * piece_length) + begin_offset
                        piece_data = file_data[absolute_offset : absolute_offset + req_length]
                        
                        piece_msg_len = 9 + len(piece_data)
                        piece_header = struct.pack('>IBII', piece_msg_len, 7, piece_index, begin_offset)
                        
                        client_socket.sendall(piece_header + piece_data)
                        print(f"      [>] Sent PIECE {piece_index} to {peer_ip}:{peer_port} ({len(piece_data)} bytes)")
                        try:
                            urllib.request.urlopen(f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={peer_ip}:{peer_port}&type=PIECE")
                        except:
                            pass
                    except Exception as e:
                        print(f"      [-] File read error: {e}")
                        
            elif incoming_msg_id == 7:
                piece_index, begin_offset = struct.unpack('>II', payload[:8])
                raw_piece_data = payload[8:]
                
                print(f"      [<] Received PIECE {piece_index} from {peer_ip}:{peer_port} ({len(raw_piece_data)} bytes)")
                
                expected_hash = info_dict['pieces'][piece_index * 20 : (piece_index + 1) * 20]
                
                actual_hash = hashlib.sha1(raw_piece_data).digest()
                
                print(payload[0:8])
                print(f"      [DEBUG] Piece {piece_index} -> Expected Hash (Hex): {expected_hash.hex()}")
                print(f"      [DEBUG] Piece {piece_index} -> Actual Hash (Hex):   {actual_hash.hex()}")
                print(f"      [DEBUG] Raw data length: {len(raw_piece_data)}")
                
                ### compare hash to see that fila has been transfered correctly
                if expected_hash != actual_hash:
                    print(f"      [-] HASH FAILED for piece {piece_index}! Data is corrupted. Dropping piece.")
                    ### parallel download
                    continue
                else:
                    print(f"      [+] HASH VERIFIED for piece {piece_index}.")
                    
                    downloaded_buffer[piece_index] = raw_piece_data
                    current_piece_index += 1
                    
                    ### send have message so we say that we have this part
                    have_msg = struct.pack('>IBI', 5, 4, piece_index)
                    
                    for (p_ip, p_port), sock in list(ACTIVE_CONNECTIONS.items()):
                        try:
                            sock.send(have_msg)
                            print(f"      [>] Broadcasted HAVE (Piece {piece_index}) to {p_ip}:{p_port}")
                        except:
                            pass

                    ### go for the next piece
                    if current_piece_index < num_pieces:
                        if current_piece_index == num_pieces - 1:
                            req_len = total_length - (current_piece_index * piece_length)
                        else:
                            req_len = piece_length
                        
                        req_msg = struct.pack('>IBIII', 13, 6, current_piece_index, 0, req_len)
                        client_socket.send(req_msg)
                        print(f"      [>] Sent REQUEST for piece {current_piece_index} (size: {req_len} bytes)")
                        try:
                            urllib.request.urlopen(f"http://127.0.0.1:8000/log_traffic?from_peer=127.0.0.1:{PEER_PORT}&to_peer={peer_ip}:{peer_port}&type=REQ")
                        except:
                            pass
                    else:
                        print("      [*] All pieces received! Reconstructing file(s)...")
                        
                        ### attach all the pieces
                        full_data = b"".join(downloaded_buffer[i] for i in range(num_pieces))
                        try:
                            ### save the files
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
                            
                            ### become seeder for this piece and delete it from the files that are being downloaded
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

### this is implemented as the real word case which is 68 bits
def create_handshake(info_hash, peer_id):
    pstr = b"BitTorrent protocol"
    pstr_len = bytes([len(pstr)]) # 19 bytes
    reserved = b'\x00' * 8        # 8 bytes
    
    ### convert str to byte
    if isinstance(peer_id, str):
        peer_id_bytes = peer_id.encode('utf-8')
    else:
        peer_id_bytes = peer_id
        
    return pstr_len + pstr + reserved + info_hash + peer_id_bytes

### start handshake
def initiate_handshake(ip, port, info_hash, torrent_name):
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
            
            s.settimeout(None)

            threading.Thread(target=handle_connection, args=(s, ip, port, torrent_name, info_hash), daemon=True).start()
            return
        
        s.close()
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
        
        total_length = 0
        is_seeder = True
        
        ### chek to see if we are leecher or seeder
        if 'files' in info_dict:
            root_dir = info_dict['name']
            for f in info_dict['files']:
                total_length += f['length']
                file_path = os.path.join(WORKSPACE_DIR, root_dir, *f['path'])
                
                if not os.path.exists(file_path) or os.path.getsize(file_path) != f['length']:
                    is_seeder = False
        else:
            total_length = info_dict['length']
            file_path = os.path.join(WORKSPACE_DIR, torrent_name)
            
            if not os.path.exists(file_path) or os.path.getsize(file_path) != total_length:
                is_seeder = False

        left_bytes = 0 if is_seeder else total_length
        
        status_text = "SEEDER" if is_seeder else "LEECHER"
        print(f"      [*] Status for '{torrent_name}': {status_text} (Left: {left_bytes} bytes)")
        peer_logger.log_event("FILE_CHECK", f"Determined status as {status_text} for {torrent_name}")

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
    ### A Thread for listener 
    listener_thread = threading.Thread(target=start_peer_listener, daemon=True)
    listener_thread.start()
    time.sleep(1)

    ### Check for files to act as Seed 
    torrent_files = [
        r"Torrent_Handler\my_real_torrent_1.txt",
        r"Torrent_Handler\my_real_torrent_2.txt"
    ]
    print("\n[*] Scanning for existing files to seed...")
    for i, file_path in enumerate(torrent_files):
        if os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                torrent_data = bencode.decode(f.read())
            info_dict = torrent_data['info']
            torrent_name = info_dict['name']

            ### If this work space doesnt have the file not seed
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

    ### Ping other peers to check there is still connection 
    def heartbeat_loop():
        while True:
            time.sleep(30)
            if GLOBAL_KNOWN_PEERS:
                print("\n[*] --- Initiating Periodic Heartbeat ---")
                peer_logger.log_event("HEARTBEAT_SYNC", "Periodic ping")
                for peer_ip, peer_port in list(GLOBAL_KNOWN_PEERS):
                    ping_other_peer(peer_ip, peer_port, "Heartbeat")
                    
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    ### make internal cmd to have control over download 
    print("\n[+] Peer is ready! Background tasks (Listener, Pings, Seeding) are running.")
    print("    Commands:")
    print("    download <path_to_torrent_file>  -> Add a new torrent (1 for my_real_torrent_1.txt and 2 for my_real_torrent_2.txt and)")
    print("    exit                             -> Stop the peer")

    try:
        while True:
            cmd = input("\npeer> ").strip()
            if cmd == "exit":
                break
            elif cmd.startswith("download"):
                parts = cmd.split(maxsplit = 1)
                if len(parts) > 1:
                    user_input = parts[1].strip('"').strip("'") 
                    
                    if user_input == "1":
                        torrent_path = r"Torrent_Handler\my_real_torrent_1.txt"
                    elif user_input == "2":
                        torrent_path = r"Torrent_Handler\my_real_torrent_2.txt"
                    else:
                        torrent_path = user_input
                    
                    if os.path.exists(torrent_path):
                        print(f"[*] Adding new torrent: {torrent_path}")
                        t = threading.Thread(target=process_torrent, args=(torrent_path,), daemon=True)
                        t.start()
                    else:
                        abs_path = os.path.abspath(torrent_path)
                        print(f"[-] Error: Torrent file not found!")
                        print(f"    Python is exactly looking here: {abs_path}")
                else:
                    print("[-] Usage: download <1 or 2> OR download <full_path>")
    except KeyboardInterrupt:
        pass

    ### Shuting down peer and save in logger
        
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