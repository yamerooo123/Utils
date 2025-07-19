from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn
import os
import cgi
import json
import time
from urllib.parse import unquote
import gzip
import io

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in separate threads."""
    daemon_threads = True

class FileHandler(SimpleHTTPRequestHandler):
    # Increase buffer size for better performance
    wbufsize = 1024 * 1024  # 1MB buffer
    
    def do_GET(self):
        start_time = time.time()
        try:
            if self.path == '/list-files':
                self.handle_file_list()
            else:
                # Serve static files with caching headers
                if self.path.endswith(('.html', '.css', '.js', '.png', '.jpg', '.jpeg')):
                    self.send_cache_headers()
                super().do_GET()
        finally:
            print(f"GET {self.path} took {time.time() - start_time:.2f}s")

    def do_POST(self):
        start_time = time.time()
        try:
            if self.path == '/upload':
                self.handle_file_upload()
            else:
                self.send_error(404, "Not Found")
        finally:
            print(f"POST {self.path} took {time.time() - start_time:.2f}s")

    def handle_file_upload(self):
        try:
            content_type = self.headers.get('Content-Type')
            if not content_type or not content_type.startswith('multipart/form-data'):
                self.send_error(400, "Bad Request: Expected multipart/form-data")
                return

            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={'REQUEST_METHOD': 'POST'}
            )
            
            if 'file' not in form:
                self.send_error(400, "Bad Request: No file uploaded")
                return

            file_item = form['file']
            if not file_item.filename:
                self.send_error(400, "Bad Request: Empty filename")
                return

            upload_dir = "uploads"
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, os.path.basename(file_item.filename))
            
            # Write file in chunks to handle large files
            with open(file_path, 'wb') as f:
                while True:
                    chunk = file_item.file.read(8192)  # 8KB chunks
                    if not chunk:
                        break
                    f.write(chunk)
            
            self.send_json_response({'status': 'success', 'message': 'File uploaded'})
        except Exception as e:
            self.send_error(500, f"Internal Server Error: {str(e)}")

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
        
        self.send_json_response(files)

    def send_json_response(self, data):
        json_data = json.dumps(data).encode('utf-8')
        
        # Compress response if client supports it
        if 'gzip' in self.headers.get('Accept-Encoding', ''):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='wb') as f:
                f.write(json_data)
            compressed = buf.getvalue()
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Encoding', 'gzip')
            self.send_header('Content-Length', str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)
        else:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(json_data)))
            self.end_headers()
            self.wfile.write(json_data)

    def send_cache_headers(self):
        """Add caching headers for static files"""
        self.send_header('Cache-Control', 'public, max-age=3600')  # 1 hour cache
        self.send_header('Expires', self.date_time_string(time.time() + 3600))

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Connection', 'close')
        SimpleHTTPRequestHandler.end_headers(self)

if __name__ == '__main__':
    # Create uploads directory if it doesn't exist
    os.makedirs("uploads", exist_ok=True)
    
    # Set socket options for better performance
    import socket
    socket.setdefaulttimeout(30)  # 30 seconds timeout
    
    server = ThreadedHTTPServer(('0.0.0.0', 15973), FileHandler)
    print("Server running at http://localhost:15973")
    print("Drag and drop interface: http://localhost:15973/index.html")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()