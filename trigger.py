from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import openai
import json
from dotenv import load_dotenv
import os
import time
import logging
import re
from functools import wraps

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your_secret_key")  # Better to use env variable on Render

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# Improved data structure for users (in production, use a proper database)
USERS = {
    "maen": {"password": "maen", "role": "admin"},
    "user1": {"password": "password1", "role": "user"},
    "robotics": {"password": "securepass", "role": "user"}
}

# Command history for audit and improved responses
command_history = {}

# Rate limiting configuration
rate_limits = {
    "admin": {"requests": 50, "period": 3600},  # 50 requests per hour
    "user": {"requests": 20, "period": 3600}    # 20 requests per hour
}

# Decorator for authentication
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# Decorator for rate limiting
def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = session.get('user')
        if not user or user not in USERS:
            return jsonify({"error": "Authentication required"}), 401
        
        # Get user's role and corresponding rate limit
        role = USERS[user].get('role', 'user')
        limit = rate_limits.get(role, rate_limits['user'])
        
        # Initialize history if not exists
        if user not in command_history:
            command_history[user] = []
        
        # Clean up old requests
        current_time = time.time()
        command_history[user] = [t for t in command_history[user] 
                                  if current_time - t < limit['period']]
        
        # Check if limit exceeded
        if len(command_history[user]) >= limit['requests']:
            return jsonify({
                "error": f"Rate limit exceeded. Maximum {limit['requests']} requests per {limit['period']//3600} hour(s).",
                "retry_after": limit['period'] - (current_time - command_history[user][0])
            }), 429
        
        # Add current request timestamp
        command_history[user].append(current_time)
        
        return f(*args, **kwargs)
    return decorated_function

def interpret_command(command, previous_commands=None):
    """
    Enhanced function to interpret human commands with context from previous commands.
    """
    # Define a more detailed system prompt
    system_prompt = """You are an AI that converts human movement instructions into structured JSON commands for a 4-wheeled robot.

You MUST only output valid JSON. No explanatory text or markdown formatting is allowed.

**Supported Movements:**
- Linear motion (forward, backward) with speed and distance/time
- Rotation (left, right) with degrees
- Arc movements with radius
- Complex shapes (squares, triangles, circles, etc.)
- Sequential commands

**JSON Output Format:**
{
  "commands": [
    {
      "mode": "linear|rotate|arc|stop",
      "direction": "forward|backward|left|right",
      "speed": float,  // meters per second
      "distance": float,  // meters (if applicable)
      "time": float,  // seconds (if applicable)
      "rotation": float,  // degrees (if applicable)
      "turn_radius": float,  // meters (if applicable)
      "stop_condition": "time|distance|obstacle"  // when to stop
    },
    // Additional commands for sequences
  ],
  "sequence_type": "parallel|sequential",
  "description": "Human-readable description of what the robot will do"
}

For complex shapes, break them down into a sequence of linear/rotation commands.
"""

    # User prompt with context
    user_prompt = f"Convert this command into a structured robot command: \"{command}\""
    
    # Add context from previous commands if available
    if previous_commands and len(previous_commands) > 0:
        context = "Previous commands for context:\n" + "\n".join([
            f"- {cmd}" for cmd in previous_commands[-3:]  # Last 3 commands
        ])
        user_prompt = context + "\n\n" + user_prompt

    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}  # Ensure JSON response
        )

        raw_output = response.choices[0].message.content
        logger.info(f"Raw LLM output: {raw_output}")

        try:
            parsed_data = json.loads(raw_output)
            
            # Add metadata
            parsed_data["timestamp"] = time.time()
            parsed_data["original_command"] = command
            
            return parsed_data
        except json.JSONDecodeError as e:
            logger.error(f"JSON parsing error: {e}, raw output: {raw_output}")
            
            # Try to extract JSON from the response using regex
            json_match = re.search(r'```json(.*?)```', raw_output, re.DOTALL)
            if json_match:
                try:
                    json_str = json_match.group(1).strip()
                    return json.loads(json_str)
                except:
                    pass
            
            return {
                "error": "Failed to parse response as JSON",
                "commands": [{
                    "mode": "stop",
                    "description": "Command parsing error - robot stopped"
                }]
            }

    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return {"error": str(e)}

# HTML Templates as strings for single-file deployment
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Robot Control Login</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; text-align: center; background-color: #1e293b; color: white; padding: 50px; }
        .login-container { max-width: 400px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        input, button { padding: 15px; font-size: 16px; margin: 10px 0; border-radius: 10px; border: none; width: 100%; box-sizing: border-box; }
        input { background-color: #475569; color: white; }
        button { background-color: #0284c7; color: white; cursor: pointer; transition: all 0.2s ease; }
        button:hover { background-color: #0ea5e9; transform: translateY(-2px); }
        .error { color: #f87171; margin-top: 10px; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>🤖 Robot Control</h1>
        <h2>Login</h2>
        <form action="/auth" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        {% if error %}
        <p class="error">{{ error }}</p>
        {% endif %}
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
        .robot-container {
            margin: 20px 0;
            position: relative;
        }
        #robotFace {
            width: 100px;
            height: 100px;
            background-color: #334155;
            border-radius: 50%;
            margin: 0 auto;
            position: relative;
            transition: all 0.3s ease;
        }
        #robotFace.active {
            background-color: #0ea5e9;
            box-shadow: 0 0 20px rgba(14, 165, 233, 0.7);
        }
        #robotFace::before,
        #robotFace::after {
            content: '';
            position: absolute;
            width: 20px;
            height: 20px;
            background-color: #0f172a;
            border-radius: 50%;
            top: 30px;
            transition: all 0.3s ease;
        }
        #robotFace::before {
            left: 25px;
        }
        #robotFace::after {
            right: 25px;
        }
        #robotFace.active::before,
        #robotFace.active::after {
            background-color: #ffffff;
        }
        .mouth {
            position: absolute;
            width: 40px;
            height: 10px;
            background-color: #0f172a;
            bottom: 25px;
            left: calc(50% - 20px);
            border-radius: 10px;
            transition: all 0.3s ease;
        }
        #robotFace.active .mouth {
            background-color: #ffffff;
            height: 3px;
            width: 30px;
            left: calc(50% - 15px);
        }
        #commandVisualizer {
            height: 150px;
            background-color: #1e293b;
            border-radius: 10px;
            position: relative;
            overflow: hidden;
            margin-top: 20px;
        }
        .robot {
            width: 40px;
            height: 40px;
            background-color: #0ea5e9;
            border-radius: 8px;
            position: absolute;
            top: calc(50% - 20px);
            left: calc(50% - 20px);
        }
        .move-forward {
            animation: moveForward 2s linear;
        }
        .move-backward {
            animation: moveBackward 2s linear;
        }
        .rotate-left {
            animation: rotateLeft 2s linear;
        }
        .rotate-right {
            animation: rotateRight 2s linear;
        }
        .arc-left {
            animation: arcLeft 3s linear;
        }
        .arc-right {
            animation: arcRight 3s linear;
        }
        @keyframes moveForward {
            from { transform: translateY(0); }
            to { transform: translateY(-60px); }
        }
        @keyframes moveBackward {
            from { transform: translateY(0); }
            to { transform: translateY(60px); }
        }
        @keyframes rotateLeft {
            from { transform: rotate(0deg); }
            to { transform: rotate(-360deg); }
        }
        @keyframes rotateRight {
            from { transform: rotate(0deg); }
            to { transform: rotate(360deg); }
        }
        @keyframes arcLeft {
            0% { transform: translate(0, 0) rotate(0deg); }
            100% { transform: translate(-60px, 0) rotate(-180deg); }
        }
        @keyframes arcRight {
            0% { transform: translate(0, 0) rotate(0deg); }
            100% { transform: translate(60px, 0) rotate(180deg); }
        }
        a.logout { 
            display: inline-block; 
            margin-top: 20px; 
            color: #f87171; 
            text-decoration: none; 
            font-weight: bold;
            transition: color 0.2s ease;
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
    </style>
    <script>
        // Improved Voice Recognition
        function initSpeechRecognition() {
            if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
                alert("Speech recognition not supported in this browser. Try Chrome, Edge, or Safari.");
                return;
            }

            const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
            const triggerPhrases = ["hey robot", "okay robot", "robot", "hey bot"];
            
            let recognition = new SpeechRecognition();
            let isListeningForTrigger = false;
            let isListeningForCommand = false;
            let commandTimeout = null;
            
            // Configure recognition
            recognition.continuous = true;
            recognition.interimResults = true;
            recognition.lang = 'en-US';
            
            // Update UI to show status
            function updateStatus(status) {
                const statusElement = document.getElementById('voiceStatus');
                statusElement.textContent = status;
                statusElement.className = status.includes('Listening') ? 'listening' : '';
            }
            
            // Process speech results
            recognition.onresult = function(event) {
                const lastResult = event.results[event.results.length - 1];
                const transcript = lastResult[0].transcript.trim().toLowerCase();
                
                // For debugging
                console.log(`🎤 Heard: "${transcript}" (Confidence: ${lastResult[0].confidence.toFixed(2)})`);
                
                if (isListeningForTrigger) {
                    // Check for trigger phrases
                    if (triggerPhrases.some(phrase => transcript.includes(phrase))) {
                        console.log("✅ Trigger phrase detected");
                        recognition.stop(); // Stop current session
                        
                        // Visual feedback
                        updateStatus("Listening for command...");
                        document.getElementById('robotFace').className = 'active';
                        
                        // Start listening for the actual command
                        setTimeout(() => {
                            isListeningForTrigger = false;
                            isListeningForCommand = true;
                            recognition.continuous = false; // Only get one command
                            recognition.start();
                            
                            // Set timeout for command (5 seconds)
                            commandTimeout = setTimeout(() => {
                                if (isListeningForCommand) {
                                    recognition.stop();
                                    resetToTriggerMode();
                                    updateStatus("Command timeout. Try again.");
                                }
                            }, 5000);
                        }, 300);
                    }
                } 
                else if (isListeningForCommand && !lastResult.isFinal) {
                    // Show intermediate results
                    document.getElementById('command').value = transcript;
                }
                else if (isListeningForCommand && lastResult.isFinal) {
                    // Final command received
                    clearTimeout(commandTimeout);
                    document.getElementById('command').value = transcript;
                    console.log("🎙️ Command received:", transcript);
                    
                    // Visual feedback
                    updateStatus("Processing command...");
                    
                    // Submit the form
                    document.getElementById('commandForm').requestSubmit();
                    
                    // Reset to trigger mode
                    resetToTriggerMode();
                }
            };
            
            // Reset to initial trigger word listening mode
            function resetToTriggerMode() {
                isListeningForCommand = false;
                isListeningForTrigger = true;
                recognition.continuous = true;
                document.getElementById('robotFace').className = '';
                updateStatus("Listening for trigger word...");
                
                // Restart recognition after a short delay
                setTimeout(() => {
                    try {
                        recognition.start();
                    } catch (e) {
                        console.log("Recognition already started, restarting...");
                        recognition.stop();
                        setTimeout(() => recognition.start(), 200);
                    }
                }, 300);
            }
            
            // Handle errors
            recognition.onerror = function(event) {
                console.log("⚠️ Speech recognition error:", event.error);
                if (event.error === 'no-speech') {
                    // No speech detected, just restart
                    recognition.stop();
                    resetToTriggerMode();
                } else {
                    // For other errors, wait a bit longer before restarting
                    updateStatus("Voice recognition error. Restarting...");
                    setTimeout(resetToTriggerMode, 2000);
                }
            };
            
            // Handle end of recognition
            recognition.onend = function() {
                if (isListeningForTrigger) {
                    // If we're supposed to be listening for the trigger but recognition ended,
                    // restart it after a short delay
                    setTimeout(() => {
                        try {
                            recognition.start();
                        } catch (e) {
                            console.log("Recognition already started");
                        }
                    }, 200);
                }
            };
            
            // Initial start
            updateStatus("Listening for trigger word...");
            try {
                recognition.start();
                isListeningForTrigger = true;
            } catch (e) {
                console.error("Failed to start speech recognition:", e);
                updateStatus("Failed to start voice recognition");
            }
            
            // Expose functions
            window.manualStartListening = function() {
                recognition.stop();
                isListeningForTrigger = false;
                isListeningForCommand = true;
                updateStatus("Listening for command...");
                document.getElementById('robotFace').className = 'active';
                
                // Set timeout for command
                commandTimeout = setTimeout(() => {
                    if (isListeningForCommand) {
                        recognition.stop();
                        resetToTriggerMode();
                    }
                }, 7000);
                
                setTimeout(() => {
                    recognition.continuous = false;
                    recognition.start();
                }, 200);
            };
        }

        // Initialize when the page loads
        document.addEventListener('DOMContentLoaded', function() {
            initSpeechRecognition();
            
            // Handle form submission
            document.getElementById('commandForm').addEventListener('submit', function(event) {
                event.preventDefault();
                let formData = new FormData(this);
                
                // Update UI
                document.getElementById('responseStatus').textContent = "Processing...";
                
                fetch('/send_command', {
                    method: 'POST',
                    body: formData
                })
                .then(response => response.json())
                .then(data => {
                    const output = JSON.stringify(data, null, 4);
                    document.getElementById('response').textContent = output;
                    document.getElementById('responseStatus').textContent = "Command received";
                    
                    // Visualize the command (simple animation based on command type)
                    visualizeCommand(data);
                })
                .catch(error => {
                    document.getElementById('response').textContent = "⚠️ Error: " + error;
                    document.getElementById('responseStatus').textContent = "Error";
                });
            });
        });

        // Simple visualization of the command
        function visualizeCommand(command) {
            const visualizer = document.getElementById('commandVisualizer');
            visualizer.innerHTML = ''; // Clear previous visualization
            
            const robot = document.createElement('div');
            robot.className = 'robot';
            visualizer.appendChild(robot);
            
            // Different animations based on command type
            if (command.commands && command.commands.length > 0) {
                const firstCommand = command.commands[0];
                
                if (firstCommand.mode === 'linear') {
                    robot.classList.add('move-' + firstCommand.direction);
                    robot.style.animationDuration = (firstCommand.time || 2) + 's';
                } 
                else if (firstCommand.mode === 'rotate') {
                    robot.classList.add('rotate-' + firstCommand.direction);
                    robot.style.animationDuration = (firstCommand.time || 2) + 's';
                }
                else if (firstCommand.mode === 'arc') {
                    robot.classList.add('arc-' + firstCommand.direction);
                    robot.style.animationDuration = (firstCommand.time || 3) + 's';
                }
                else if (firstCommand.mode === 'stop') {
                    robot.classList.add('stop');
                }
            } else if (command.mode) {
                // Handle legacy format
                if (command.mode === 'linear') {
                    robot.classList.add('move-' + command.direction);
                    robot.style.animationDuration = (command.time || 2) + 's';
                } 
                else if (command.mode === 'rotate') {
                    robot.classList.add('rotate-' + command.direction);
                    robot.style.animationDuration = (command.time || 2) + 's';
                }
                else if (command.mode === 'arc') {
                    robot.classList.add('arc-' + command.direction);
                    robot.style.animationDuration = (command.time || 3) + 's';
                }
                else if (command.mode === 'stop') {
                    robot.classList.add('stop');
                }
            }
        }
    </script>
</head>
<body>
    <div class="container">
        <div class="chatbox">
            <h1>🤖 Robot Control Interface</h1>
            
            <div class="status-bar">
                <span>Status: <span id="voiceStatus">Initializing...</span></span>
                <span>User: <strong>{{ username }}</strong></span>
            </div>
            
            <div class="robot-container">
                <div id="robotFace">
                    <div class="mouth"></div>
                </div>
            </div>
            
            <form id="commandForm" method="POST" action="/send_command">
                <input type="text" id="command" name="command" placeholder="Enter movement command or say 'Hey Robot'..." required>
                <div>
                    <button type="button" class="btn-speak" onclick="window.manualStartListening()">🎤 Speak</button>
                    <button type="submit">Send Command</button>
                </div>
            </form>
            
            <div class="command-examples">
                <h3>Try these commands:</h3>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    Draw a square with 1 meter sides
                </div>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    Move forward for 2 seconds at 0.5 meters per second
                </div>
                <div class="example" onclick="document.getElementById('command').value=this.textContent;document.getElementById('commandForm').requestSubmit()">
                    Turn right 90 degrees then move forward 1 meter
                </div>
            </div>
            
            <h2>🧠 Command Visualization:</h2>
            <div id="commandVisualizer"></div>
            
            <h2>🔍 Generated Robot Commands: <span id="responseStatus"></span></h2>
            <pre id="response">No command sent yet.</pre>
            
            <a class="logout" href="/logout">🚪 Logout</a>
        </div>
    </div>
</body>
</html>
"""

# Route handlers
@app.route('/')
def login():
    if 'user' in session:
        return redirect(url_for('home'))
    return LOGIN_HTML

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    if username in USERS and USERS[username]["password"] == password:
        session['user'] = username
        return redirect(url_for('home'))
    else:
        return LOGIN_HTML.replace("{% if error %}", "").replace("{% endif %}", "").replace("{{ error }}", "Invalid credentials")

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/home')
@login_required
def home():
    # Replace the username placeholder with the actual username
    return ROBOT_INTERFACE_HTML.replace("{{ username }}", session['user'])

@app.route('/send_command', methods=['POST'])
@login_required
@rate_limit
def send_command():
    command = request.form.get('command', '').strip()
    user = session.get('user')
    
    if not command:
        return jsonify({"error": "No command provided"})
    
    # Get user command history
    user_commands = []
    if user in command_history:
        user_commands = [entry.get("original_command", "") for entry in command_history[user] 
                          if isinstance(entry, dict) and "original_command" in entry]
    
    # Process the command
    response = interpret_command(command, user_commands)
    
    # Store command in history
    if user not in command_history:
        command_history[user] = []
    
    command_history[user].append({
        "timestamp": time.time(),
        "original_command": command,
        "response": response
    })
    
    return jsonify(response)

if __name__ == '__main__':
    # Configure host and port for Render deployment
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)  # Use debug=False for production
