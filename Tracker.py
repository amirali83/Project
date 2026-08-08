from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
import bencode 
from logger import BitTorrentLogger

swarm_db = {}
live_pings = []
tracker_logger = BitTorrentLogger("tracker_log.txt", "Tracker")

class TrackerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed_url.query)
        
        # --------------------------------------------------------
        # 0. مسیر جدید برای دریافت گزارش پینگ از Peerها
        # --------------------------------------------------------
        if parsed_url.path == '/log_traffic':
            from_p = params.get('from_peer', [''])[0]
            to_p = params.get('to_peer', [''])[0]
            msg_type = params.get('type', ['PING'])[0] # PING, PIECE, REQ, etc.
            
            if from_p and to_p:
                import time
                global live_pings
                live_pings.append({
                    'from': from_p, 
                    'to': to_p, 
                    'type': msg_type,
                    'time': time.time(),
                    'id': f"edge_{time.time()}"
                })
                # نگهداری ترافیک‌های ۳ ثانیه اخیر برای نمایش انیمیشن
                live_pings = [p for p in live_pings if time.time() - p['time'] < 3]
                
            self.send_response(200)
            self.end_headers()
            return

        # --------------------------------------------------------
        # 1. API Endpoint (ارسال همزمان دیتای Swarm و پینگ‌های زنده)
        # --------------------------------------------------------
        elif parsed_url.path == '/api/network':
            import json
            safe_db = {}
            for h, peers in swarm_db.items():
                clean_hash = h.hex() if isinstance(h, bytes) else h.encode('latin-1', 'ignore').hex()
                safe_db[clean_hash] = peers
                
            response_data = {
                'swarm': safe_db,
                'links': live_pings
            }
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(json.dumps(response_data).encode('utf-8'))
            return
            
        # --------------------------------------------------------
        # 2. Dashboard Endpoint (Added P2P Mesh Topology)
        # --------------------------------------------------------
        elif parsed_url.path == '/dashboard':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            
            try:
                # باز کردن و خواندن فایل HTML
                with open('Desktop.html', 'r', encoding='utf-8') as f:
                    html_content = f.read()
                self.wfile.write(html_content.encode('utf-8'))
            except FileNotFoundError:
                # هندل کردن خطای پیدا نشدن فایل
                error_msg = "<h2>Error 404: dashboard.html file not found!</h2>"
                self.wfile.write(error_msg.encode('utf-8'))
            return

        if parsed_url.path == '/announce':
            info_hash = params.get('info_hash', [None])[0]
            peer_id = params.get('peer_id', [None])[0]
            port = params.get('port', [None])[0]

            event = params.get('event', [None])[0]
            left = params.get('left', ['-1'])[0]

            try:
                left = int(left)
            except ValueError:
                left = -1
            
            if not info_hash or not peer_id or not port:
                tracker_logger.log_event("ERROR", "Missing required parameters from client")
                self.send_error_response("Missing required parameters")
                return

            client_ip = self.client_address[0]
            tracker_logger.log_event("REQUEST_RECV", f"Received {event or 'regular'} request from {peer_id} at {client_ip}:{port}")
            
            if info_hash not in swarm_db:
                swarm_db[info_hash] = []
            
            if event == 'stopped':
                swarm_db[info_hash] = [p for p in swarm_db[info_hash] if p['peer_id'] != peer_id]
                
                print(f"[-] Removed Peer (stopped): {peer_id} from {client_ip}:{port}")
                tracker_logger.log_event("PEER_REMOVED", f"Peer {peer_id} gracefully stopped and removed")
                
                self.send_success_response(info_hash)
                return
            
            elif event == 'completed':
                print(f"[*] Peer {peer_id} completed the download! Now a Seeder.")
                tracker_logger.log_event("PEER_COMPLETED", f"Peer {peer_id} at {client_ip}:{port} is now a seeder")
                left = 0

            elif event == "started":
                new_peer = {
                    'peer_id': peer_id,
                    'ip': client_ip,   
                    'port': int(port)  
                }
                
                peer_exists = False
                for p in swarm_db[info_hash]:
                    if p['peer_id'] == peer_id:
                        peer_exists = True
                        p['port'] = int(port) 
                        break
                
                if not peer_exists:
                    swarm_db[info_hash].append(new_peer)

                print(f"[+] Added/Updated Peer: {peer_id} at {client_ip}:{port}")
                tracker_logger.log_event("PEER_ADDED", f"Added/Updated Peer: {peer_id} at {client_ip}:{port}")

            self.send_success_response(info_hash, requested_by=peer_id)
            
        else:
            tracker_logger.log_event("WARNING", f"Invalid path requested: {self.path}")

            self.send_response(404)
            self.end_headers()
        
    def send_success_response(self, info_hash, requested_by=None):
        complete_count = sum(1 for p in swarm_db[info_hash] if p.get('left', -1) == 0)
        incomplete_count = len(swarm_db[info_hash]) - complete_count

        peers_to_send = [
            {'peer_id': p['peer_id'], 
             'ip': p['ip'], 
             'port': p['port']} 
            for p in swarm_db[info_hash]
        ]

        response_dict = {
            'interval': 30,
            'complete': complete_count, 
            'incomplete': incomplete_count,
            'peers': peers_to_send
        }
        
        bencoded_response = bencode.encode(response_dict)
        
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(bencoded_response)

        if requested_by:
            tracker_logger.log_event("RESPONSE_SENT", f"Sent peer list ({len(peers_to_send)} peers) to {requested_by}")   

    def send_error_response(self, reason):
        response = bencode.encode({'failure reason': reason})
        self.send_response(200) 
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(response)

def start_tracker(port=8000):
    server_address = ('', port)
    tracker_server = ThreadingHTTPServer(server_address, TrackerHandler)
    print(f"[*] Tracker is up and running on port {port}...")
    tracker_logger.log_event("SERVER_START", f"Tracker started on port {port}")

    try:
        tracker_server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Shutting down the tracker.")
        tracker_logger.log_event("SERVER_STOP", "Tracker shut down by user")
        tracker_server.server_close()

if __name__ == '__main__':
    start_tracker()