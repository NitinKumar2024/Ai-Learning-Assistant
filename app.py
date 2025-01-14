import json
import sqlite3
import uuid
from datetime import datetime
import re

import requests
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)


def init_db():
    conn = sqlite3.connect('chat_history.db')
    c = conn.cursor()
    # Add last_accessed column to track most recent conversation
    c.execute('''
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            last_accessed TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP,
            FOREIGN KEY (conversation_id) REFERENCES conversations (id)
        )
    ''')
    conn.commit()
    conn.close()


init_db()


class OllamaChat:
    def __init__(self, model: str = "llama2", host: str = "http://localhost:11434"):
        self.model = model
        self.host = host
        self.conversation_contexts = {}  # Store context per conversation

    def get_context(self, conversation_id: str):
        return self.conversation_contexts.get(conversation_id, [])

    def set_context(self, conversation_id: str, context: list):
        self.conversation_contexts[conversation_id] = context

    def generate_title(self, prompt: str) -> str:
        url = f"{self.host}/api/generate"
        title_prompt = f"Generate a very brief title (3-5 words) for a conversation that starts with: {prompt}"

        try:
            response = requests.post(url, json={
                "model": self.model,
                "prompt": title_prompt,
                "stream": False
            })
            response.raise_for_status()
            title = response.json().get('response', '').strip()
            title = re.sub(r'^["\']*|["\']*$', '', title)
            title = re.sub(r'\n', ' ', title)
            return title[:50]
        except:
            return "New Chat"

    def generate_response(self, prompt: str, conversation_id: str = None) -> Response:
        url = f"{self.host}/api/generate"
        headers = {"Content-Type": "application/json"}

        # Get context for this specific conversation
        context = self.get_context(conversation_id) if conversation_id else []

        data = {
            "model": self.model,
            "prompt": prompt,
            "context": context,
            "stream": True
        }

        def generate():
            try:
                response = requests.post(url, headers=headers, json=data, stream=True)
                response.raise_for_status()

                full_response = ""
                for line in response.iter_lines():
                    if line:
                        json_response = json.loads(line)
                        if 'response' in json_response:
                            chunk = json_response['response']
                            full_response += chunk
                            yield f"data: {json.dumps({'type': 'content', 'content': chunk})}\n\n"
                        if json_response.get('done', False):
                            if 'context' in json_response and conversation_id:
                                self.set_context(conversation_id, json_response['context'])
                            if conversation_id:
                                add_message(conversation_id, 'assistant', full_response)
                            yield f"data: {json.dumps({'type': 'done', 'fullResponse': full_response, 'conversationId': conversation_id})}\n\n"

            except requests.exceptions.ConnectionError:
                yield f"data: {json.dumps({'type': 'error', 'content': 'Error: Cannot connect to Ollama. Make sure it\'s running locally.'})}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'content': f'Error: {str(e)}'})}\n\n"

        return Response(stream_with_context(generate()), mimetype='text/event-stream')


chat = OllamaChat()


def get_db():
    conn = sqlite3.connect('chat_history.db')
    conn.row_factory = sqlite3.Row
    return conn


def create_conversation(model: str, prompt: str) -> str:
    conn = get_db()
    conversation_id = str(uuid.uuid4())
    now = datetime.utcnow()
    title = chat.generate_title(prompt)

    conn.execute(
        'INSERT INTO conversations (id, title, model, created_at, updated_at, last_accessed) VALUES (?, ?, ?, ?, ?, ?)',
        (conversation_id, title, model, now, now, now)
    )
    conn.commit()
    conn.close()
    return conversation_id


def add_message(conversation_id: str, role: str, content: str):
    conn = get_db()
    try:
        message_id = str(uuid.uuid4())
        now = datetime.utcnow()

        # Update in a transaction to ensure consistency
        conn.execute('BEGIN TRANSACTION')

        # Add message
        conn.execute(
            'INSERT INTO messages (id, conversation_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)',
            (message_id, conversation_id, role, content, now)
        )

        # Update conversation timestamp
        conn.execute(
            'UPDATE conversations SET updated_at = ?, last_accessed = ? WHERE id = ?',
            (now, now, conversation_id)
        )

        conn.commit()
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def get_conversations():
    conn = get_db()
    try:
        conversations = conn.execute(
            'SELECT * FROM conversations ORDER BY last_accessed DESC'
        ).fetchall()
        return [dict(conv) for conv in conversations]
    finally:
        conn.close()


def get_conversation_messages(conversation_id: str):
    conn = get_db()
    try:
        # Update last_accessed timestamp
        now = datetime.utcnow()
        conn.execute(
            'UPDATE conversations SET last_accessed = ? WHERE id = ?',
            (now, conversation_id)
        )
        conn.commit()

        messages = conn.execute(
            'SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at',
            (conversation_id,)
        ).fetchall()
        return [dict(msg) for msg in messages]
    finally:
        conn.close()


@app.route('/')
def home():
    return render_template('index.html')


@app.route('/chat', methods=['POST'])
def chat_response():
    data = request.json
    prompt = data.get('prompt', '')
    model = data.get('model', 'llama2')
    conversation_id = data.get('conversation_id')

    if not conversation_id:
        conversation_id = create_conversation(model, prompt)

    # Store user message
    add_message(conversation_id, 'user', prompt)

    # Set the model and generate response
    chat.model = model
    return chat.generate_response(prompt, conversation_id)


@app.route('/conversations', methods=['GET'])
def get_conversation_list():
    return jsonify(get_conversations())


@app.route('/conversations/<conversation_id>', methods=['GET'])
def get_conversation(conversation_id):
    return jsonify(get_conversation_messages(conversation_id))


@app.route('/conversations/<conversation_id>', methods=['DELETE'])
def delete_conversation(conversation_id):
    conn = get_db()
    try:
        conn.execute('BEGIN TRANSACTION')
        conn.execute('DELETE FROM messages WHERE conversation_id = ?', (conversation_id,))
        conn.execute('DELETE FROM conversations WHERE id = ?', (conversation_id,))
        conn.commit()

        # Clear the context for this conversation
        if conversation_id in chat.conversation_contexts:
            del chat.conversation_contexts[conversation_id]

        return jsonify({'status': 'success'})
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


@app.route('/conversations/<conversation_id>/title', methods=['PUT'])
def update_conversation_title(conversation_id):
    data = request.json
    new_title = data.get('title', 'New Chat')
    conn = get_db()
    try:
        conn.execute(
            'UPDATE conversations SET title = ? WHERE id = ?',
            (new_title, conversation_id)
        )
        conn.commit()
        return jsonify({'status': 'success'})
    finally:
        conn.close()


@app.route('/get_available_models')
def get_available_models():
    try:
        response = requests.get('http://localhost:11434/api/tags')
        models = response.json().get('models', [])
        formatted_models = [
            {
                'name': model['name'],
                'details': model.get('details', {}),
                'modified_at': model.get('modified_at', '')
            }
            for model in models
        ]
        return jsonify({'models': formatted_models})
    except Exception as e:
        return jsonify({'models': [{'name': 'llama2:latest', 'details': {}}]})


if __name__ == '__main__':
    app.run(debug=True)