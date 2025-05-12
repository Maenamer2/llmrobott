from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
import openai
import json
from dotenv import load_dotenv
import os
import time
import logging
import re
import sqlite3
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your_secret_key")  # Better to use env variable on Render

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# SQLite Database setup
DB_PATH = 'robot_app.db'

def init_db():
    """Initialize the database with necessary tables."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Create users table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create command_history table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS command_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        command TEXT NOT NULL,
        parsed_command TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users (id)
    )
    ''')
    
    # Insert default admin user if not exists
    cursor.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
    if cursor.fetchone()[0] == 0:
        cursor.execute(
            "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
            ('admin', generate_password_hash('admin'), 'admin')
        )
        
    # Insert some example users if not exists
    example_users = [
        ('maen', 'maen', 'admin'),
        ('user1', 'password1', 'user'),
        ('robotics', 'securepass', 'user')
    ]
    
    for username, password, role in example_users:
        cursor.execute("SELECT COUNT(*) FROM users WHERE username = ?", (username,))
        if cursor.fetchone()[0] == 0:
            cursor.execute(
                "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), role)
            )
    
    conn.commit()
    conn.close()

# Rate limiting configuration
rate_limits = {
    "admin": {"requests": 50, "period": 3600},  # 50 requests per hour
    "user": {"requests": 20, "period": 3600}    # 20 requests per hour
}

# Store rate limit data in memory (could be moved to Redis in production)
rate_limit_data = {}

# Decorator for authentication
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# Decorator for admin only access
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required"}), 401
        
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE id = ?", (session['user_id'],))
        result = cursor.fetchone()
        conn.close()
        
        if not result or result[0] != 'admin':
            return jsonify({"error": "Admin privileges required"}), 403
        
        return f(*args, **kwargs)
    return decorated_function

# Decorator for rate limiting
def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required"}), 401
        
        user_id = session['user_id']
        
        # Get user's role
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT role FROM users WHERE id = ?", (user_id,))
        result = cursor.fetchone()
        conn.close()
        
        if not result:
            return jsonify({"error": "User not found"}), 404
        
        role = result[0]
        limit = rate_limits.get(role, rate_limits['user'])
        
        # Initialize rate limit data if not exists
        if user_id not in rate_limit_data:
            rate_limit_data[user_id] = []
        
        # Clean up old requests
        current_time = time.time()
        rate_limit_data[user_id] = [t for t in rate_limit_data[user_id] 
                                   if current_time - t < limit['period']]
        
        # Check if limit exceeded
        if len(rate_limit_data[user_id]) >= limit['requests']:
            return jsonify({
                "error": f"Rate limit exceeded. Maximum {limit['requests']} requests per {limit['period']//3600} hour(s).",
                "retry_after": limit['period'] - (current_time - rate_limit_data[user_id][0])
            }), 429
        
        # Add current request timestamp
        rate_limit_data[user_id].append(current_time)
        
        return f(*args, **kwargs)
    return decorated_function

def interpret_command(command, previous_commands=None):
    """
    Enhanced function to interpret human commands with context from previous commands.
    Improved to handle directional commands more logically.
    """
    # Define a more detailed system prompt with improved prompt engineering
    system_prompt = """You are an AI that converts natural language movement instructions into structured JSON commands for a 4-wheeled robot.

You MUST ONLY output valid JSON. No explanations, text, or markdown formatting.

Input: Natural language instructions for robot movement
Output: JSON object representing the commands

**Supported Movements:**
- Linear motion: Use "mode": "linear" with "direction": "forward" or "backward", with speed (m/s) and either distance (m) or time (s)
- Rotation: Use "mode": "rotate" with "direction": "left" or "right", with degrees and speed
- Arc movements: Use "mode": "arc" for curved paths with specified radius and direction
- Complex shapes: "square", "circle", "triangle", "rectangle", "spiral", "figure-eight"
- Sequential movements: Multiple commands in sequence

**Output Format:**
{
  "commands": [
    {
      "mode": "linear|rotate|arc|stop",
      "direction": "forward|backward|left|right",
      "speed": float,  // meters per second (0.1-2.0)
      "distance": float,  // meters (if applicable)
      "time": float,  // seconds (if applicable)
      "rotation": float,  // degrees (if applicable)
      "turn_radius": float,  // meters (for arc movements)
      "stop_condition": "time|distance|obstacle"  // when to stop
    },
    // Additional commands for sequences
  ],
  "description": "Brief human-readable description of what the robot will do"
}

**IMPORTANT RULES:**
1. For rotation movements:
   - Use "mode": "rotate" with "direction": "left" or "right"
   - Always specify a rotation value in degrees (default to 90 if not specified)
   - Always specify a reasonable speed (0.5-1.0 m/s is typical for rotation)
   - Use "stop_condition": "time" if time is specified, otherwise "rotation"

2. For linear movements:
   - Use "mode": "linear" with "direction": "forward" or "backward"
   - Never use "left" or "right" as direction for linear movements
   - For "go right" type instructions, interpret as "rotate right, then go forward"
   - For "go left quickly for 5 meters", interpret as "rotate left, then go forward for 5 meters"

3. For sequences:
   - Break each logical movement into its own command object
   - Make sure speeds match descriptions (e.g., "quickly" = 1.5-2.0 m/s, "slowly" = 0.3-0.7 m/s)

For shapes, break them down into appropriate primitive movements:
- Square: 4 forward movements with 90° right/left turns
- Circle: A series of short arcs that form a complete 360° path
- Triangle: 3 forward movements with 120° turns
- Rectangle: 2 pairs of different-length forward movements with 90° turns
- Figure-eight: Two connected circles in opposite directions

Always provide complete, valid JSON that a robot can execute immediately.
"""

    # User prompt with context
    user_prompt = f"Convert this command into a structured robot command: \"{command}\""
    
    # Add context from previous commands if available
    if previous_commands and len(previous_commands) > 0:
        recent_commands = previous_commands[-3:]  # Last 3 commands
        context = "Previous commands for context:\n" + "\n".join([
            f"- {cmd}" for cmd in recent_commands
        ])
        user_prompt = context + "\n\n" + user_prompt

    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",  # Changed from gpt-3.5-turbo to 4o-mini
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,  # Lower temperature for more consistent outputs
            response_format={"type": "json_object"}  # Ensure JSON response
        )

        raw_output = response.choices[0].message.content
        logger.info(f"Raw LLM output: {raw_output}")

        try:
            parsed_data = json.loads(raw_output)
            
            # Remove timestamp and sequence_type if present
            if "timestamp" in parsed_data:
                del parsed_data["timestamp"]
                
            if "sequence_type" in parsed_data:
                del parsed_data["sequence_type"]
            
            parsed_data["original_command"] = command
            
            # Validate the JSON structure
            if "commands" not in parsed_data:
                parsed_data["commands"] = [{
                    "mode": "stop",
                    "description": "Invalid command structure - missing commands array"
                }]
            
            return parsed_data
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {e}, raw output: {raw_output}")
            
            # Try to extract JSON from the response using regex - useful for debugging
            json_match = re.search(r'```json(.*?)```', raw_output, re.DOTALL)
            if json_match:
                try:
                    json_str = json_match.group(1).strip()
                    return json.loads(json_str)
                except:
                    pass
            
            # Fallback response if parsing fails
            return {
                "error": "Failed to parse response as JSON",
                "commands": [{
                    "mode": "stop",
                    "description": "Command parsing error - robot stopped"
                }],
                "description": "Error in command processing"  # Removed sequence_type
            }

    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return {
            "error": str(e),
            "commands": [{
                "mode": "stop",
                "description": "API error - robot stopped"
            }],
            "description": "Error in API communication"  # Removed sequence_type
        }

# HTML Templates
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Robot Control System</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; text-align: center; background-color: #1e293b; color: white; padding: 50px; }
        .login-container { max-width: 400px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        input, button { padding: 15px; font-size: 16px; margin: 10px 0; border-radius: 10px; border: none; width: 100%; box-sizing: border-box; }
        input { background-color: #475569; color: white; }
        button { background-color: #0284c7; color: white; cursor: pointer; transition: all 0.2s ease; }
        button:hover { background-color: #0ea5e9; transform: translateY(-2px); }
        .error { color: #f87171; margin-top: 10px; }
        .register-link { margin-top: 20px; color: #94a3b8; }
        .register-link a { color: #60a5fa; text-decoration: none; }
        .register-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Robot Control System</h1>
        <h2>Login</h2>
        <form action="/auth" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        <p class="error" id="errorMsg" style="display: none;"></p>
        <p class="register-link">Don't have an account? <a href="/register">Register</a></p>
    </div>
</body>
</html>
"""

REGISTER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Robot Control System - Register</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; text-align: center; background-color: #1e293b; color: white; padding: 50px; }
        .register-container { max-width: 400px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        input, button { padding: 15px; font-size: 16px; margin: 10px 0; border-radius: 10px; border: none; width: 100%; box-sizing: border-box; }
        input { background-color: #475569; color: white; }
        button { background-color: #0284c7; color: white; cursor: pointer; transition: all 0.2s ease; }
        button:hover { background-color: #0ea5e9; transform: translateY(-2px); }
        .error { color: #f87171; margin-top: 10px; }
        .login-link { margin-top: 20px; color: #94a3b8; }
        .login-link a { color: #60a5fa; text-decoration: none; }
        .login-link a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="register-container">
        <h1>Robot Control System</h1>
        <h2>Register</h2>
        <form action="/do_register" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <input type="password" name="confirm_password" placeholder="Confirm Password" required>
            <button type="submit">Register</button>
        </form>
        <p class="error" id="errorMsg" style="display: none;"></p>
        <p class="login-link">Already have an account? <a href="/">Login</a></p>
    </div>
</body>
</html>
"""

ROBOT_INTERFACE_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Robot Control Interface</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { 
            font-family: 'Segoe UI', sans-serif; 
            background-color: #0f172a; 
            color: white; 
            text-align: center; 
            padding: 20px;
            margin: 0;
        }
        .container {
            max-width: 900px;
            margin: auto;
        }
        .chatbox { 
            background: #1e293b; 
            padding: 30px; 
            border-radius: 20px; 
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3);
            margin-bottom: 20px;
        }
        input[type="text"] { 
            width: 80%; 
            padding: 15px; 
            font-size: 16px; 
            border-radius: 12px; 
            border: none; 
            margin: 10px 0;
            background-color: #334155;
            color: white;
        }
        button { 
            padding: 15px 20px; 
            font-size: 16px; 
            margin: 5px; 
            border-radius: 12px; 
            border: none; 
            background-color: #0284c7; 
            color: white; 
            cursor: pointer; 
            transition: all 0.2s ease;
        }
        button:hover { 
            background-color: #0ea5e9; 
            transform: translateY(-2px);
        }
        button:active {
            transform: translateY(0);
        }
        .btn-speak {
            background-color: #059669;
        }
        .btn-speak:hover {
            background-color: #10b981;
        }
        pre { 
            text-align: left; 
            background: #0f172a; 
            padding: 20px; 
            border-radius: 12px; 
            color: #f1f5f9; 
            overflow-x: auto; 
            margin-top: 20px; 
            font-size: 14px;
            white-space: pre-wrap;
        }
        .status-bar {
            display: flex;
            justify-content: space-between;
            background-color: #334155;
            padding: 10px 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            font-size: 14px;
        }
        #voiceStatus {
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            background-color: #475569;
        }
        #voiceStatus.listening {
            background-color: #059669;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% { opacity: 0.7; }
            50% { opacity: 1; }
            100% { opacity: 0.7; }
        }
        a.nav-link { 
            display: inline-block; 
            margin: 10px 5px; 
            color: #60a5fa; 
            text-decoration: none; 
            font-weight: bold;
            transition: color 0.2s ease;
        }
        a.nav-link:hover {
            color: #93c5fd;
        }
        a.logout { 
            color: #f87171; 
        }
        a.logout:hover {
            color: #ef4444;
        }
        .command-examples {
            text-align: left;
            background-color: #334155;
            padding: 15px;
            border-radius: 10px;
            margin-top: 20px;
            font-size: 14px;
        }
        .command-examples h3 {
            margin-top: 0;
        }
        .example {
            margin: 5px 0;
            cursor: pointer;
            padding: 5px;
            border-radius: 5px;
        }
        .example:hover {
            background-color: #475569;
        }
        .navigation {
            margin-top: 20px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="chatbox">
            <h1>Robot Control Interface</h1>
            
            <div class="status-bar">
                <span>Status: <span id="voiceStatus">Ready</span></span>
                <span>User: <strong>{{ username }}</strong> ({{ role }})</span>
            </div>
            
            <form id="commandForm" method="POST" action="/send_command">
                <input type="text" id="command" name="command" placeholder="Enter movement command..." required>
                <div>
                    <button type="button" class="btn-speak" onclick="manualStartListening()">Speak</button>
                    <button type="submit">Send Command</button>
                </div>
            </form>
            
            <div class="command-examples">
                <h3>Try these commands:</h3>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    Do a square with 1.5 meter sides
                </div>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    Go left for 3 seconds then go right quickly for 5 meters
                </div>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    Draw a circle with an area of 20 meters
                </div>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    make a star 
                </div>
            </div>
            
            <h2>Generated Robot Commands:</h2>
            <pre id="response">No command sent yet.</pre>
            
            <div class="navigation">
                <a class="nav-link" href="/command_history">Command History</a>
                {% if is_admin %}
                <a class="nav-link" href="/admin/dashboard">Admin Dashboard</a>
                {% endif %}
                <a class="nav-link logout" href="/logout">Logout</a>
            </div>
        </div>
    </div>

    <script>
    // Speech Recognition Setup
    let recognition = null;
    const triggerPhrases = ["hey robot", "okay robot", "robot", "hey bot"];
    let isListeningForCommand = false;
    let commandTimeout = null;

    function initSpeechRecognition() {
        if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
            alert("Speech recognition not supported. Try Chrome, Edge, or Safari.");
            return;
        }

        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        recognition = new SpeechRecognition();
        
        // Configure recognition
        recognition.continuous = false;
        recognition.interimResults = true;
        recognition.lang = 'en-US';
        
        // Process speech results
        recognition.onresult = function(event) {
            const lastResult = event.results[event.results.length - 1];
            const transcript = lastResult[0].transcript.trim().toLowerCase();
            
            console.log(`Heard: "${transcript}" (Confidence: ${lastResult[0].confidence.toFixed(2)})`);
            
            if (isListeningForCommand) {
                document.getElementById('command').value = transcript;
                
                if (lastResult.isFinal) {
                    document.getElementById('voiceStatus').textContent = "Processing command...";
                    document.getElementById('commandForm').requestSubmit();
                    recognition.stop();
                    isListeningForCommand = false;
                }
            }
        };
        
        // Handle errors
        recognition.onerror = function(event) {
            console.log("Speech recognition error:", event.error);
            document.getElementById('voiceStatus').textContent = "Voice recognition error";
            recognition.stop();
            isListeningForCommand = false;
        };
        
        // Handle end of recognition
        recognition.onend = function() {
            if (isListeningForCommand) {
                document.getElementById('voiceStatus').textContent = "Ready";
                isListeningForCommand = false;
            }
        };
    }

    function manualStartListening() {
        if (!recognition) {
            initSpeechRecognition();
        }
        
        try {
            recognition.stop();
        } catch (e) {}
        
        setTimeout(() => {
            isListeningForCommand = true;
            document.getElementById('voiceStatus').textContent = "Listening for command...";
            document.getElementById('voiceStatus').className = "listening";
            
            commandTimeout = setTimeout(() => {
                if (isListeningForCommand) {
                    recognition.stop();
                    document.getElementById('voiceStatus').textContent = "Ready";
                    document.getElementById('voiceStatus').className = "";
                    isListeningForCommand = false;
                }
            }, 5000);
            
            recognition.start();
        }, 200);
    }

    // Initialize speech recognition and form submission
    document.addEventListener('DOMContentLoaded', function() {
        initSpeechRecognition();
        
        document.getElementById('commandForm').addEventListener('submit', function(event) {
            event.preventDefault();
            let formData = new FormData(this);
            
            fetch('/send_command', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (!response.ok) {
                    throw new Error(`Server returned ${response.status}: ${response.statusText}`);
                }
                return response.json();
            })
            .then(data => {
                const output = JSON.stringify(data, null, 4);
                document.getElementById('response').textContent = output;
                document.getElementById('voiceStatus').textContent = "Ready";
                document.getElementById('voiceStatus').className = "";
            })
            .catch(error => {
                document.getElementById('response').textContent = "⚠️ Error: " + error.message;
                document.getElementById('voiceStatus').textContent = "Error";
                document.getElementById('voiceStatus').className = "";
            });
        });
    });
    </script>
</body>
</html>
"""

COMMAND_HISTORY_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Command History</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { 
            font-family: 'Segoe UI', sans-serif; 
            background-color: #0f172a; 
            color: white; 
            text-align: center; 
            padding: 20px;
            margin: 0;
        }
        .container {
            max-width: 900px;
            margin: auto;
        }
        .content-box { 
            background: #1e293b; 
            padding: 30px; 
            border-radius: 20px; 
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3);
            margin-bottom: 20px;
            text-align: left;
        }
        h1, h2 {
            text-align: center;
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            font-size: 14px;
        }
        th, td {
            padding: 10px;
            border-bottom: 1px solid #334155;
        }
        th {
            background-color: #334155;
            text-align: left;
        }
        tr:hover {
            background-color: #334155;
        }
        .timestamp {
            font-size: 12px;
            color: #94a3b8;
        }
        .navigation {
            margin-top: 20px;
            text-align: center;
        }
        a.nav-link { 
            display: inline-block; 
            margin: 10px 5px; 
            color: #60a5fa; 
            text-decoration: none; 
            font-weight: bold;
            transition: color 0.2s ease;
        }
        a.nav-link:hover {
            color: #93c5fd;
        }
        .view-button {
            background-color: #0284c7;
            color: white;
            border: none;
            padding: 5px 10px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 12px;
        }
        .view-button:hover {
            background-color: #0ea5e9;
        }
        .modal {
            display: none;
            position: fixed;
            z-index: 1;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            overflow: auto;
            background-color: rgba(0,0,0,0.7);
        }
        .modal-content {
            background-color: #1e293b;
            margin: 10% auto;
            padding: 20px;
            border-radius: 10px;
            max-width: 700px;
            max-height: 80vh;
            overflow-y: auto;
        }
        .close {
            color: #aaa;
            float: right;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
        }
        .close:hover {
            color: white;
        }
        pre {
            white-space: pre-wrap;
            background-color: #0f172a;
            padding: 10px;
            border-radius: 5px;
            overflow-x: auto;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="content-box">
            <h1>Command History</h1>
            
            <table>
                <thead>
                    <tr>
                        <th>Command</th>
                        <th>Timestamp</th>
                        <th>Action</th>
                    </tr>
                </thead>
                <tbody id="historyTable">
                    {% for command in command_history %}
                    <tr>
                        <td>{{ command.command }}</td>
                        <td class="timestamp">{{ command.timestamp }}</td>
                        <td><button class="view-button" onclick="showCommandDetails('{{ command.id }}')">View Details</button></td>
                    </tr>
                    {% endfor %}
                </tbody>
            </table>
            
            {% if not command_history %}
            <p style="text-align: center; margin-top: 20px; color: #94a3b8;">No commands found</p>
            {% endif %}
            
            <div class="navigation">
                <a class="nav-link" href="/home">Back to Control Interface</a>
            </div>
        </div>
    </div>
    
    <!-- Modal for command details -->
    <div id="commandModal" class="modal">
        <div class="modal-content">
            <span class="close" onclick="closeModal()">&times;</span>
            <h2>Command Details</h2>
            <div id="commandDetails"></div>
        </div>
    </div>
    
    <script>
        function showCommandDetails(commandId) {
            fetch(`/command_details/${commandId}`)
            .then(response => response.json())
            .then(data => {
                const details = document.getElementById('commandDetails');
                details.innerHTML = `
                    <p><strong>Original Command:</strong> ${data.command}</p>
                    <p><strong>Timestamp:</strong> ${data.timestamp}</p>
                    <h3>Parsed Command:</h3>
                    <pre>${JSON.stringify(JSON.parse(data.parsed_command), null, 2)}</pre>
                `;
                document.getElementById('commandModal').style.display = 'block';
            })
            .catch(error => {
                console.error('Error fetching command details:', error);
                alert('Error fetching command details');
            });
        }
        
        function closeModal() {
            document.getElementById('commandModal').style.display = 'none';
        }
        
        // Close modal when clicking outside of it
        window.onclick = function(event) {
            const modal = document.getElementById('commandModal');
            if (event.target == modal) {
                modal.style.display = 'none';
            }
        }
    </script>
</body>
</html>
"""

ADMIN_DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Admin Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { 
            font-family: 'Segoe UI', sans-serif; 
            background-color: #0f172a; 
            color: white; 
            text-align: center; 
            padding: 20px;
            margin: 0;
        }
        .container {
            max-width: 900px;
            margin: auto;
        }
        .content-box { 
            background: #1e293b; 
            padding: 30px; 
            border-radius: 20px; 
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3);
            margin-bottom: 20px;
        }
        h1, h2 {
            text-align: center;
        }
        .tab {
            overflow: hidden;
            background-color: #334155;
            border-radius: 10px;
            margin-bottom: 20px;
        }
        .tab button {
            background-color: inherit;
            float: left;
            border: none;
            outline: none;
            cursor: pointer;
            padding: 15px 20px;
            transition: 0.3s;
            color: white;
            font-size: 16px;
        }
        .tab button:hover {
            background-color: #475569;
        }
        .tab button.active {
            background-color: #0284c7;
        }
        .tabcontent {
            display: none;
            padding: 20px;
            background-color: #1e293b;
            border-radius: 10px;
            animation: fadeEffect 1s;
            text-align: left;
        }
        @keyframes fadeEffect {
            from {opacity: 0;}
            to {opacity: 1;}
        }
        table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            font-size: 14px;
        }
        th, td {
            padding: 10px;
            border-bottom: 1px solid #334155;
            text-align: left;
        }
        th {
            background-color: #334155;
        }
        tr:hover {
            background-color: #334155;
        }
        input, select {
            padding: 10px;
            margin: 5px 0;
            border-radius: 5px;
            border: none;
            background-color: #475569;
            color: white;
            width: 100%;
        }
        button {
            padding: 10px 15px;
            margin: 10px 0;
            border-radius: 5px;
            border: none;
            background-color: #0284c7;
            color: white;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        button:hover {
            background-color: #0ea5e9;
        }
        .danger {
            background-color: #ef4444;
        }
        .danger:hover {
            background-color: #dc2626;
        }
        .success {
            background-color: #10b981;
        }
        .success:hover {
            background-color: #059669;
        }
        .form-group {
            margin-bottom: 15px;
        }
        .navigation {
            margin-top: 20px;
        }
        a.nav-link { 
            display: inline-block; 
            margin: 10px 5px; 
            color: #60a5fa; 
            text-decoration: none; 
            font-weight: bold;
            transition: color 0.2s ease;
        }
        a.nav-link:hover {
            color: #93c5fd;
        }
        .action-button {
            padding: 5px 10px;
            margin: 0 2px;
            font-size: 12px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="content-box">
            <h1>Admin Dashboard</h1>
            
            <div class="tab">
                <button class="tablinks active" onclick="openTab(event, 'Users')">Users</button>
                <button class="tablinks" onclick="openTab(event, 'Commands')">Commands</button>
                <button class="tablinks" onclick="openTab(event, 'Settings')">Settings</button>
            </div>
            
            <!-- Users Tab -->
            <div id="Users" class="tabcontent" style="display: block;">
                <h2>User Management</h2>
                
                <h3>Add New User</h3>
                <form id="addUserForm" action="/admin/add_user" method="post">
                    <div class="form-group">
                        <input type="text" name="username" placeholder="Username" required>
                    </div>
                    <div class="form-group">
                        <input type="password" name="password" placeholder="Password" required>
                    </div>
                    <div class="form-group">
                        <select name="role" required>
                            <option value="user">User</option>
                            <option value="admin">Admin</option>
                        </select>
                    </div>
                    <button type="submit" class="success">Add User</button>
                </form>
                
                <h3>User List</h3>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Username</th>
                            <th>Role</th>
                            <th>Created</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for user in users %}
                        <tr>
                            <td>{{ user.id }}</td>
                            <td>{{ user.username }}</td>
                            <td>{{ user.role }}</td>
                            <td>{{ user.created_at }}</td>
                            <td>
                                <button class="action-button" onclick="editUser('{{ user.id }}', '{{ user.username }}', '{{ user.role }}')">Edit</button>
                                <button class="action-button danger" onclick="deleteUser('{{ user.id }}', '{{ user.username }}')">Delete</button>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            
            <!-- Commands Tab -->
            <div id="Commands" class="tabcontent">
                <h2>Command History</h2>
                
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>User</th>
                            <th>Command</th>
                            <th>Timestamp</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {% for cmd in all_commands %}
                        <tr>
                            <td>{{ cmd.id }}</td>
                            <td>{{ cmd.username }}</td>
                            <td>{{ cmd.command }}</td>
                            <td>{{ cmd.timestamp }}</td>
                            <td>
                                <button class="action-button" onclick="viewCommand('{{ cmd.id }}')">View</button>
                                <button class="action-button danger" onclick="deleteCommand('{{ cmd.id }}')">Delete</button>
                            </td>
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
            
            <!-- Settings Tab -->
            <div id="Settings" class="tabcontent">
                <h2>System Settings</h2>
                
                <h3>Rate Limits</h3>
                <form id="rateLimitForm" action="/admin/update_settings" method="post">
                    <div class="form-group">
                        <label>Admin requests per hour:</label>
                        <input type="number" name="admin_rate" value="{{ settings.admin_rate }}" min="1" required>
                    </div>
                    <div class="form-group">
                        <label>User requests per hour:</label>
                        <input type="number" name="user_rate" value="{{ settings.user_rate }}" min="1" required>
                    </div>
                    <button type="submit">Update Settings</button>
                </form>
                
                <h3>Database Management</h3>
                <button onclick="if(confirm('Are you sure? This will clear all command history.')) window.location.href='/admin/clear_commands';" class="danger">Clear Command History</button>
                <button onclick="if(confirm('Are you sure? This will reset the entire database!')) window.location.href='/admin/reset_database';" class="danger">Reset Database</button>
            </div>
            
            <div class="navigation">
                <a class="nav-link" href="/home">Back to Control Interface</a>
            </div>
        </div>
    </div>
    
    <script>
        function openTab(evt, tabName) {
            var i, tabcontent, tablinks;
            tabcontent = document.getElementsByClassName("tabcontent");
            for (i = 0; i < tabcontent.length; i++) {
                tabcontent[i].style.display = "none";
            }
            tablinks = document.getElementsByClassName("tablinks");
            for (i = 0; i < tablinks.length; i++) {
                tablinks[i].className = tablinks[i].className.replace(" active", "");
            }
            document.getElementById(tabName).style.display = "block";
            evt.currentTarget.className += " active";
        }
        
        function editUser(id, username, role) {
            const newRole = prompt(`Change role for ${username} (current: ${role})`, role);
            if (newRole && (newRole === 'admin' || newRole === 'user')) {
                window.location.href = `/admin/edit_user/${id}?role=${newRole}`;
            }
        }
        
        function deleteUser(id, username) {
            if (confirm(`Are you sure you want to delete user "${username}"?`)) {
                window.location.href = `/admin/delete_user/${id}`;
            }
        }
        
        function viewCommand(id) {
            window.location.href = `/command_details/${id}`;
        }
        
        function deleteCommand(id) {
            if (confirm('Are you sure you want to delete this command?')) {
                window.location.href = `/admin/delete_command/${id}`;
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
def login():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return LOGIN_HTML

@app.route('/register')
def register():
    return REGISTER_HTML

@app.route('/do_register', methods=['POST'])
def do_register():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    
    # Validate input
    if not username or not password:
        error_html = REGISTER_HTML.replace('<p class="error" id="errorMsg" style="display: none;"></p>', 
                                         '<p class="error" id="errorMsg">Username and password are required</p>')
        return error_html
    
    if password != confirm_password:
        error_html = REGISTER_HTML.replace('<p class="error" id="errorMsg" style="display: none;"></p>', 
                                         '<p class="error" id="errorMsg">Passwords do not match</p>')
        return error_html
    
    # Check if username already exists
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        error_html = REGISTER_HTML.replace('<p class="error" id="errorMsg" style="display: none;"></p>', 
                                         '<p class="error" id="errorMsg">Username already exists</p>')
        return error_html
    
    # Create new user
    hashed_password = generate_password_hash(password)
    cursor.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, hashed_password, 'user')
    )
    conn.commit()
    conn.close()
    
    # Redirect to login page with success message
    success_html = LOGIN_HTML.replace('<p class="error" id="errorMsg" style="display: none;"></p>', 
                                     '<p class="error" id="errorMsg" style="display: block; color: #10b981;">Registration successful! Please login.</p>')
    return success_html

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id, password, role FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    
    if user and check_password_hash(user[1], password):
        session['user_id'] = user[0]
        session['username'] = username
        session['role'] = user[2]
        return redirect(url_for('home'))
    else:
        error_html = LOGIN_HTML.replace('<p class="error" id="errorMsg" style="display: none;"></p>', 
                                       '<p class="error" id="errorMsg" style="display: block;">Invalid credentials</p>')
        return error_html

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    session.pop('role', None)
    return redirect(url_for('login'))

@app.route('/home')
@login_required
def home():
    # Replace the placeholders with the actual values
    html = ROBOT_INTERFACE_HTML.replace("{{ username }}", session.get('username', 'Unknown'))
    html = html.replace("{{ role }}", session.get('role', 'user'))
    
    # Add admin link if user is admin
    is_admin = session.get('role') == 'admin'
    if is_admin:
        html = html.replace("{% if is_admin %}", "")
        html = html.replace("{% endif %}", "")
    else:
        html = html.replace("{% if is_admin %}\n<a class=\"nav-link\" href=\"/admin/dashboard\">Admin Dashboard</a>\n{% endif %}", "")
    
    return html

@app.route('/send_command', methods=['POST'])
@login_required
@rate_limit
def send_command():
    try:
        command = request.form.get('command', '').strip()
        user_id = session.get('user_id')
        
        if not command:
            return jsonify({"error": "No command provided"})
        
        # Get user command history for context
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT command FROM command_history 
            WHERE user_id = ? 
            ORDER BY timestamp DESC 
            LIMIT 5
        """, (user_id,))
        previous_commands = [row[0] for row in cursor.fetchall()]
        
        # Interpret the command
        interpreted_command = interpret_command(command, previous_commands)
        
        # Store command in history
        cursor.execute("""
            INSERT INTO command_history (user_id, command, parsed_command) 
            VALUES (?, ?, ?)
        """, (user_id, command, json.dumps(interpreted_command)))
        conn.commit()
        conn.close()
        
        # Return the interpreted command
        return jsonify(interpreted_command)
    
    except Exception as e:
        logger.error(f"Error processing command: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/command_history')
@login_required
def command_history():
    user_id = session.get('user_id')
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT id, command, datetime(timestamp, 'localtime') as timestamp 
        FROM command_history 
        WHERE user_id = ? 
        ORDER BY timestamp DESC
    """, (user_id,))
    
    command_history = cursor.fetchall()
    conn.close()
    
    # Convert to list of dicts for Jinja template
    commands = [dict(cmd) for cmd in command_history]
    
    # Replace template variables
    html = COMMAND_HISTORY_HTML
    
    # Replace for loop in template with actual HTML
    if commands:
        rows_html = ""
        for cmd in commands:
            rows_html += f"""
            <tr>
                <td>{cmd['command']}</td>
                <td class="timestamp">{cmd['timestamp']}</td>
                <td><button class="view-button" onclick="showCommandDetails('{cmd['id']}')">View Details</button></td>
            </tr>
            """
        
        html = html.replace("{% for command in command_history %}\n                    <tr>\n                        <td>{{ command.command }}</td>\n                        <td class=\"timestamp\">{{ command.timestamp }}</td>\n                        <td><button class=\"view-button\" onclick=\"showCommandDetails('{{ command.id }}')\">View Details</button></td>\n                    </tr>\n                    {% endfor %}", rows_html)
    
    # Handle empty command history
    if not commands:
        html = html.replace("{% if not command_history %}\n            <p style=\"text-align: center; margin-top: 20px; color: #94a3b8;\">No commands found</p>\n            {% endif %}", "<p style=\"text-align: center; margin-top: 20px; color: #94a3b8;\">No commands found</p>")
    else:
        html = html.replace("{% if not command_history %}\n            <p style=\"text-align: center; margin-top: 20px; color: #94a3b8;\">No commands found</p>\n            {% endif %}", "")
    
    return html

@app.route('/command_details/<int:command_id>')
@login_required
def command_details(command_id):
    user_id = session.get('user_id')
    role = session.get('role')
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # If admin, can view any command. Otherwise, only user's own commands
    if role == 'admin':
        cursor.execute("""
            SELECT ch.id, ch.command, ch.parsed_command, 
                  datetime(ch.timestamp, 'localtime') as timestamp,
                  u.username
            FROM command_history ch
            JOIN users u ON ch.user_id = u.id
            WHERE ch.id = ?
        """, (command_id,))
    else:
        cursor.execute("""
            SELECT id, command, parsed_command, 
                  datetime(timestamp, 'localtime') as timestamp 
            FROM command_history 
            WHERE id = ? AND user_id = ?
        """, (command_id, user_id))
    
    command = cursor.fetchone()
    conn.close()
    
    if not command:
        return jsonify({"error": "Command not found or access denied"}), 404
    
    return jsonify(dict(command))

@app.route('/admin/dashboard')
@login_required
@admin_required
def admin_dashboard():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all users
    cursor.execute("""
        SELECT id, username, role, datetime(created_at, 'localtime') as created_at 
        FROM users 
        ORDER BY id
    """)
    users = [dict(user) for user in cursor.fetchall()]
    
    # Get all commands with usernames
    cursor.execute("""
        SELECT ch.id, ch.command, datetime(ch.timestamp, 'localtime') as timestamp, u.username 
        FROM command_history ch
        JOIN users u ON ch.user_id = u.id
        ORDER BY ch.timestamp DESC
    """)
    all_commands = [dict(cmd) for cmd in cursor.fetchall()]
    
    # Get current settings
    settings = {
        "admin_rate": rate_limits["admin"]["requests"],
        "user_rate": rate_limits["user"]["requests"]
    }
    
    conn.close()
    
    # Replace template variables
    html = ADMIN_DASHBOARD_HTML
    
    # Replace users table rows
    users_html = ""
    for user in users:
        users_html += f"""
        <tr>
            <td>{user['id']}</td>
            <td>{user['username']}</td>
            <td>{user['role']}</td>
            <td>{user['created_at']}</td>
            <td>
                <button class="action-button" onclick="editUser('{user['id']}', '{user['username']}', '{user['role']}')">Edit</button>
                <button class="action-button danger" onclick="deleteUser('{user['id']}', '{user['username']}')">Delete</button>
            </td>
        </tr>
        """
    
    html = html.replace("{% for user in users %}\n                        <tr>\n                            <td>{{ user.id }}</td>\n                            <td>{{ user.username }}</td>\n                            <td>{{ user.role }}</td>\n                            <td>{{ user.created_at }}</td>\n                            <td>\n                                <button class=\"action-button\" onclick=\"editUser('{{ user.id }}', '{{ user.username }}', '{{ user.role }}')\">Edit</button>\n                                <button class=\"action-button danger\" onclick=\"deleteUser('{{ user.id }}', '{{ user.username }}')\">Delete</button>\n                            </td>\n                        </tr>\n                        {% endfor %}", users_html)
    
    # Replace commands table rows
    commands_html = ""
    for cmd in all_commands:
        commands_html += f"""
        <tr>
            <td>{cmd['id']}</td>
            <td>{cmd['username']}</td>
            <td>{cmd['command']}</td>
            <td>{cmd['timestamp']}</td>
            <td>
                <button class="action-button" onclick="viewCommand('{cmd['id']}')">View</button>
                <button class="action-button danger" onclick="deleteCommand('{cmd['id']}')">Delete</button>
            </td>
        </tr>
        """
    
    html = html.replace("{% for cmd in all_commands %}\n                        <tr>\n                            <td>{{ cmd.id }}</td>\n                            <td>{{ cmd.username }}</td>\n                            <td>{{ cmd.command }}</td>\n                            <td>{{ cmd.timestamp }}</td>\n                            <td>\n                                <button class=\"action-button\" onclick=\"viewCommand('{{ cmd.id }}')\">View</button>\n                                <button class=\"action-button danger\" onclick=\"deleteCommand('{{ cmd.id }}')\">Delete</button>\n                            </td>\n                        </tr>\n                        {% endfor %}", commands_html)
    
    # Replace settings values
    html = html.replace("{{ settings.admin_rate }}", str(settings["admin_rate"]))
    html = html.replace("{{ settings.user_rate }}", str(settings["user_rate"]))
    
    return html

@app.route('/admin/add_user', methods=['POST'])
@login_required
@admin_required
def admin_add_user():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    role = request.form.get('role', 'user')
    
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Check if username already exists
    cursor.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cursor.fetchone():
        conn.close()
        return jsonify({"error": "Username already exists"}), 400
    
    # Create new user
    hashed_password = generate_password_hash(password)
    cursor.execute(
        "INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
        (username, hashed_password, role)
    )
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/edit_user/<int:user_id>')
@login_required
@admin_required
def admin_edit_user(user_id):
    new_role = request.args.get('role')
    
    if not new_role or new_role not in ['admin', 'user']:
        return jsonify({"error": "Invalid role"}), 400
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_user/<int:user_id>')
@login_required
@admin_required
def admin_delete_user(user_id):
    # Prevent self-deletion
    if user_id == session.get('user_id'):
        return jsonify({"error": "Cannot delete your own account"}), 400
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Delete user's command history first (foreign key constraint)
    cursor.execute("DELETE FROM command_history WHERE user_id = ?", (user_id,))
    
    # Delete the user
    cursor.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/delete_command/<int:command_id>')
@login_required
@admin_required
def admin_delete_command(command_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM command_history WHERE id = ?", (command_id,))
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/update_settings', methods=['POST'])
@login_required
@admin_required
def admin_update_settings():
    admin_rate = request.form.get('admin_rate', type=int)
    user_rate = request.form.get('user_rate', type=int)
    
    if not admin_rate or not user_rate or admin_rate < 1 or user_rate < 1:
        return jsonify({"error": "Invalid rate limit values"}), 400
    
    # Update rate limits (in memory for this example)
    rate_limits["admin"]["requests"] = admin_rate
    rate_limits["user"]["requests"] = user_rate
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/clear_commands')
@login_required
@admin_required
def admin_clear_commands():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("DELETE FROM command_history")
    conn.commit()
    conn.close()
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/reset_database')
@login_required
@admin_required
def admin_reset_database():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Drop tables
    cursor.execute("DROP TABLE IF EXISTS command_history")
    cursor.execute("DROP TABLE IF EXISTS users")
    conn.commit()
    conn.close()
    
    # Reinitialize database
    init_db()
    
    # Reset session and redirect to login
    session.clear()
    return redirect(url_for('login'))

# API endpoint for ESP32 robot
@app.route('/api/robot_command', methods=['GET', 'POST'])
def robot_command():
    # Simple authentication using API key instead of session-based auth
    api_key = request.headers.get('X-API-Key')
    if not api_key or api_key != os.getenv("API_KEY", "1234"):
        return jsonify({"error": "Invalid API key"}), 401
    
    # For GET requests, return the latest command for the robot
    if request.method == 'GET':
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get the most recent command
        cursor.execute("""
            SELECT ch.parsed_command
            FROM command_history ch
            JOIN users u ON ch.user_id = u.id
            WHERE u.username = 'robotics'
            ORDER BY ch.timestamp DESC
            LIMIT 1
        """)
        
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return jsonify(json.loads(result['parsed_command']))
        else:
            return jsonify({"error": "No commands available"}), 404
    
    # For POST requests, allow the ESP32 to send status updates
    elif request.method == 'POST':
        try:
            data = request.get_json()
            # Process status update from ESP32
            logger.info(f"Received status update from ESP32: {data}")
            
            # In a production system, you might want to store these updates in the database
            return jsonify({"status": "received"}), 200
        except Exception as e:
            logger.error(f"Error processing ESP32 status update: {str(e)}")
            return jsonify({"error": str(e)}), 400

def create_app():
    """Create and configure the Flask app for production"""
    init_db()
    return app

if __name__ == '__main__':
    # Initialize the database on startup
    init_db()
    
    # For development, otherwise use production WSGI server
    app.run(debug=True, host='0.0.0.0', port=5000)
