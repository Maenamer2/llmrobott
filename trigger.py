from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import openai
import json
from dotenv import load_dotenv
import os
import time
import logging
import re
from functools import wraps
import base64
import hashlib

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
    "maen": {"password": "maen", "role": "admin", "voice_print": None},
    "user1": {"password": "password1", "role": "user", "voice_print": None},
    "robotics": {"password": "securepass", "role": "user", "voice_print": None}
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
            return jsonify({"error": "Authentication required"}), 401
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
                                 if isinstance(t, float) and current_time - t < limit['period']]
        
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

def compare_voice_prints(voice_print1, voice_print2, threshold=0.85):
    """
    Compare two voice prints (simplified implementation)
    In a real-world scenario, you would use a proper voice recognition API
    or machine learning model for this comparison
    """
    if not voice_print1 or not voice_print2:
        return False
    
    # Simple string comparison for demonstration
    # In production, use a proper voice biometric system
    similarity = 1.0 if voice_print1 == voice_print2 else 0.0
    
    return similarity >= threshold

def process_voice_auth(username, voice_data):
    """
    Process voice authentication for a user
    """
    try:
        if username not in USERS:
            return False, "User not found"
        
        # Process audio data (base64 encoded)
        # This is a simplified version - in a real system we would:
        # 1. Convert audio to a standardized format
        # 2. Extract voice features using techniques like MFCC
        # 3. Compare against stored voiceprint using a model
        
        # For demo purposes, we'll just hash the audio data as a "voice print"
        voice_hash = hashlib.sha256(voice_data.encode()).hexdigest()
        
        # If user doesn't have a voice print yet, save this one
        if not USERS[username]["voice_print"]:
            USERS[username]["voice_print"] = voice_hash
            return True, "Voice print registered"
            
        # Compare with stored voice print
        if compare_voice_prints(USERS[username]["voice_print"], voice_hash):
            return True, "Voice authentication successful"
        else:
            return False, "Voice authentication failed"
            
    except Exception as e:
        logger.error(f"Voice authentication error: {str(e)}")
        return False, f"Error processing voice authentication: {str(e)}"

# HTML Templates
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
        .success { color: #4ade80; margin-top: 10px; }
        .tabs { display: flex; margin-bottom: 20px; }
        .tab { flex: 1; padding: 10px; background-color: #475569; cursor: pointer; }
        .tab.active { background-color: #0284c7; }
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        .voice-auth-container { margin-top: 20px; padding-top: 20px; border-top: 1px solid #4b5563; }
        .voice-status { font-size: 14px; margin: 10px 0; }
        .record-btn { background-color: #ef4444; display: inline-flex; align-items: center; justify-content: center; }
        .record-btn.recording { animation: pulse 1.5s infinite; }
        .record-btn svg { margin-right: 8px; }
        .voice-phrase { font-size: 18px; background-color: #475569; padding: 15px; border-radius: 10px; margin: 15px 0; }
        @keyframes pulse {
            0% { opacity: 0.7; }
            50% { opacity: 1; }
            100% { opacity: 0.7; }
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1> Robot Control</h1>
        <h2>Login</h2>
        
        <div class="tabs">
            <div class="tab active" onclick="switchTab('password')">Password</div>
            <div class="tab" onclick="switchTab('voice')">Voice</div>
        </div>
        
        <!-- Password Login Tab -->
        <div id="password-tab" class="tab-content active">
            <form id="password-form" action="/auth" method="post">
                <input type="text" name="username" id="username" placeholder="Username" required>
                <input type="password" name="password" placeholder="Password" required>
                <button type="submit">Login</button>
            </form>
        </div>
        
        <!-- Voice Login Tab -->
        <div id="voice-tab" class="tab-content">
            <form id="voice-form" action="/voice_auth" method="post">
                <input type="text" name="username" id="voice-username" placeholder="Username" required>
                <div class="voice-phrase">
                    Say: "My voice is my passport, verify me"
                </div>
                <div class="voice-status" id="voice-status">Ready to record</div>
                <button type="button" id="record-btn" class="record-btn" onclick="toggleRecording()">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                        <circle cx="12" cy="12" r="6"></circle>
                    </svg>
                    Record Voice
                </button>
                <input type="hidden" name="voice_data" id="voice-data">
                <button type="submit" id="voice-submit" disabled>Login with Voice</button>
            </form>
        </div>
        
        <p class="error" id="errorMsg" style="display: none;"></p>
        <p class="success" id="successMsg" style="display: none;"></p>
    </div>

    <script>
        // Tab switching functionality
        function switchTab(tabName) {
            document.querySelectorAll('.tab').forEach(tab => {
                tab.classList.remove('active');
            });
            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });
            
            document.querySelector(`.tab:nth-child(${tabName === 'password' ? 1 : 2})`).classList.add('active');
            document.getElementById(`${tabName}-tab`).classList.add('active');
            
            // Clear error messages when switching tabs
            document.getElementById('errorMsg').style.display = 'none';
            document.getElementById('successMsg').style.display = 'none';
            
            // Sync usernames between tabs
            if (tabName === 'voice') {
                document.getElementById('voice-username').value = document.getElementById('username').value;
            } else {
                document.getElementById('username').value = document.getElementById('voice-username').value;
            }
        }
        
        // Voice recording functionality
        let mediaRecorder;
        let audioChunks = [];
        let isRecording = false;
        
        async function setupMediaRecorder() {
            try {
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                mediaRecorder = new MediaRecorder(stream);
                
                mediaRecorder.addEventListener('dataavailable', event => {
                    audioChunks.push(event.data);
                });
                
                mediaRecorder.addEventListener('stop', () => {
                    const audioBlob = new Blob(audioChunks, { type: 'audio/wav' });
                    const reader = new FileReader();
                    
                    reader.onload = () => {
                        const base64Audio = reader.result.split(',')[1];
                        document.getElementById('voice-data').value = base64Audio;
                        document.getElementById('voice-status').textContent = 'Recording complete';
                        document.getElementById('voice-submit').disabled = false;
                    };
                    
                    reader.readAsDataURL(audioBlob);
                });
                
                document.getElementById('voice-status').textContent = 'Microphone ready';
            } catch (err) {
                document.getElementById('voice-status').textContent = `Error: ${err.message}`;
                document.getElementById('record-btn').disabled = true;
            }
        }
        
        function toggleRecording() {
            if (isRecording) {
                mediaRecorder.stop();
                isRecording = false;
                document.getElementById('record-btn').textContent = 'Record Again';
                document.getElementById('record-btn').classList.remove('recording');
            } else {
                audioChunks = [];
                mediaRecorder.start();
                isRecording = true;
                document.getElementById('voice-status').textContent = 'Recording...';
                document.getElementById('record-btn').textContent = 'Stop Recording';
                document.getElementById('record-btn').classList.add('recording');
                document.getElementById('voice-submit').disabled = true;
                
                // Auto-stop after 5 seconds
                setTimeout(() => {
                    if (isRecording) {
                        toggleRecording();
                    }
                }, 5000);
            }
        }
        
        // Initialize voice recording when switching to voice tab
        document.querySelector('.tab:nth-child(2)').addEventListener('click', () => {
            if (!mediaRecorder) {
                setupMediaRecorder();
            }
        });
        
        // Handle voice authentication form submission
        document.getElementById('voice-form').addEventListener('submit', function(e) {
            e.preventDefault();
            
            const formData = new FormData(this);
            const username = formData.get('username');
            const voiceData = formData.get('voice_data');
            
            if (!username || !voiceData) {
                document.getElementById('errorMsg').textContent = 'Username and voice recording are required';
                document.getElementById('errorMsg').style.display = 'block';
                return;
            }
            
            fetch('/voice_auth', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    username: username,
                    voice_data: voiceData
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('successMsg').textContent = data.message;
                    document.getElementById('successMsg').style.display = 'block';
                    document.getElementById('errorMsg').style.display = 'none';
                    
                    // Redirect to home page after successful login
                    setTimeout(() => {
                        window.location.href = '/home';
                    }, 1000);
                } else {
                    document.getElementById('errorMsg').textContent = data.message;
                    document.getElementById('errorMsg').style.display = 'block';
                    document.getElementById('successMsg').style.display = 'none';
                }
            })
            .catch(error => {
                document.getElementById('errorMsg').textContent = 'An error occurred. Please try again.';
                document.getElementById('errorMsg').style.display = 'block';
                console.error('Error:', error);
            });
        });
    </script>
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
            transition: all 0.5s ease;
        }
        #robotFace.active {
            background-color: #0ea5e9;
            box-shadow: 0 0 20px rgba(14, 165, 233, 0.7);
        }
        #robotFace.listening {
            background-color: #10b981;
            box-shadow: 0 0 20px rgba(16, 185, 129, 0.7);
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
            transition: all 0.5s ease;
        }
        #robotFace::before {
            left: 25px;
        }
        #robotFace::after {
            right: 25px;
        }
        #robotFace.active::before,
        #robotFace.active::after,
        #robotFace.listening::before,
        #robotFace.listening::after {
            background-color: #ffffff;
            width: 22px;
            height: 22px;
        }
        .mouth {
            position: absolute;
            width: 40px;
            height: 10px;
            background-color: #0f172a;
            bottom: 25px;
            left: calc(50% - 20px);
            border-radius: 10px;
            transition: all 0.5s ease;
        }
        #robotFace.active .mouth,
        #robotFace.listening .mouth {
            background-color: #ffffff;
            height: 15px;
            width: 40px;
            left: calc(50% - 20px);
            border-radius: 0 0 20px 20px;
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
</head>
<body>
    <div class="container">
        <div class="chatbox">
            <h1> Robot Control Interface</h1>
            
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
                    <button type="button" class="btn-speak" onclick="manualStartListening()"> Speak</button>
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
            
            <h2> Generated Robot Commands: <span id="responseStatus"></span></h2>
            <pre id="response">No command sent yet.</pre>
            
            <a class="logout" href="/logout"> Logout</a>
        </div>
    </div>

    <script>
    // Speech Recognition Setup
    let recognition = null;
    const triggerPhrases = ["hey robot", "okay robot", "robot", "hey bot"];
    let isListeningForTrigger = false;
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
        recognition.continuous = true;
        recognition.interimResults = true;
        recognition.lang = 'en-US';
        
        // Update UI to show status
        function updateStatus(status) {
            const statusElement = document.getElementById('voiceStatus');
            const robotFace = document.getElementById('robotFace');
            
            statusElement.textContent = status;
            statusElement.className = status.includes('Listening') ? 'listening' : '';
            
            if (status.includes('Listening for trigger')) {
                robotFace.className = '';
            } else if (status.includes('Listening for command')) {
                robotFace.className = 'listening';
            } else if (status.includes('Processing')) {
                robotFace.className = 'active';
            }
             }
        
        // Process speech results
        recognition.onresult = function(event) {
            const lastResult = event.results[event.results.length - 1];
            const transcript = lastResult[0].transcript.trim().toLowerCase();
            
            console.log(` Heard: "${transcript}" (Confidence: ${lastResult[0].confidence.toFixed(2)})`);
            
            if (isListeningForTrigger) {
                // Check for trigger phrases
                if (triggerPhrases.some(phrase => transcript.includes(phrase))) {
                    recognition.stop();
                    updateStatus("Listening for command...");
                    
                    setTimeout(() => {
                        isListeningForTrigger = false;
                        isListeningForCommand = true;
                        recognition.continuous = false;
                        recognition.start();
                        
                        commandTimeout = setTimeout(() => {
                            if (isListeningForCommand) {
                                recognition.stop();
                                resetToTriggerMode();
                                updateStatus("No command heard. Try again.");
                            }
                        }, 5000);
                    }, 300);
                }
            } 
            else if (isListeningForCommand && !lastResult.isFinal) {
                document.getElementById('command').value = transcript;
            }
            else if (isListeningForCommand && lastResult.isFinal) {
                clearTimeout(commandTimeout);
                document.getElementById('command').value = transcript;
                
                updateStatus("Processing command...");
                document.getElementById('commandForm').requestSubmit();
                resetToTriggerMode();
            }
        };
        
        // Reset to trigger word listening mode
        function resetToTriggerMode() {
            isListeningForCommand = false;
            isListeningForTrigger = true;
            recognition.continuous = true;
            updateStatus("Listening for trigger word...");
            
            setTimeout(() => {
                try {
                    recognition.start();
                } catch (e) {
                    console.log("Recognition restart error:", e);
                }
            }, 300);
        }
        
        // Handle errors
        recognition.onerror = function(event) {
            console.log("⚠️ Speech recognition error:", event.error);
            if (event.error === 'no-speech' || event.error === 'network') {
                recognition.stop();
                resetToTriggerMode();
            } else {
                updateStatus("Voice recognition error. Restarting...");
                setTimeout(resetToTriggerMode, 2000);
            }
        };
        
        // Handle end of recognition
        recognition.onend = function() {
            if (isListeningForTrigger) {
                setTimeout(() => {
                    try {
                        recognition.start();
                    } catch (e) {
                        console.log("Recognition start error:", e);
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
    }

    function manualStartListening() {
        if (!recognition) return;
        
        recognition.stop();
        isListeningForTrigger = false;
        isListeningForCommand = true;
        document.getElementById('voiceStatus').textContent = "Listening for command...";
        document.getElementById('robotFace').className = 'listening';
        
        commandTimeout = setTimeout(() => {
            if (isListeningForCommand) {
                recognition.stop();
                resetToTriggerMode();
                document.getElementById('voiceStatus').textContent = "No command heard. Try again.";
            }
        }, 5000);
        
        setTimeout(() => {
            recognition.continuous = false;
            recognition.start();
        }, 200);
    }

    // Initialize speech recognition and form submission
    document.addEventListener('DOMContentLoaded', function() {
        initSpeechRecognition();
        
        document.getElementById('commandForm').addEventListener('submit', function(event) {
            event.preventDefault();
            let formData = new FormData(this);
            
            document.getElementById('responseStatus').textContent = "Processing...";
            
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
                document.getElementById('responseStatus').textContent = "Command received";
            })
            .catch(error => {
                document.getElementById('response').textContent = "⚠️ Error: " + error.message;
                document.getElementById('responseStatus').textContent = "Error";
            });
        });
    });
    </script>
</body>
</html>
"""

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
        error_html = LOGIN_HTML.replace('<p class="error" id="errorMsg" style="display: none;"></p>', 
                                         '<p class="error" id="errorMsg">Invalid credentials</p>')
        return error_html

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
    try:
        command = request.form.get('command', '').strip()
        user = session.get('user')
        
        if not command:
            return jsonify({"error": "No command provided"})
        
        # Get user command history for context (only command strings)
        user_commands = []
        if user in command_history:
            # Extract original commands from command objects
            user_commands = [
                item["original_command"] for item in command_history[user] 
                if isinstance(item, dict) and "original_command" in item
            ]
        
        # Interpret the command
        interpreted_command = interpret_command(command, user_commands)
        
        # Store command in history
        if user not in command_history:
            command_history[user] = []
        command_history[user].append(interpreted_command)
        
        # Limit command history to last 10 commands
        if len(command_history[user]) > 10:
            command_history[user] = command_history[user][-10:]
        
        # Return the interpreted command
        return jsonify(interpreted_command)
    
    except Exception as e:
        logger.error(f"Error processing command: {str(e)}")
        return jsonify({"error": str(e)}), 500

# New endpoint for ESP32 communication
@app.route('/api/robot_command', methods=['GET', 'POST'])
def robot_command():
    # Simple authentication using API key instead of session-based auth
    api_key = request.headers.get('X-API-Key')
    if not api_key or api_key != '1234' :
        return jsonify({"error": "Invalid API key"}), 401
    
    # For GET requests, return the latest command for the robot
    if request.method == 'GET':
        # This could be the most recent command in your system
        if 'robotics' in command_history and command_history['robotics']:
            # Find the most recent valid command
            for item in reversed(command_history['robotics']):
                if isinstance(item, dict) and "commands" in item:
                    return jsonify(item)
            
        return jsonify({"error": "No commands available"}), 404
    
    # For POST requests, allow the ESP32 to send status updates
    elif request.method == 'POST':
        try:
            data = request.get_json()
            # Process status update from ESP32
            logger.info(f"Received status update from ESP32: {data}")
            
            # Store the status update if needed
            if 'status' in data and 'commandId' in data:
                status_update = {
                    "timestamp": time.time(),
                    "status": data['status'],
                    "commandId": data['commandId']
                }
                
                # You could store this in a database or in memory
                if 'esp32_status' not in command_history:
                    command_history['esp32_status'] = []
                
                command_history['esp32_status'].append(status_update)
                
                # Keep only the last 20 status updates
                if len(command_history['esp32_status']) > 20:
                    command_history['esp32_status'] = command_history['esp32_status'][-20:]
            
            return jsonify({"status": "received"}), 200
        except Exception as e:
            logger.error(f"Error processing ESP32 status update: {str(e)}")
            return jsonify({"error": str(e)}), 400

if __name__ == '__main__':
    # For development, otherwise use production WSGI server
    app.run(debug=True, host='0.0.0.0', port=5000)

