from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import openai
import json
from dotenv import load_dotenv
import os
import time
import logging
import re
from functools import wraps
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "your_secret_key")

# Configure PostgreSQL
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

# Define User model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime, nullable=True)
    failed_login_attempts = db.Column(db.Integer, default=0)
    account_locked = db.Column(db.Boolean, default=False)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
    def __repr__(self):
        return f'<User {self.username}>'

# Define CommandHistory model
class CommandHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    command = db.Column(db.Text, nullable=False)
    timestamp = db.Column(db.Float, nullable=False)
    is_time_record = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f'<CommandHistory {self.id}>'

# Password validation function
def validate_password(password):
    """
    Validates password strength based on multiple criteria.
    Returns (is_valid, message) tuple.
    """
    if len(password) < 8:
        return False, "Password must be at least 8 characters long"
    
    # Check for at least one uppercase letter
    if not re.search(r'[A-Z]', password):
        return False, "Password must contain at least one uppercase letter"
    
    # Check for at least one lowercase letter
    if not re.search(r'[a-z]', password):
        return False, "Password must contain at least one lowercase letter"
    
    # Check for at least one digit
    if not re.search(r'\d', password):
        return False, "Password must contain at least one number"
    
    # Check for at least one special character
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        return False, "Password must contain at least one special character"
    
    return True, "Password is strong"

# Rate limiting configuration
rate_limits = {
    "admin": {"requests": 50, "period": 3600},  # 50 requests per hour
    "user": {"requests": 20, "period": 3600}    # 20 requests per hour
}

# Decorator for authentication
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return decorated_function

# Decorator for rate limiting
def rate_limit(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({"error": "Authentication required"}), 401
        
        user_id = session['user_id']
        user = User.query.get(user_id)
        
        if not user:
            return jsonify({"error": "User not found"}), 401
        
        # Get user's role and corresponding rate limit
        role = user.role
        limit = rate_limits.get(role, rate_limits['user'])
        
        # Get recent command time records
        current_time = time.time()
        cutoff_time = current_time - limit['period']
        
        # Count recent requests within the time period
        recent_requests = CommandHistory.query.filter(
            CommandHistory.user_id == user_id,
            CommandHistory.is_time_record == True,
            CommandHistory.timestamp > cutoff_time
        ).count()
        
        # Check if limit exceeded
        if recent_requests >= limit['requests']:
            oldest_request = CommandHistory.query.filter(
                CommandHistory.user_id == user_id,
                CommandHistory.is_time_record == True,
                CommandHistory.timestamp > cutoff_time
            ).order_by(CommandHistory.timestamp.asc()).first()
            
            retry_after = limit['period'] - (current_time - oldest_request.timestamp)
            
            return jsonify({
                "error": f"Rate limit exceeded. Maximum {limit['requests']} requests per {limit['period']//3600} hour(s).",
                "retry_after": retry_after
            }), 429
        
        # Add current request timestamp
        new_time_record = CommandHistory(
            user_id=user_id,
            command="TIME_RECORD",
            timestamp=current_time,
            is_time_record=True
        )
        db.session.add(new_time_record)
        db.session.commit()
        
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
       - Always specify a rotation value in degrees 
       - Always specify a reasonable speed (0.5-1.0 m/s is typical for rotation)
       - Use "stop_condition": "time" if time is specified, otherwise "rotation"
       1.2) for arc mode specify: turn radius and distance(distance of the arc)

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
    - Circle: A arc that forms a complete 360° path
    - Triangle: 3 forward movements with 120° turns
    - Rectangle: 2 pairs of different-length forward movements with 90° turns
    - Figure-eight: Two connected circles in opposite directions
    -question mark figure(complrx shape example) : half circle then downwards line movement
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

# Database initialization
with app.app_context():
    # Create all tables
    db.create_all()
    
    # Check if default users exist
    admin = User.query.filter_by(username='maen').first()
    
    if admin is None:
        try:
            # Create default admin user
            admin_user = User(
                username='maen', 
                email='admin@example.com',  # Add a default email
                role='admin', 
                created_at=datetime.utcnow()
            )
            admin_user.set_password('maen')
            
            # Create default user1
            user1 = User(
                username='user1', 
                email='user1@example.com',
                role='user',
                created_at=datetime.utcnow()
            )
            user1.set_password('password1')
            
            # Create default robotics user
            robotics = User(
                username='robotics', 
                email='robotics@example.com',
                role='user',
                created_at=datetime.utcnow()
            )
            robotics.set_password('securepass')
            
            # Add all users to database
            db.session.add_all([admin_user, user1, robotics])
            db.session.commit()
            
            print("Default users created successfully")
        except Exception as e:
            db.session.rollback()
            print(f"Error creating default users: {str(e)}")
    else:
        print("Default users already exist")

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
        a { color: #38bdf8; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1> Robot Control</h1>
        <h2>Login</h2>
        <form action="/auth" method="post">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
        </form>
        <p>Don't have an account? <a href="/register">Register here</a></p>
        <p class="error" id="errorMsg" style="display: none;"></p>
    </div>
</body>
</html>
"""

# Registration form HTML
REGISTER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Register - Robot Control</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; text-align: center; background-color: #1e293b; color: white; padding: 50px; }
        .register-container { max-width: 500px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        input, button { padding: 15px; font-size: 16px; margin: 10px 0; border-radius: 10px; border: none; width: 100%; box-sizing: border-box; }
        input { background-color: #475569; color: white; }
        button { background-color: #0284c7; color: white; cursor: pointer; transition: all 0.2s ease; }
        button:hover { background-color: #0ea5e9; transform: translateY(-2px); }
        .error { color: #f87171; margin-top: 10px; }
        .success { color: #4ade80; margin-top: 10px; }
        .password-strength { text-align: left; margin: 5px 0 15px 0; font-size: 14px; }
        .password-criteria { text-align: left; margin: 0; padding: 0 0 0 20px; font-size: 14px; color: #94a3b8; }
        .password-criteria li { margin: 5px 0; }
        .password-criteria li.met { color: #4ade80; }
        a { color: #38bdf8; text-decoration: none; }
        a:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="register-container">
        <h1>Robot Control</h1>
        <h2>Create an Account</h2>
        <form id="registerForm" action="/register" method="post">
            <input type="text" name="username" id="username" placeholder="Username (3-20 characters)" required>
            <input type="email" name="email" id="email" placeholder="Email Address" required>
            <input type="password" name="password" id="password" placeholder="Password" required>
            <div class="password-strength">
                <div id="passwordStrengthBar" style="height: 5px; width: 0%; background-color: #ef4444; border-radius: 5px; transition: all 0.3s;"></div>
                <ul class="password-criteria">
                    <li id="length">At least 8 characters</li>
                    <li id="uppercase">At least one uppercase letter</li>
                    <li id="lowercase">At least one lowercase letter</li>
                    <li id="number">At least one number</li>
                    <li id="special">At least one special character (!@#$%^&*)</li>
                </ul>
            </div>
            <input type="password" name="confirm_password" id="confirmPassword" placeholder="Confirm Password" required>
            <button type="submit">Register</button>
        </form>
        <p class="error" id="errorMsg" style="display: none;"></p>
        <p class="success" id="successMsg" style="display: none;"></p>
        <p>Already have an account? <a href="/">Login here</a></p>
    </div>

    <script>
        document.addEventListener('DOMContentLoaded', function() {
            const passwordInput = document.getElementById('password');
            const confirmPasswordInput = document.getElementById('confirmPassword');
            const passwordStrengthBar = document.getElementById('passwordStrengthBar');
            const form = document.getElementById('registerForm');
            
            // Password criteria elements
            const lengthCriteria = document.getElementById('length');
            const uppercaseCriteria = document.getElementById('uppercase');
            const lowercaseCriteria = document.getElementById('lowercase');
            const numberCriteria = document.getElementById('number');
            const specialCriteria = document.getElementById('special');
            
            passwordInput.addEventListener('input', function() {
                const password = passwordInput.value;
                let strength = 0;
                
                // Reset all criteria
                lengthCriteria.classList.remove('met');
                uppercaseCriteria.classList.remove('met');
                lowercaseCriteria.classList.remove('met');
                numberCriteria.classList.remove('met');
                specialCriteria.classList.remove('met');
                
                // Check each criteria
                if (password.length >= 8) {
                    strength += 20;
                    lengthCriteria.classList.add('met');
                }
                
                if (/[A-Z]/.test(password)) {
                    strength += 20;
                    uppercaseCriteria.classList.add('met');
                }
                
                if (/[a-z]/.test(password)) {
                    strength += 20;
                    lowercaseCriteria.classList.add('met');
                }
                
                if (/\d/.test(password)) {
                    strength += 20;
                    numberCriteria.classList.add('met');
                }
                
                if (/[!@#$%^&*(),.?":{}|<>]/.test(password)) {
                    strength += 20;
                    specialCriteria.classList.add('met');
                }
                
                // Update strength bar
                passwordStrengthBar.style.width = strength + '%';
                
                // Change color based on strength
                if (strength < 40) {
                    passwordStrengthBar.style.backgroundColor = '#ef4444'; // red
                } else if (strength < 80) {
                    passwordStrengthBar.style.backgroundColor = '#eab308'; // yellow
                } else {
                    passwordStrengthBar.style.backgroundColor = '#4ade80'; // green
                }
            });
            
            form.addEventListener('submit', function(e) {
                // Check if passwords match
                if (passwordInput.value !== confirmPasswordInput.value) {
                    e.preventDefault();
                    document.getElementById('errorMsg').textContent = 'Passwords do not match';
                    document.getElementById('errorMsg').style.display = 'block';
                    document.getElementById('successMsg').style.display = 'none';
                }
            });
        });
    </script>
</body>
</html>
"""

# Registration success page
REGISTRATION_SUCCESS_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Registration Successful - Robot Control</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; text-align: center; background-color: #1e293b; color: white; padding: 50px; }
        .success-container { max-width: 500px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        .success-icon { font-size: 64px; color: #4ade80; margin: 20px 0; }
        h1 { color: #4ade80; }
        p { margin: 20px 0; line-height: 1.6; }
        a { display: inline-block; margin-top: 20px; padding: 15px 30px; background-color: #0284c7; color: white; text-decoration: none; border-radius: 10px; transition: all 0.2s; }
        a:hover { background-color: #0ea5e9; transform: translateY(-2px); }
    </style>
</head>
<body>
    <div class="success-container">
        <div class="success-icon">✓</div>
        <h1>Registration Successful!</h1>
        <p>Your account has been created successfully. You can now log in to access the Robot Control interface.</p>
        <a href="/">Login Now</a>
    </div>
</body>
</html>
"""

@app.route('/')
def login():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return LOGIN_HTML

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    user = User.query.filter_by(username=username).first()
    
    # Check if account is locked
    if user and user.account_locked:
        error_html = LOGIN_HTML.replace(
            '<p class="error" id="errorMsg" style="display: none;"></p>', 
            '<p class="error" id="errorMsg">Account locked due to multiple failed login attempts. Please contact an administrator.</p>'
        )
        return error_html
    
    # Validate password
    if user and user.check_password(password):
        # Reset failed login attempts on successful login
        user.failed_login_attempts = 0
        user.last_login = datetime.utcnow()
        db.session.commit()
        
        session['user_id'] = user.id
        session['username'] = user.username
        return redirect(url_for('home'))
    else:
        # Increment failed login attempts
        if user:
            user.failed_login_attempts += 1
            
            # Lock account after 5 failed attempts
            if user.failed_login_attempts >= 5:
                user.account_locked = True
                db.session.commit()
                error_html = LOGIN_HTML.replace(
                    '<p class="error" id="errorMsg" style="display: none;"></p>', 
                    '<p class="error" id="errorMsg">Account locked due to multiple failed login attempts. Please contact an administrator.</p>'
                )
                return error_html
            
            db.session.commit()
        
        error_html = LOGIN_HTML.replace(
            '<p class="error" id="errorMsg" style="display: none;"></p>', 
            '<p class="error" id="errorMsg">Invalid credentials</p>'
        )
        return error_html

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return REGISTER_HTML
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Verify all fields are provided
        if not (username and email and password and confirm_password):
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                '<p class="error" id="errorMsg">All fields are required</p>'
            )
            return error_html
        
        # Check username length
        if len(username) < 3 or len(username) > 20:
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                '<p class="error" id="errorMsg">Username must be between 3 and 20 characters</p>'
            )
            return error_html
        
        # Check if username already exists
        if User.query.filter_by(username=username).first():
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                '<p class="error" id="errorMsg">Username already taken</p>'
            )
            return error_html
        
        # Check if email already exists
        if User.query.filter_by(email=email).first():
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                '<p class="error" id="errorMsg">Email already registered</p>'
            )
            return error_html
        
        # Validate password strength
        is_valid, message = validate_password(password)
        if not is_valid:
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                f'<p class="error" id="errorMsg">{message}</p>'
            )
            return error_html
        
        # Check if passwords match
        if password != confirm_password:
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                '<p class="error" id="errorMsg">Passwords do not match</p>'
            )
            return error_html
        
        # Create new user
        try:
            new_user = User(username=username, email=email, role='user')
            new_user.set_password(password)
            db.session.add(new_user)
            db.session.commit()
            
            # Log the successful registration
            logger.info(f"New user registered: {username}")
            
            return REGISTRATION_SUCCESS_HTML
        except Exception as e:
            db.session.rollback()
            logger.error(f"Error registering user: {str(e)}")
            error_html = REGISTER_HTML.replace(
                '<p class="error" id="errorMsg" style="display: none;"></p>',
                f'<p class="error" id="errorMsg">Registration failed: {str(e)}</p>'
            )
            return error_html

@app.route('/home')
@login_required
def home():
    # Replace the username placeholder with the actual username
    return ROBOT_INTERFACE_HTML.replace("{{ username }}", session['username'])

@app.route('/send_command', methods=['POST'])
@login_required
@rate_limit
def send_command():
    try:
        command = request.form.get('command', '').strip()
        user_id = session.get('user_id')
        
        if not command:
            return jsonify({"error": "No command provided"})
        
        # Get user command history for context (only command strings)
        user_commands = []
        recent_commands = CommandHistory.query.filter(
            CommandHistory.user_id == user_id,
            CommandHistory.is_time_record == False
        ).order_by(CommandHistory.timestamp.desc()).limit(10).all()
        
        for cmd in recent_commands:
            try:
                cmd_data = json.loads(cmd.command)
                if "original_command" in cmd_data:
                    user_commands.append(cmd_data["original_command"])
            except:
                continue
        
        # Interpret the command
        interpreted_command = interpret_command(command, user_commands)
        
        # Store command in history
        new_command = CommandHistory(
            user_id=user_id,
            command=json.dumps(interpreted_command),
            timestamp=time.time(),
            is_time_record=False
        )
        db.session.add(new_command)
        db.session.commit()
        
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
    if not api_key or api_key != '1234':
        return jsonify({"error": "Invalid API key"}), 401
    
    # For GET requests, return the latest command for the robot
    if request.method == 'GET':
        # Get robotics user ID
        robotics_user = User.query.filter_by(username='robotics').first()
        if not robotics_user:
            return jsonify({"error": "Robotics user not found"}), 404
            
        # Get the most recent command
        latest_command = CommandHistory.query.filter_by(
            user_id=robotics_user.id,
            is_time_record=False
        ).order_by(CommandHistory.timestamp.desc()).first()
        
        if latest_command:
            try:
                return jsonify(json.loads(latest_command.command))
            except:
                return jsonify({"error": "Invalid command format"}), 500
        else:
            return jsonify({"error": "No commands available"}), 404
    
    # For POST requests, allow the ESP32 to send status updates
    elif request.method == 'POST':
        try:
            data = request.get_json()
            # Process status update from ESP32
            logger.info(f"Received status update from ESP32: {data}")
            
            # Store the status update if needed - you could create another table for this
            return jsonify({"status": "received"}), 200
        except Exception as e:
            logger.error(f"Error processing ESP32 status update: {str(e)}")
            return jsonify({"error": str(e)}), 400

if __name__ == '__main__':
    # For development, otherwise use production WSGI server
    app.run(debug=True, host='0.0.0.0', port=5000)
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
