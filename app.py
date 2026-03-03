import os
from flask import Flask, render_template, request, jsonify, Response, stream_with_context
import requests

app = Flask(__name__)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.txt')

def load_servers():
    """Load servers from config file. Format: Name|BaseURL|APIKey|APIType"""
    servers = []
    with open(CONFIG_FILE, 'r') as f:
        for line in f:
            line = line.strip()
            if line and '|' in line:
                parts = line.split('|')
                if len(parts) >= 4:
                    servers.append({
                        'name': parts[0].strip(),
                        'base_url': parts[1].strip(),
                        'api_key': parts[2].strip(),
                        'api_type': parts[3].strip()
                    })
    return servers

@app.route('/')
def index():
    servers = load_servers()
    return render_template('index.html', servers=servers)

@app.route('/api/models')
def get_models():
    """Get available models for a given server"""
    server_name = request.args.get('server')
    servers = load_servers()
    
    for server in servers:
        if server['name'] == server_name:
            try:
                headers = {
                    'Content-Type': 'application/json'
                }
                
                # Add auth header if API key is provided
                if server['api_key']:
                    headers['Authorization'] = f"Bearer {server['api_key']}"
                
                if server['api_type'] == 'ollama':
                    # Ollama uses /api/tags for local, but cloud uses /models
                    if server['api_key']:
                        # Cloud Ollama - use /api/models endpoint
                        response = requests.get(
                            f"{server['base_url']}/models",
                            headers=headers,
                            timeout=10
                        )
                    else:
                        # Local Ollama
                        response = requests.get(
                            f"{server['base_url']}/api/tags",
                            timeout=10
                        )
                    if response.status_code == 200:
                        data = response.json()
                        if server['api_key']:
                            # Cloud response format
                            models = [m.get('name', '') for m in data.get('models', [])]
                        else:
                            # Local response format
                            models = [m.get('name', '') for m in data.get('models', [])]
                        return jsonify({'models': models})
                else:
                    # OpenAI-compatible API
                    response = requests.get(
                        f"{server['base_url']}/models",
                        headers=headers,
                        timeout=10
                    )
                    if response.status_code == 200:
                        data = response.json()
                        models = [m.get('id', '') for m in data.get('data', [])]
                        return jsonify({'models': models})
                return jsonify({'error': f'Failed to fetch models: {response.status_code}'}), 400
            except Exception as e:
                return jsonify({'error': str(e)}), 400
    
    return jsonify({'error': 'Server not found'}), 404

@app.route('/api/chat', methods=['POST'])
def chat():
    """Proxy chat requests to the selected server"""
    data = request.json
    server_name = data.get('server')
    messages = data.get('messages', [])
    model = data.get('model')
    stream = data.get('stream', False)
    
    servers = load_servers()
    
    for server in servers:
        if server['name'] == server_name:
            headers = {
                'Content-Type': 'application/json'
            }
            
            # Add auth header if API key is provided
            if server['api_key']:
                headers['Authorization'] = f"Bearer {server['api_key']}"
            
            # Build URL and payload based on API type
            if server['api_type'] == 'ollama':
                url = f"{server['base_url']}/api/chat"
                payload = {
                    'model': model,
                    'messages': messages,
                    'stream': stream
                }
            else:
                url = f"{server['base_url']}/chat/completions"
                payload = {
                    'model': model,
                    'messages': messages,
                    'stream': stream
                }
            
            try:
                if stream:
                    def generate():
                        response = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)
                        
                        if server['api_type'] == 'ollama':
                            # Ollama streaming format (SSE)
                            for line in response.iter_lines():
                                if line:
                                    line = line.decode('utf-8')
                                    if line.startswith('data: '):
                                        yield line + '\n'
                        else:
                            # OpenAI-compatible streaming format (ndjson)
                            for chunk in response.iter_content(chunk_size=None):
                                if chunk:
                                    yield chunk
                    
                    content_type = 'text/event-stream' if server['api_type'] == 'ollama' else 'application/x-ndjson'
                    return Response(stream_with_context(generate()), content_type=content_type)
                else:
                    response = requests.post(url, json=payload, headers=headers, timeout=120)
                    return jsonify(response.json())
            except Exception as e:
                return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Server not found'}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
