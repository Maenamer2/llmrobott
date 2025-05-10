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
    email = db.Column(db.String(120), unique=True, nullable=True)  # Made nullable to handle existing records
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default='user')
    created_at = db.Column(db.DateTime, nullable=True)  # Made nullable to handle existing records
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

# Database migration function to add new columns safely
def add_column_if_not_exists(engine, table_name, column_name, column_type):
    """Add a column to a table if it doesn't exist already."""
    column_exists = False
    
    # Check if column already exists
    inspector = db.inspect(engine)
    columns = [col['name'] for col in inspector.get_columns(table_name)]
    column_exists = column_name in columns
    
    if not column_exists:
        # Column doesn't exist, add it
        try:
            logger.info(f"Adding column {column_name} to table {table_name}")
            with engine.connect() as conn:
                conn.execute(db.text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
                conn.commit()
            logger.info(f"Column {column_name} added successfully")
            return True
        except Exception as e:
            logger.error(f"Error adding column {column_name}: {e}")
            return False
    else:
        logger.info(f"Column {column_name} already exists in {table_name}")
        return True

# Database initialization and migration
with app.app_context():
    # Create all tables first (for new tables)
    db.create_all()
    
    # Get database engine
    engine = db.engine
    
    # Add new columns to User table if they don't exist
    add_column_if_not_exists(engine, 'user', 'email', 'VARCHAR(120) DEFAULT NULL')
    add_column_if_not_exists(engine, 'user', 'created_at', 'TIMESTAMP DEFAULT NOW()')
    add_column_if_not_exists(engine, 'user', 'last_login', 'TIMESTAMP DEFAULT NULL')
    add_column_if_not_exists(engine, 'user', 'failed_login_attempts', 'INTEGER DEFAULT 0')
    add_column_if_not_exists(engine, 'user', 'account_locked', 'BOOLEAN DEFAULT FALSE')
    
    # Make email column unique if it exists
    try:
        with engine.connect() as conn:
            # This SQL is PostgreSQL specific
            conn.execute(db.text("CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email_unique ON \"user\" (email) WHERE email IS NOT NULL"))
            conn.commit()
    except Exception as e:
        logger.error(f"Error making email column unique: {e}")
    
    # Now check for admin user and update/create if needed
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
        # If admin exists but might be missing email
        if not hasattr(admin, 'email') or admin.email is None:
            try:
                admin.email = 'admin@example.com'
                db.session.commit()
                print("Updated admin email")
            except Exception as e:
                db.session.rollback()
                print(f"Error updating admin email: {str(e)}")
        print("Default users already exist")

# Route handlers
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('robot_interface'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET'])
def login():
    return LOGIN_HTML

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form.get('username')
    password = request.form.get('password')
    
    if not username or not password:
        return jsonify({"error": "Username and password are required"}), 400
    
    user = User.query.filter_by(username=username).first()
    
    # Check if user exists and is not locked
    if user and not user.account_locked:
        if user.check_password(password):
            # Successful login
            session['user_id'] = user.id
            session['username'] = user.username
            session['role'] = user.role
            
            # Reset failed login attempts
            user.failed_login_attempts = 0
            user.last_login = datetime.utcnow()
            db.session.commit()
            
            return redirect(url_for('robot_interface'))
        else:
            # Failed password
            user.failed_login_attempts += 1
            
            # Lock account after 5 failed attempts
            if user.failed_login_attempts >= 5:
                user.account_locked = True
                
            db.session.commit()
            
            return jsonify({"error": "Invalid password"}), 401
    elif user and user.account_locked:
        return jsonify({"error": "Account locked. Please contact an administrator."}), 403
    else:
        return jsonify({"error": "User not found"}), 404

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return REGISTER_HTML
    
    if request.method == 'POST':
        try:
            # For JSON requests from fetch API
            if request.is_json:
                data = request.get_json()
                username = data.get('username')
                email = data.get('email')
                password = data.get('password')
                confirm_password = data.get('confirm_password')
            else:
                # For traditional form submission
                username = request.form.get('username')
                email = request.form.get('email')
                password = request.form.get('password')
                confirm_password = request.form.get('confirm_password')
            
            # Validate input
            if not username or not email or not password or not confirm_password:
                return jsonify({"error": "All fields are required"}), 400
            
            if password != confirm_password:
                return jsonify({"error": "Passwords do not match"}), 400
            
            # Check if username already exists
            existing_user = User.query.filter_by(username=username).first()
            if existing_user:
                return jsonify({"error": "Username already exists"}), 400
            
            # Check if email already exists
            existing_email = User.query.filter_by(email=email).first()
            if existing_email:
                return jsonify({"error": "Email already in use"}), 400
            
            # Validate password strength
            is_valid, message = validate_password(password)
            if not is_valid:
                return jsonify({"error": message}), 400
            
            # Create new user with default 'user' role
            new_user = User(
                username=username,
                email=email,
                role='user',
                created_at=datetime.utcnow()
            )
            new_user.set_password(password)
            
            db.session.add(new_user)
            db.session.commit()
            
            return jsonify({"success": True, "message": "Registration successful"}), 201
            
        except Exception as e:
            logger.error(f"Registration error: {str(e)}")
            return jsonify({"error": "An unexpected error occurred"}), 500

@app.route('/robot', methods=['GET'])
@login_required
def robot_interface():
    username = session.get('username', 'Guest')
    return ROBOT_INTERFACE_HTML.replace('{{ username }}', username)

@app.route('/send_command', methods=['POST'])
@login_required
@rate_limit
def send_command():
    try:
        # For JSON requests from fetch API
        if request.is_json:
            data = request.get_json()
            command = data.get('command', '')
        else:
            # For traditional form submission
            command = request.form.get('command', '')
        
        if not command:
            return jsonify({"error": "No command provided"}), 400
        
        # Get user ID from session
        user_id = session.get('user_id')
        
        # Get previous commands for context
        previous_commands = [
            ch.command for ch in CommandHistory.query.filter_by(
                user_id=user_id, 
                is_time_record=False
            ).order_by(CommandHistory.timestamp.desc()).limit(5).all()
        ]
        
        # Interpret the command
        interpreted_command = interpret_command(command, previous_commands)
        
        # Record the command in history
        new_command = CommandHistory(
            user_id=user_id,
            command=command,
            timestamp=time.time(),
            is_time_record=False
        )
        db.session.add(new_command)
        db.session.commit()
        
        return jsonify(interpreted_command)
        
    except Exception as e:
        logger.error(f"Command error: {str(e)}")
        return jsonify({
            "error": str(e),
            "commands": [{
                "mode": "stop",
                "description": "Server error - robot stopped"
            }],
            "description": "Error in server processing"
        }), 500

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/admin')
@login_required
def admin_panel():
    # Check if user has admin role
    if session.get('role') != 'admin':
        return jsonify({"error": "Unauthorized access"}), 403
    
    # Render admin panel (Not implemented in this code snippet)
    return "Admin Panel - Not implemented in this example"

# HTML Templates - Using raw strings to avoid syntax issues with CSS
LOGIN_HTML = r"""
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

ROBOT_INTERFACE_HTML = r"""
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
    
    // Function for manual activation of speech recognition
    function manualStartListening() {
        if (recognition) {
            // Stop current recognition if running
            try {
                recognition.stop();
            } catch (e) {
                console.log("Error stopping recognition:", e);
            }
            
            // Clear any existing timeouts
            if (commandTimeout) {
                clearTimeout(commandTimeout);
            }
            
            // Set up for command listening
            isListeningForTrigger = false;
            isListeningForCommand = true;
            recognition.continuous = false;
            
            // Start listening
            try {
                recognition.start();
                document.getElementById('voiceStatus').textContent = "Listening for command...";
                document.getElementById('voiceStatus').className = "listening";
                document.getElementById('robotFace').className = "listening";
                
                // Set timeout for listening
                commandTimeout = setTimeout(() => {
                    if (isListeningForCommand) {
                        recognition.stop();
                        resetToTriggerMode();
                        document.getElementById('voiceStatus').textContent = "No command heard. Try again.";
                        document.getElementById('voiceStatus').className = "";
                        document.getElementById('robotFace').className = "";
                    }
                }, 5000);
            } catch (e) {
                console.log("Error starting manual listening:", e);
                document.getElementById('voiceStatus').textContent = "Speech recognition error";
                document.getElementById('voiceStatus').className = "";
                document.getElementById('robotFace').className = "";
            }
        } else {
            alert("Speech recognition not available");
        }
    }

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
        
        // Start listening for trigger words
        try {
            recognition.start();
            isListeningForTrigger = true;
            updateStatus("Listening for trigger word...");
        } catch (e) {
            console.log("Error starting speech recognition:", e);
            updateStatus("Speech recognition unavailable");
        }
        
        // Handle errors
        recognition.onerror = function(event) {
            console.log("⚠️ Speech recognition error:", event.error);
            if (event.error === 'no-speech' || event.error === 'network') {
                recognition.stop();
                setTimeout(() => {
                    resetToTriggerMode();
                }, 1000);
            } else if (event.error === 'aborted' || event.error === 'audio-capture' || event.error === 'not-allowed') {
                updateStatus("Speech recognition unavailable");
                isListeningForTrigger = false;
                isListeningForCommand = false;
            }
        };
        """
