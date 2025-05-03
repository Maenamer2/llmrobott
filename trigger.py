from flask import Flask, render_template, request, jsonify, redirect, url_for, session, send_from_directory
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
app.secret_key = os.getenv("SECRET_KEY", "your_secret_key")

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# User voice profiles with voice patterns (would be securely stored in a real app)
VOICE_PROFILES = {
    "aya": {"passphrase": "my name is aya", "confidence": 0.7},
    "maen": {"passphrase": "my name is maen", "confidence": 0.7},
    "tima": {"passphrase": "my name is tima", "confidence": 0.7},
    "layan": {"passphrase": "my name is layan", "confidence": 0.7}
}

# User credentials and roles
USERS = {
    "maen": {"password": "maen", "role": "admin", "voice_auth": True},
    "aya": {"password": "aya", "role": "user", "voice_auth": True},
    "tima": {"password": "tima", "role": "user", "voice_auth": True},
    "layan": {"password": "layan", "role": "user", "voice_auth": True},
    "user1": {"password": "password1", "role": "user", "voice_auth": False},
    "robotics": {"password": "securepass", "role": "user", "voice_auth": False}
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
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3,
            response_format={"type": "json_object"}
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
                "description": "Error in command processing"
            }

    except Exception as e:
        logger.error(f"API error: {str(e)}")
        return {
            "error": str(e),
            "commands": [{
                "mode": "stop",
                "description": "API error - robot stopped"
            }],
            "description": "Error in API communication"
        }

# HTML templates directory
@app.route('/static/<path:path>')
def send_static(path):
    return send_from_directory('static', path)

@app.route('/')
def login():
    if 'user' in session:
        return redirect(url_for('home'))
    return render_template('login.html')

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    # Check if user exists and credentials are correct
    if username in USERS and USERS[username]["password"] == password:
        # If user has voice auth enabled, redirect to voice verification
        if USERS[username].get("voice_auth", False):
            # Store username temporarily (pending voice verification)
            session['pending_user'] = username
            return redirect(url_for('voice_auth'))
        else:
            # Complete authentication immediately for users without voice auth
            session['user'] = username
            return redirect(url_for('home'))
    else:
        return render_template('login.html', error="Invalid credentials")

@app.route('/voice_auth')
def voice_auth():
    if 'pending_user' not in session:
        return redirect(url_for('login'))
    
    username = session['pending_user']
    return render_template('voice_auth.html', username=username)

@app.route('/verify_voice', methods=['POST'])
def verify_voice():
    if 'pending_user' not in session:
        return jsonify({"success": False, "error": "No pending authentication"}), 401
    
    username = session['pending_user']
    data = request.get_json()
    
    transcript = data.get('transcript', '').lower()
    confidence = data.get('confidence', 0.0)

    # Get user's voice profile
    if username not in VOICE_PROFILES:
        return jsonify({"success": False, "error": "No voice profile found"}), 400
        
    user_profile = VOICE_PROFILES[username]
    
    # Simple verification logic (in a real app, use more sophisticated voice biometrics)
    expected_phrase = user_profile.get('passphrase')
    confidence_threshold = user_profile.get('confidence', 0.7)
    
    phrase_match = expected_phrase in transcript
    
    if phrase_match and confidence >= confidence_threshold:
        # Voice verification successful, complete login
        session['user'] = username
        session.pop('pending_user', None)
        return jsonify({"success": True})
    elif not phrase_match:
        return jsonify({"success": False, "error": "Voice passphrase doesn't match"})
    else:
        return jsonify({"success": False, "error": "Voice confidence too low"})

@app.route('/logout')
def logout():
    session.pop('user', None)
    session.pop('pending_user', None)
    return redirect(url_for('login'))

@app.route('/home')
@login_required
def home():
    username = session['user']
    return render_template('robot_control.html', username=username)

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

# API endpoint for ESP32 communication
@app.route('/api/robot_command', methods=['GET', 'POST'])
def robot_command():
    # Simple authentication using API key instead of session-based auth
    api_key = request.headers.get('X-API-Key')
    if not api_key or api_key != '1234':
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
    # Make sure the templates directory exists
    if not os.path.exists('templates'):
        os.makedirs('templates')
    
    # For development, otherwise use production WSGI server
    app.run(debug=True, host='0.0.0.0', port=5000)
