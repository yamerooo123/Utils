from http.server import HTTPServer, SimpleHTTPRequestHandler
import os
import cgi
import json
from urllib.parse import unquote

class FileHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/list-files':
            self.handle_file_list()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == '/upload':
            self.handle_file_upload()
        else:
            self.send_error(404, "Not Found")

    def handle_file_upload(self):
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={'REQUEST_METHOD': 'POST'}
        )
        
        file_item = form['file']
        if file_item.filename:
            upload_dir = "uploads"
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, file_item.filename)
            
            with open(file_path, 'wb') as f:
                f.write(file_item.file.read())
            
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(b'{"status": "success", "message": "File uploaded"}')
        else:
            self.send_error(400, "No file uploaded")

    def handle_file_list(self):
        upload_dir = "uploads"
        os.makedirs(upload_dir, exist_ok=True)
        
        files = []
        for filename in os.listdir(upload_dir):
            filepath = os.path.join(upload_dir, filename)
            if os.path.isfile(filepath):
                files.append({
                    'name': filename,
                    'size': os.path.getsize(filepath),
                    'url': f'/download/{filename}'
                })
        
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(files).encode())

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        super().end_headers()

if __name__ == '__main__':
    # Create uploads directory if it doesn't exist
    os.makedirs("uploads", exist_ok=True)
    
    server = HTTPServer(('0.0.0.0', 8000), FileHandler)
    print("Server running at http://localhost:8000")
    print("Drag and drop interface: http://localhost:8000/index.html")
    server.serve_forever()