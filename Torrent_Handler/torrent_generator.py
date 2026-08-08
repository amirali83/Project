import json
import bencode
import os
import hashlib

def generate_real_pieces_for_torrent(files_list, piece_length):
    full_data = b""
    pieces_hash = b""
    
    for file_path in files_list:
        if os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                full_data += f.read()
        else:
            print(f"[-] Error: File '{file_path}' not found for hashing!")
            
    for i in range(0, len(full_data), piece_length):
        chunk = full_data[i : i + piece_length]
        pieces_hash += hashlib.sha1(chunk).digest()
        
    return pieces_hash

def create_torrent_from_json(json_path, output_torrent_path):
    print(f"[*] Reading human-readable config from: {json_path}")
    
    with open(json_path, 'r', encoding='utf-8') as f:
        torrent_dict = json.load(f)
        
    piece_length = torrent_dict['info']['piece length']
    
    ### If Single File
    if 'info' in torrent_dict and 'name' in torrent_dict['info'] and 'files' not in torrent_dict['info']:
        file_name = torrent_dict['info']['name']
        
        real_pieces = generate_real_pieces_for_torrent([file_name], piece_length)
        torrent_dict['info']['pieces'] = real_pieces
        
    elif 'info' in torrent_dict and 'files' in torrent_dict['info']:
        root_dir = torrent_dict['info']['name']
        file_paths_list = []
        
        for file_info in torrent_dict['info']['files']:
            full_file_path = os.path.join(root_dir, *file_info['path'])
            file_paths_list.append(full_file_path)
            
        all_pieces = generate_real_pieces_for_torrent(file_paths_list, piece_length)
        torrent_dict['info']['pieces'] = all_pieces

    bencoded_data = bencode.encode(torrent_dict)
    
    with open(output_torrent_path, 'wb') as f:
        f.write(bencoded_data)
        
    print(f"[+] Successfully generated: {output_torrent_path}\n")

if __name__ == "__main__":
    
    ### Reading the first Torrent
    json_file_1 = "torrent1.json"
    output_file_1 = "my_real_torrent_1.txt"
    
    if os.path.exists(json_file_1):
        create_torrent_from_json(json_file_1, output_file_1)
    else:
        print(f"[-] {json_file_1} not found.")

    ### Reading the second Torrent
    json_file_2 = "torrent2.json"
    output_file_2 = "my_real_torrent_2.txt"

    if os.path.exists(json_file_2):
        create_torrent_from_json(json_file_2, output_file_2)
    else:
        print(f"[-] {json_file_2} not found.")