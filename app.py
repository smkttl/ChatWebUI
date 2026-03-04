import os
import sys
import json
import hashlib
import uuid
import argparse
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session, g
import requests

app = Flask(__name__)
app.secret_key = os.urandom(32)  # Secure secret key

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.txt')
USERDATA_DIR = os.path.join(os.path.dirname(__file__), 'userdata')
SESSION_FILE = os.path.join(USERDATA_DIR, 'sessions.json')

# Ensure directories exist
os.makedirs(USERDATA_DIR, exist_ok=True)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

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

def load_user(username):
    user_file = os.path.join(USERDATA_DIR, f"{username}.json")
    if os.path.exists(user_file):
        with open(user_file, 'r') as f:
            return json.load(f)
    return None

def save_user(user_data):
    user_file = os.path.join(USERDATA_DIR, f"{user_data['username']}.json")
    with open(user_file, 'w') as f:
        json.dump(user_data, f, indent=2)

def load_sessions():
    if os.path.exists(SESSION_FILE):
        with open(SESSION_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_sessions(sessions):
    with open(SESSION_FILE, 'w') as f:
        json.dump(sessions, f)

def get_current_user():
    session_token = session.get('session_token')
    if not session_token:
        return None
    
    sessions = load_sessions()
    username = sessions.get(session_token)
    if not username:
        return None
    
    return load_user(username)

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Login required'}), 401
        g.current_user = user
        return f(*args, **kwargs)
    return decorated_function

def superuser_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({'error': 'Login required'}), 401
        if not user.get('is_superuser'):
            return jsonify({'error': 'Superuser access required'}), 403
        g.current_user = user
        return f(*args, **kwargs)
    return decorated_function

def record_usage(username, model, prompt_tokens=0, completion_tokens=0):
    user = load_user(username)
    if user:
        if 'usage' not in user:
            user['usage'] = {}
        
        date_key = datetime.now().strftime('%Y-%m-%d')
        if date_key not in user['usage']:
            user['usage'][date_key] = {'requests': 0, 'prompt_tokens': 0, 'completion_tokens': 0}
        
        user['usage'][date_key]['requests'] += 1
        user['usage'][date_key]['prompt_tokens'] += prompt_tokens
        user['usage'][date_key]['completion_tokens'] += completion_tokens
        save_user(user)

@app.route('/')
def index():
    servers = load_servers()
    user = get_current_user()
    return render_template('index.html', servers=servers, user=user)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    email = data.get('email', '').strip()
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    
    if load_user(username):
        return jsonify({'error': 'Username already exists'}), 400
    
    user_data = {
        "username": username,
        "password_hash": hash_password(password),
        "email": email,
        "is_verified": False,
        "is_superuser": False,
        "created_at": datetime.now().isoformat(),
        "usage": {}
    }
    
    save_user(user_data)
    return jsonify({'message': 'Registration successful. Please wait for superuser verification.'})

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    
    user = load_user(username)
    if not user:
        return jsonify({'error': 'Invalid credentials'}), 401
    
    if user['password_hash'] != hash_password(password):
        return jsonify({'error': 'Invalid credentials'}), 401
    
    if not user.get('is_verified'):
        return jsonify({'error': 'Account not verified by superuser'}), 403
    
    # Create session
    session_token = str(uuid.uuid4())
    sessions = load_sessions()
    sessions[session_token] = username
    save_sessions(sessions)
    
    session['session_token'] = session_token
    
    return jsonify({
        'message': 'Login successful',
        'username': username,
        'is_superuser': user.get('is_superuser', False)
    })

@app.route('/api/logout', methods=['POST'])
def logout():
    session_token = session.get('session_token')
    if session_token:
        sessions = load_sessions()
        sessions.pop(session_token, None)
        save_sessions(sessions)
        session.pop('session_token', None)
    
    return jsonify({'message': 'Logged out'})

@app.route('/api/me', methods=['GET'])
@login_required
def get_me():
    user = g.current_user
    return jsonify({
        'username': user['username'],
        'email': user.get('email', ''),
        'is_superuser': user.get('is_superuser', False)
    })

@app.route('/api/users', methods=['GET'])
@superuser_required
def list_users():
    users = []
    for filename in os.listdir(USERDATA_DIR):
        if filename.endswith('.json') and filename != 'sessions.json':
            with open(os.path.join(USERDATA_DIR, filename), 'r') as f:
                user = json.load(f)
                users.append({
                    'username': user['username'],
                    'email': user.get('email', ''),
                    'is_verified': user.get('is_verified', False),
                    'is_superuser': user.get('is_superuser', False),
                    'created_at': user.get('created_at'),
                    'usage': user.get('usage', {})
                })
    
    return jsonify({'users': users})

@app.route('/api/verify', methods=['POST'])
@superuser_required
def verify_user():
    data = request.json
    username = data.get('username')
    action = data.get('action')  # 'verify' or 'reject'
    
    user = load_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    if action == 'verify':
        user['is_verified'] = True
    elif action == 'reject':
        user_file = os.path.join(USERDATA_DIR, f"{username}.json")
        os.remove(user_file)
        return jsonify({'message': f'User {username} rejected and removed'})
    
    save_user(user)
    return jsonify({'message': f'User {username} {"verified" if action == "verify" else action}'})

@app.route('/api/models')
@login_required
def get_models():
    """Get available models for a given server"""
    server_name = request.args.get('server')
    servers = load_servers()
    
    for server in servers:
        if server['name'] == server_name:
            try:
                headers = {'Content-Type': 'application/json'}
                
                if server['api_key']:
                    headers['Authorization'] = f"Bearer {server['api_key']}"
                
                if server['api_type'] == 'ollama':
                    if server['api_key']:
                        response = requests.get(
                            f"{server['base_url']}/models",
                            headers=headers,
                            timeout=10
                        )
                    else:
                        response = requests.get(
                            f"{server['base_url']}/api/tags",
                            timeout=10
                        )
                    if response.status_code == 200:
                        data = response.json()
                        models = [m.get('name', '') for m in data.get('models', [])]
                        return jsonify({'models': models})
                else:
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
@login_required
def chat():
    """Proxy chat requests to the selected server"""
    data = request.json
    server_name = data.get('server')
    messages = data.get('messages', [])
    model = data.get('model')
    stream = data.get('stream', False)
    
    servers = load_servers()
    username = g.current_user['username']
    
    for server in servers:
        if server['name'] == server_name:
            headers = {'Content-Type': 'application/json'}
            
            if server['api_key']:
                headers['Authorization'] = f"Bearer {server['api_key']}"
            
            if server['api_type'] == 'ollama':
                url = f"{server['base_url']}/api/chat"
                payload = {'model': model, 'messages': messages, 'stream': stream}
            else:
                url = f"{server['base_url']}/chat/completions"
                payload = {'model': model, 'messages': messages, 'stream': stream}
            
            try:
                if stream:
                    def generate():
                        response = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)
                        
                        full_content = ''
                        prompt_tokens = 0
                        completion_tokens = 0
                        
                        if server['api_type'] == 'ollama':
                            for line in response.iter_lines():
                                if line:
                                    line = line.decode('utf-8')
                                    if line.startswith('data: '):
                                        yield line + '\n'
                                        try:
                                            parsed = json.loads(line[6:])
                                            if 'message' in parsed:
                                                full_content += parsed['message'].get('content', '')
                                            # Ollama doesn't always provide token counts
                                        except:
                                            pass
                        else:
                            for chunk in response.iter_content(chunk_size=None):
                                if chunk:
                                    yield chunk
                                    try:
                                        for line in chunk.decode('utf-8').split('\n'):
                                            if line.startswith('data: '):
                                                parsed = json.loads(line[6:])
                                                if parsed.get('choices'):
                                                    delta = parsed['choices'][0].get('delta', {}).get('content', '')
                                                    full_content += delta
                                                # Try to get token counts
                                                usage = parsed.get('usage', {})
                                                prompt_tokens = usage.get('prompt_tokens', 0)
                                                completion_tokens = usage.get('completion_tokens', 0)
                                    except:
                                        pass
                        
                        # Record usage
                        record_usage(username, model, prompt_tokens, completion_tokens)
                    
                    content_type = 'text/event-stream' if server['api_type'] == 'ollama' else 'application/x-ndjson'
                    return Response(stream_with_context(generate()), content_type=content_type)
                else:
                    response = requests.post(url, json=payload, headers=headers, timeout=120)
                    resp_json = response.json()
                    
                    # Record usage for non-streaming
                    prompt_tokens = resp_json.get('usage', {}).get('prompt_tokens', 0)
                    completion_tokens = resp_json.get('usage', {}).get('completion_tokens', 0)
                    record_usage(username, model, prompt_tokens, completion_tokens)
                    
                    return jsonify(resp_json)
            except Exception as e:
                return jsonify({'error': str(e)}), 500
    
    return jsonify({'error': 'Server not found'}), 404

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Chat Web UI Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to bind to (default: 5000)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    
    args = parser.parse_args()
    
    app.run(host=args.host, port=args.port, debug=args.debug)
