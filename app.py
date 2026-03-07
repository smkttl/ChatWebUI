import os
import sys
import json
import hashlib
import uuid
import argparse
import logging
from datetime import datetime
import time
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response, stream_with_context, session, g
import requests
import re

# Configure logging to stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(32)

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.txt')
USERDATA_DIR = os.path.join(os.path.dirname(__file__), 'userdata')
SESSION_FILE = os.path.join(USERDATA_DIR, 'sessions.json')

# Models cache: {server_name: {'models': [...], 'timestamp': time}}
MODELS_CACHE = {}
MODELS_CACHE_TTL = 1800  # 5 minutes

os.makedirs(USERDATA_DIR, exist_ok=True)

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def load_servers():
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

def clean_model_name(model_name):
    """Clean model name for readable display in 'Any' server."""
    name = model_name.lower().strip()
    
    # Remove provider prefixes
    prefixes = ['groq/', 'meta-llama/', 'moonshotai/', 'openai/', 'qwen/', 'anthropic/']
    for prefix in prefixes:
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    
    # Remove useless suffixes in order (longer first)
    suffixes_to_remove = [
        '-chat-latest-ca', '-chat-latest', '-latest-ca',
        '-ca', '-chat', ':cloud', '-cloud', '-latest', ':latest',
    ]
    for suffix in suffixes_to_remove:
        if name.endswith(suffix):
            name = name[:-len(suffix)]
    
    # Remove dates/timestamps
    name = re.sub(r'-\d{2,6}(-\d{2})?$', '', name)
    name = re.sub(r'-\d{3,6}(-\d{2})?(?!\w)', '', name)
    
    # Replace : and - with space (keep version dots)
    name = re.sub(r'[:\-]', ' ', name)
    
    # Remove useless tags
    useless_tags = ['preview', 'instruct', 'thinking', 'think', 'pro', 'plus', 'vl', 'versatile']
    for tag in useless_tags:
        name = re.sub(rf'\s+{tag}$', '', name)
        name = re.sub(rf'\s+{tag}\s', ' ', name)
    
    # Normalize: internvl3.5 -> internvl
    name = re.sub(r'^internvl3\.5', 'internvl', name)
    name = re.sub(r'^internvl\s*3\.5', 'internvl', name)
    
    # Normalize: qwen3 -> qwen (standalone)
    name = re.sub(r'^qwen3$', 'qwen', name)
    name = re.sub(r'^qwen3\s', 'qwen ', name)
    
    # Automatically remove size indicators (70b, 120b, 17b, 241b, 32b, etc.)
    name = re.sub(r'\s+\d+b(?:\s+\d+[eb])*$', '', name)
    name = re.sub(r'\s+\d+e(?:\s+\d+[eb])*$', '', name)
    
    # Remove "16k" suffix (context window size)
    name = re.sub(r'\s+16k$', '', name)
    
    # Remove alphanumeric codes at the end (like 30ba3b, a22b)
    name = re.sub(r'\s+[0-9a-z]{3,}$', '', name)
    
    # Replace multiple spaces with single
    name = re.sub(r'\s+', ' ', name)
    
    return name.strip()

def get_any_server_data():
    """Get all models from all servers and return cleaned/merged list for 'Any' server."""
    # Check cache first
    if 'Any' in MODELS_CACHE:
        cached = MODELS_CACHE['Any']
        if time.time() - cached['timestamp'] < MODELS_CACHE_TTL:
            return cached['models'], cached['map']
    
    servers = load_servers()
    all_models = []
    server_models_map = {}  # cleaned_name -> [(server, original_model), ...]
    
    for server in servers:
        try:
            headers = {'Content-Type': 'application/json'}
            if server['api_key']:
                headers['Authorization'] = f"Bearer {server['api_key']}"
            
            if server['api_type'] == 'ollama':
                if server['api_key']:
                    url = f"{server['base_url']}/models"
                else:
                    url = f"{server['base_url']}/api/tags"
                response = requests.get(url, timeout=10)
            else:
                url = f"{server['base_url']}/models"
                response = requests.get(url, headers=headers, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                if server['api_type'] == 'ollama':
                    models = [m.get('name', '') for m in data.get('models', [])]
                else:
                    models = [m.get('id', '') for m in data.get('data', [])]
                
                for model in models:
                    cleaned = clean_model_name(model)
                    if cleaned not in server_models_map:
                        server_models_map[cleaned] = []
                    server_models_map[cleaned].append((server['name'], model))
        except Exception as e:
            logger.warning(f"Failed to get models from {server['name']}: {e}")
            continue
    
    # Get unique cleaned model names
    unique_cleaned = list(server_models_map.keys())
    unique_cleaned.sort()
    
    # Cache the result
    MODELS_CACHE['Any'] = {
        'models': unique_cleaned,
        'map': server_models_map,
        'timestamp': time.time()
    }
    
    return unique_cleaned, server_models_map


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

def record_chat_history(username, server, model, messages):
    user = load_user(username)
    if user:
        if 'chat_history' not in user:
            user['chat_history'] = []
        conversation = {
            'id': str(uuid.uuid4()),
            'server': server,
            'model': model,
            'messages': messages,
            'timestamp': datetime.now().isoformat()
        }
        user['chat_history'].append(conversation)
        if len(user['chat_history']) > 100:
            user['chat_history'] = user['chat_history'][-100:]
        save_user(user)

@app.route('/')
def index():
    servers = load_servers()
    # Add 'Any' server which aggregates all models from other servers
    servers.append({
        'name': 'Any',
        'base_url': '',
        'api_key': '',
        'api_type': 'any'
    })
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
        "usage": {},
        "chat_history": []
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

@app.route('/api/history', methods=['GET'])
@login_required
def get_history():
    user = g.current_user
    history = user.get('chat_history', [])
    return jsonify({'history': history})

@app.route('/api/history', methods=['DELETE'])
@login_required
def clear_history():
    user = g.current_user
    user['chat_history'] = []
    save_user(user)
    return jsonify({'message': 'Chat history cleared'})

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
                    'usage': user.get('usage', {}),
                    'chat_history_count': len(user.get('chat_history', []))
                })
    return jsonify({'users': users})

@app.route('/api/verify', methods=['POST'])
@superuser_required
def verify_user():
    data = request.json
    username = data.get('username')
    action = data.get('action')
    
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
    server_name = request.args.get('server')
    
    # Check cache first for non-Any servers
    if server_name in MODELS_CACHE:
        cached = MODELS_CACHE[server_name]
        if time.time() - cached['timestamp'] < MODELS_CACHE_TTL:
            return jsonify({'models': cached['models']})
    
    # Handle 'Any' server - return merged/cleaned models from all servers
    if server_name == 'Any':
        try:
            models, server_map = get_any_server_data()
            return jsonify({'models': models})
        except Exception as e:
            logger.error(f"Error getting Any server models: {e}")
            return jsonify({'error': str(e)}), 500
    
    servers = load_servers()
    
    for server in servers:
        if server['name'] == server_name:
            try:
                headers = {'Content-Type': 'application/json'}
                if server['api_key']:
                    headers['Authorization'] = f"Bearer {server['api_key']}"
                
                if server['api_type'] == 'ollama':
                    if server['api_key']:
                        url = f"{server['base_url']}/models"
                    else:
                        url = f"{server['base_url']}/api/tags"
                    response = requests.get(url, timeout=10)
                else:
                    url = f"{server['base_url']}/models"
                    response = requests.get(url, headers=headers, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    if server['api_type'] == 'ollama':
                        models = [m.get('name', '') for m in data.get('models', [])]
                    else:
                        models = [m.get('id', '') for m in data.get('data', [])]
                    
                    # Cache the result
                    MODELS_CACHE[server_name] = {
                        'models': models,
                        'timestamp': time.time()
                    }
                    
                    return jsonify({'models': models})
                else:
                    error_msg = f"API returned status {response.status_code}: {response.text[:200]}"
                    logger.error(f"Models API error for {server_name}: {error_msg}")
                    return jsonify({'error': error_msg}), 400
            except requests.exceptions.Timeout:
                error_msg = f"Request timeout when fetching models from {server_name}"
                logger.error(error_msg)
                return jsonify({'error': error_msg}), 504
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error to {server_name}: {str(e)[:200]}"
                logger.error(error_msg)
                return jsonify({'error': error_msg}), 503
            except Exception as e:
                error_msg = f"Error fetching models: {str(e)[:200]}"
                logger.error(f"Models API error for {server_name}: {error_msg}")
                return jsonify({'error': error_msg}), 500
    
    return jsonify({'error': 'Server not found'}), 404

@app.route('/api/chat', methods=['POST'])
@login_required
def chat():
    data = request.json
    server_name = data.get('server')
    messages = data.get('messages', [])
    model = data.get('model')
    stream = data.get('stream', False)
    
    servers = load_servers()
    username = g.current_user['username']
    
    # Handle 'Any' server - try each possible server/model until one succeeds
    if server_name == 'Any':
        try:
            _, server_map = get_any_server_data()
            candidates = server_map.get(model, [])
            
            if not candidates:
                return jsonify({'error': f'No servers available for model: {model}'}), 404
            
            # Try each candidate server/model until one succeeds
            last_error = None
            for actual_server_name, actual_model in candidates:
                # Find the actual server config
                actual_server = None
                for s in servers:
                    if s['name'] == actual_server_name:
                        actual_server = s
                        break
                
                if not actual_server:
                    continue
                
                headers = {'Content-Type': 'application/json'}
                if actual_server['api_key']:
                    headers['Authorization'] = f"Bearer {actual_server['api_key']}"
                
                if actual_server['api_type'] == 'ollama':
                    url = f"{actual_server['base_url']}/api/chat"
                    payload = {'model': actual_model, 'messages': messages, 'stream': stream}
                else:
                    url = f"{actual_server['base_url']}/chat/completions"
                    payload = {'model': actual_model, 'messages': messages, 'stream': stream}
                
                logger.info(f"Any server trying: server={actual_server_name}, model={actual_model}, stream={stream}")
                
                try:
                    if stream:
                        def generate():
                            try:
                                response = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)
                                
                                if response.status_code != 200:
                                    error_msg = f"API returned status {response.status_code}: {response.text[:500]}"
                                    logger.error(f"Chat API error: {error_msg}")
                                    yield f"data: {json.dumps({'error': error_msg})}\n\n"
                                    return
                                
                                full_content = ''
                                full_thinking = ''
                                prompt_tokens = 0
                                completion_tokens = 0
                                
                                if actual_server['api_type'] == 'ollama':
                                    for line in response.iter_lines():
                                        if line:
                                            line_decoded = line.decode('utf-8')
                                            yield f"data: {line_decoded}\n\n"
                                            try:
                                                parsed = json.loads(line_decoded)
                                                if parsed.get('error'):
                                                    logger.error(f"Ollama API error: {parsed['error']}")
                                                msg = parsed.get('message', {})
                                                thinking = msg.get('thinking', '')
                                                content = msg.get('content', '')
                                                if thinking:
                                                    full_thinking += thinking
                                                if content:
                                                    full_content += content
                                            except:
                                                pass
                                else:
                                    for chunk in response.iter_content(chunk_size=None):
                                        if chunk:
                                            yield chunk
                                            try:
                                                for line in chunk.decode('utf-8').split('\n'):
                                                    if line.startswith('data: '):
                                                        data_str = line[6:]
                                                        if data_str == '[DONE]':
                                                            continue
                                                        parsed = json.loads(data_str)
                                                        if parsed.get('error'):
                                                            logger.error(f"API error: {parsed['error']}")
                                                        if parsed.get('choices'):
                                                            delta = parsed['choices'][0].get('delta', {}).get('content', '')
                                                            full_content += delta
                                                        usage = parsed.get('usage', {})
                                                        prompt_tokens = usage.get('prompt_tokens', 0)
                                                        completion_tokens = usage.get('completion_tokens', 0)
                                            except:
                                                pass
                                
                                record_usage(username, actual_model, prompt_tokens, completion_tokens)
                                
                                assistant_msg = {'role': 'assistant'}
                                if full_thinking:
                                    assistant_msg['thinking'] = full_thinking
                                if full_content:
                                    assistant_msg['content'] = full_content
                                
                                conversation = messages + [assistant_msg]
                                record_chat_history(username, actual_server_name, actual_model, conversation)
                                
                            except requests.exceptions.Timeout:
                                error_msg = "Request timeout after 120 seconds"
                                logger.error(f"Chat API timeout: {error_msg}")
                                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                            except requests.exceptions.ConnectionError as e:
                                error_msg = f"Connection error: {str(e)[:200]}"
                                logger.error(f"Chat API connection error: {error_msg}")
                                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                            except Exception as e:
                                error_msg = f"Error during streaming: {str(e)[:200]}"
                                logger.error(f"Chat API error: {error_msg}")
                                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                        
                        content_type = 'text/event-stream' if actual_server['api_type'] == 'ollama' else 'application/x-ndjson'
                        return Response(stream_with_context(generate()), content_type=content_type)
                    else:
                        response = requests.post(url, json=payload, headers=headers, timeout=120)
                        
                        if response.status_code != 200:
                            error_msg = f"API returned status {response.status_code}: {response.text[:500]}"
                            logger.error(f"Chat API error: {error_msg}")
                            last_error = (jsonify({'error': error_msg}), response.status_code)
                            continue
                        
                        resp_json = response.json()
                        
                        if resp_json.get('error'):
                            error_msg = resp_json['error']
                            logger.error(f"Chat API error: {error_msg}")
                            last_error = (jsonify({'error': error_msg}), 400)
                            continue
                        
                        prompt_tokens = resp_json.get('usage', {}).get('prompt_tokens', 0)
                        completion_tokens = resp_json.get('usage', {}).get('completion_tokens', 0)
                        record_usage(username, actual_model, prompt_tokens, completion_tokens)
                        
                        assistant_msg = {'role': 'assistant'}
                        msg = resp_json.get('choices', [{}])[0].get('message', {})
                        if msg.get('thinking'):
                            assistant_msg['thinking'] = msg['thinking']
                        if msg.get('content'):
                            assistant_msg['content'] = msg['content']
                        conversation = messages + [assistant_msg]
                        record_chat_history(username, actual_server_name, actual_model, conversation)
                        
                        return jsonify(resp_json)
                        
                except requests.exceptions.Timeout:
                    last_error = (jsonify({'error': 'Request timeout after 120 seconds'}), 504)
                    logger.error(f"Chat API timeout for {actual_server_name}")
                except requests.exceptions.ConnectionError as e:
                    last_error = (jsonify({'error': f'Connection error to {actual_server_name}: {str(e)[:200]}'}), 503)
                    logger.error(f"Connection error for {actual_server_name}: {e}")
                except Exception as e:
                    last_error = (jsonify({'error': f'Error: {str(e)[:200]}'}), 500)
                    logger.error(f"Chat API error for {actual_server_name}: {e}")
                # Continue to next candidate
            
            # All candidates failed
            if last_error:
                return last_error
            return jsonify({'error': 'All servers failed for this model'}), 500
            
        except Exception as e:
            logger.error(f"Any server error: {e}")
            return jsonify({'error': str(e)}), 500
    
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
            
            logger.info(f"Chat request: server={server_name}, model={model}, stream={stream}")
            
            try:
                if stream:
                    def generate():
                        try:
                            response = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)
                            
                            if response.status_code != 200:
                                error_msg = f"API returned status {response.status_code}: {response.text[:500]}"
                                logger.error(f"Chat API error: {error_msg}")
                                yield f"data: {json.dumps({'error': error_msg})}\n\n"
                                return
                            
                            full_content = ''
                            full_thinking = ''
                            prompt_tokens = 0
                            completion_tokens = 0
                            
                            if server['api_type'] == 'ollama':
                                for line in response.iter_lines():
                                    if line:
                                        line_decoded = line.decode('utf-8')
                                        yield f"data: {line_decoded}\n\n"
                                        try:
                                            parsed = json.loads(line_decoded)
                                            if parsed.get('error'):
                                                logger.error(f"Ollama API error: {parsed['error']}")
                                            msg = parsed.get('message', {})
                                            thinking = msg.get('thinking', '')
                                            content = msg.get('content', '')
                                            if thinking:
                                                full_thinking += thinking
                                            if content:
                                                full_content += content
                                        except:
                                            pass
                            else:
                                for chunk in response.iter_content(chunk_size=None):
                                    if chunk:
                                        yield chunk
                                        try:
                                            for line in chunk.decode('utf-8').split('\n'):
                                                if line.startswith('data: '):
                                                    data_str = line[6:]
                                                    if data_str == '[DONE]':
                                                        continue
                                                    parsed = json.loads(data_str)
                                                    if parsed.get('error'):
                                                        logger.error(f"API error: {parsed['error']}")
                                                    if parsed.get('choices'):
                                                        delta = parsed['choices'][0].get('delta', {}).get('content', '')
                                                        full_content += delta
                                                    usage = parsed.get('usage', {})
                                                    prompt_tokens = usage.get('prompt_tokens', 0)
                                                    completion_tokens = usage.get('completion_tokens', 0)
                                        except:
                                            pass
                            
                            record_usage(username, model, prompt_tokens, completion_tokens)
                            
                            assistant_msg = {'role': 'assistant'}
                            if full_thinking:
                                assistant_msg['thinking'] = full_thinking
                            if full_content:
                                assistant_msg['content'] = full_content
                            
                            conversation = messages + [assistant_msg]
                            record_chat_history(username, server_name, model, conversation)
                            
                        except requests.exceptions.Timeout:
                            error_msg = "Request timeout after 120 seconds"
                            logger.error(f"Chat API timeout: {error_msg}")
                            yield f"data: {json.dumps({'error': error_msg})}\n\n"
                        except requests.exceptions.ConnectionError as e:
                            error_msg = f"Connection error: {str(e)[:200]}"
                            logger.error(f"Chat API connection error: {error_msg}")
                            yield f"data: {json.dumps({'error': error_msg})}\n\n"
                        except Exception as e:
                            error_msg = f"Error during streaming: {str(e)[:200]}"
                            logger.error(f"Chat API error: {error_msg}")
                            yield f"data: {json.dumps({'error': error_msg})}\n\n"
                    
                    content_type = 'text/event-stream' if server['api_type'] == 'ollama' else 'application/x-ndjson'
                    return Response(stream_with_context(generate()), content_type=content_type)
                else:
                    response = requests.post(url, json=payload, headers=headers, timeout=120)
                    
                    if response.status_code != 200:
                        error_msg = f"API returned status {response.status_code}: {response.text[:500]}"
                        logger.error(f"Chat API error: {error_msg}")
                        return jsonify({'error': error_msg}), response.status_code
                    
                    resp_json = response.json()
                    
                    if resp_json.get('error'):
                        error_msg = resp_json['error']
                        logger.error(f"Chat API error: {error_msg}")
                        return jsonify({'error': error_msg}), 400
                    
                    prompt_tokens = resp_json.get('usage', {}).get('prompt_tokens', 0)
                    completion_tokens = resp_json.get('usage', {}).get('completion_tokens', 0)
                    record_usage(username, model, prompt_tokens, completion_tokens)
                    
                    assistant_msg = {'role': 'assistant'}
                    msg = resp_json.get('choices', [{}])[0].get('message', {})
                    if msg.get('thinking'):
                        assistant_msg['thinking'] = msg['thinking']
                    if msg.get('content'):
                        assistant_msg['content'] = msg['content']
                    conversation = messages + [assistant_msg]
                    record_chat_history(username, server_name, model, conversation)
                    
                    return jsonify(resp_json)
                    
            except requests.exceptions.Timeout:
                error_msg = "Request timeout after 120 seconds"
                logger.error(f"Chat API timeout for {server_name}: {error_msg}")
                return jsonify({'error': error_msg}), 504
            except requests.exceptions.ConnectionError as e:
                error_msg = f"Connection error to {server_name}: {str(e)[:200]}"
                logger.error(error_msg)
                return jsonify({'error': error_msg}), 503
            except Exception as e:
                error_msg = f"Error: {str(e)[:200]}"
                logger.error(f"Chat API error for {server_name}: {error_msg}")
                return jsonify({'error': error_msg}), 500
    
    return jsonify({'error': 'Server not found'}), 404

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Chat Web UI Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('-p', '--port', type=int, default=5000, help='Port to bind to (default: 5000)')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode')
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug)
