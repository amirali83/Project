import os
import datetime

class BitTorrentLogger:
    def __init__(self, log_filename, component_type):
        self.log_filename = log_filename
        self.component_type = component_type
        
        self.log_dir = os.path.join("logs", self.component_type)
        
        os.makedirs(self.log_dir, exist_ok=True)
        
        self.full_path = os.path.join(self.log_dir, self.log_filename)
        
        self.initialize_log_file()

    def get_current_time(self):
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def initialize_log_file(self):
        if not os.path.exists(self.full_path):
            creation_time = self.get_current_time()
            with open(self.full_path, 'w', encoding='utf-8') as f:
                f.write("="*50 + "\n")
                f.write("HEADER\n")
                f.write(f"File name: {self.log_filename}\n")
                f.write(f"Creation Date/Time: {creation_time}\n")
                f.write(f"Last Modified Date/Time: {creation_time}\n")
                f.write("="*50 + "\n")
                f.write("BODY\n")
                f.write(f"{'Event Type':<15} | {'Date/Time':<20} | {'Description'}\n")
                f.write("-" * 50 + "\n")

    def log_event(self, event_type, description):
        current_time = self.get_current_time()
        
        with open(self.full_path, 'a', encoding='utf-8') as f:
            f.write(f"{event_type:<15} | {current_time:<20} | {description}\n")
        
        self._update_last_modified(current_time)

    def _update_last_modified(self, current_time):
        with open(self.full_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            
        with open(self.full_path, 'w', encoding='utf-8') as f:
            for line in lines:
                if line.startswith("Last Modified Date/Time:"):
                    f.write(f"Last Modified Date/Time: {current_time}\n")
                else:
                    f.write(line)