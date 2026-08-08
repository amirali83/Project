import json
import bencode
import os
import hashlib

### Creating Hash
def generate_real_pieces(file_path, piece_length):
    pieces = b""
    if not os.path.exists(file_path):
        print(f"[-] Warning: File '{file_path}' not found. Generating dummy pieces.")
        return b'\x00' * 20 

    with open(file_path, 'rb') as f:
        while True:
            chunk = f.read(piece_length)
            if not chunk:
                break
            pieces += hashlib.sha1(chunk).digest()
            
    return pieces

def create_torrent_from_json(json_path, output_torrent_path):
    print(f"[*] Reading human-readable config from: {json_path}")
    
    with open(json_path, 'r', encoding='utf-8') as f:
        torrent_dict = json.load(f)
    
    ### If Single File
    if 'info' in torrent_dict and 'name' in torrent_dict['info'] and 'files' not in torrent_dict['info']:
        file_name = torrent_dict['info']['name']
        piece_length = torrent_dict['info']['piece length']
        
        ### Build Hash
        real_pieces = generate_real_pieces(file_name, piece_length)
        torrent_dict['info']['pieces'] = real_pieces
        
    ### If Multiple Files 
    elif 'info' in torrent_dict and 'files' in torrent_dict['info']:
        piece_length = torrent_dict['info']['piece length']
        root_dir = torrent_dict['info']['name']
        all_pieces = b""
        
        for file_info in torrent_dict['info']['files']:
            full_file_path = os.path.join(root_dir, *file_info['path'])
            
            all_pieces += generate_real_pieces(full_file_path, piece_length)
            
        torrent_dict['info']['pieces'] = all_pieces

    bencoded_data = bencode.encode(torrent_dict)
    
    with open(output_torrent_path, 'wb') as f:
        f.write(bencoded_data)
        
    print(f"[+] Successfully generated: {output_torrent_path}\n")

if __name__ == "__main__":
    
    ### Reading the first Torrent
    json_file_1 = "torrent1.json"
    output_file_1 = "my_real_torrent_1.txt"
    
    ### Creating bencode from simple text
    if os.path.exists(json_file_1):
        create_torrent_from_json(json_file_1, output_file_1)

    ### Reading the second Torrent
    json_file_1 = "torrent2.json"
    output_file_1 = "my_real_torrent_2.txt"

    ### Creating bencode from simple text
    if os.path.exists(json_file_1):
        create_torrent_from_json(json_file_1, output_file_1)