from flask import Flask, render_template, request, jsonify, redirect, url_for, session, flash
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
app.secret_key = os.getenv("SECRET_KEY", "your_secret_key")  # Better to use env variable on Render

# Database Configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv("DATABASE_URL", "sqlite:///robot_control.db")
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

# User Model
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), default='user')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
        
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

# Command History Model
class CommandHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    command = db.Column(db.Text, nullable=False)
    processed_command = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    user = db.relationship('User', backref=db.backref('commands', lazy=True))

# Rate Limit Model
class RateLimit(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    request_time = db.Column(db.Float, nullable=False)
    
    user = db.relationship('User', backref=db.backref('rate_limits', lazy=True))

# Rate limiting configuration
rate_limits = {
    "admin": {"requests": 50, "period": 3600},  # 50 requests per hour
    "user": {"requests": 20, "period": 3600}    # 20 requests per hour
}

# Configure OpenAI
openai.api_key = os.getenv("OPENAI_API_KEY")

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
            return jsonify({"error": "User not found"}), 404
        
        # Get user's role and corresponding rate limit
        role = user.role
        limit = rate_limits.get(role, rate_limits['user'])
        
        # Clean up old requests
        current_time = time.time()
        cutoff_time = current_time - limit['period']
        
        # Delete old rate limit records
        db.session.query(RateLimit).filter(
            RateLimit.user_id == user_id,
            RateLimit.request_time < cutoff_time
        ).delete()
        
        # Count recent requests
        recent_request_count = RateLimit.query.filter(
            RateLimit.user_id == user_id
        ).count()
        
        # Check if limit exceeded
        if recent_request_count >= limit['requests']:
            oldest_request = RateLimit.query.filter(
                RateLimit.user_id == user_id
            ).order_by(RateLimit.request_time.asc()).first()
            
            retry_after = limit['period'] - (current_time - oldest_request.request_time)
            
            return jsonify({
                "error": f"Rate limit exceeded. Maximum {limit['requests']} requests per {limit['period']//3600} hour(s).",
                "retry_after": retry_after
            }), 429
        
        # Add current request timestamp
        new_rate_limit = RateLimit(user_id=user_id, request_time=current_time)
        db.session.add(new_rate_limit)
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

# Initialize database with admin user
def initialize_database():
    with app.app_context():
        db.create_all()
        
        # Check if admin user exists
        admin = User.query.filter_by(username='admin').first()
        if not admin:
            admin = User(username='admin', role='admin')
            admin.set_password('admin')  # Should be changed in production!
            db.session.add(admin)
            
            # Add the existing users
            for username, data in {
                "maen": {"password": "maen", "role": "admin"},
                "user1": {"password": "password1", "role": "user"},
                "robotics": {"password": "securepass", "role": "user"}
            }.items():
                if not User.query.filter_by(username=username).first():
                    user = User(username=username, role=data["role"])
                    user.set_password(data["password"])
                    db.session.add(user)
            
            db.session.commit()
            logger.info("Database initialized with default users")

# HTML Templates 
# (Updating the login form to include registration)
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
        .success { color: #10b981; margin-top: 10px; }
        .tab-container { display: flex; margin-bottom: 20px; }
        .tab { flex: 1; padding: 10px; cursor: pointer; border-bottom: 2px solid transparent; }
        .tab.active { border-bottom-color: #0ea5e9; font-weight: bold; }
        .form-container { display: none; }
        .form-container.active { display: block; }
        .footer { margin-top: 20px; font-size: 12px; color: #94a3b8; }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Robot Control</h1>
        
        <div class="tab-container">
            <div class="tab active" onclick="showTab('login')">Login</div>
            <div class="tab" onclick="showTab('register')">Register</div>
        </div>
        
        <div id="login-form" class="form-container active">
            <h2>Login</h2>
            <form action="/auth" method="post">
                <input type="text" name="username" placeholder="Username" required>
                <input type="password" name="password" placeholder="Password" required>
                <button type="submit">Login</button>
            </form>
            <p class="error" id="loginError" style="display: none;">{{ login_error }}</p>
        </div>
        
        <div id="register-form" class="form-container">
            <h2>Create Account</h2>
            <form action="/register" method="post">
                <input type="text" name="username" placeholder="Username (3-20 characters)" required minlength="3" maxlength="20">
                <input type="password" name="password" placeholder="Password (min 6 characters)" required minlength="6">
                <input type="password" name="confirm_password" placeholder="Confirm Password" required>
                <button type="submit">Register</button>
            </form>
            <p class="error" id="registerError" style="display: none;">{{ register_error }}</p>
            <p class="success" id="registerSuccess" style="display: none;">{{ register_success }}</p>
        </div>
        
        <div class="footer">
            Robot Control System v2.0
        </div>
    </div>
    
    <script>
        function showTab(tabName) {
            // Hide all tabs
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            document.querySelectorAll('.form-container').forEach(form => form.classList.remove('active'));
            
            // Show selected tab
            document.querySelector(`.tab[onclick="showTab('${tabName}')"]`).classList.add('active');
            document.getElementById(`${tabName}-form`).classList.add('active');
        }
        
        // Show errors if present
        document.addEventListener('DOMContentLoaded', function() {
            if ('{{ login_error }}') {
                document.getElementById('loginError').style.display = 'block';
            }
            if ('{{ register_error }}') {
                document.getElementById('registerError').style.display = 'block';
                showTab('register');
            }
            if ('{{ register_success }}') {
                document.getElementById('registerSuccess').style.display = 'block';
                showTab('login');
            }
        });
    </script>
</body>
</html>
"""

# Keep the robot interface template as is, but update the logout link
# (ROBOT_INTERFACE_HTML is unchanged since it's extensive - just update the session handling)

@app.route('/')
def login():
    if 'user_id' in session:
        return redirect(url_for('home'))
    return LOGIN_HTML.replace('{{ login_error }}', '').replace('{{ register_error }}', '').replace('{{ register_success }}', '')

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form.get('username', '')
    password = request.form.get('password', '')

    user = User.query.filter_by(username=username).first()
    
    if user and user.check_password(password):
        session['user_id'] = user.id
        session['username'] = user.username
        
        # Update last login time
        user.last_login = datetime.utcnow()
        db.session.commit()
        
        return redirect(url_for('home'))
    else:
        error_html = LOGIN_HTML.replace('{{ login_error }}', 'Invalid username or password')
        error_html = error_html.replace('{{ register_error }}', '').replace('{{ register_success }}', '')
        return error_html

@app.route('/register', methods=['POST'])
def register():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    
    # Validate input
    if not username or len(username) < 3 or len(username) > 20:
        error_html = LOGIN_HTML.replace('{{ register_error }}', 'Username must be 3-20 characters')
        error_html = error_html.replace('{{ login_error }}', '').replace('{{ register_success }}', '')
        return error_html
    
    if not password or len(password) < 6:
        error_html = LOGIN_HTML.replace('{{ register_error }}', 'Password must be at least 6 characters')
        error_html = error_html.replace('{{ login_error }}', '').replace('{{ register_success }}', '')
        return error_html
        
    if password != confirm_password:
        error_html = LOGIN_HTML.replace('{{ register_error }}', 'Passwords do not match')
        error_html = error_html.replace('{{ login_error }}', '').replace('{{ register_success }}', '')
        return error_html
    
    # Check if username exists
    if User.query.filter_by(username=username).first():
        error_html = LOGIN_HTML.replace('{{ register_error }}', 'Username already exists')
        error_html = error_html.replace('{{ login_error }}', '').replace('{{ register_success }}', '')
        return error_html
    
    # Create new user
    new_user = User(username=username, role='user')
    new_user.set_password(password)
    
    db.session.add(new_user)
    db.session.commit()
    
    success_html = LOGIN_HTML.replace('{{ register_success }}', 'Account created successfully! Please login.')
    success_html = success_html.replace('{{ login_error }}', '').replace('{{ register_error }}', '')
    return success_html

@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('username', None)
    return redirect(url_for('login'))

@app.route('/home')
@login_required
def home():
    # Get the user's username
    username = session.get('username', '')
    user = User.query.filter_by(username=username).first()
    
    if not user:
        session.clear()
        return redirect(url_for('login'))
    
    # Replace the username placeholder with the actual username
    return ROBOT_INTERFACE_HTML.replace("{{ username }}", username)

@app.route('/send_command', methods=['POST'])
@login_required
@rate_limit
def send_command():
    try:
        command = request.form.get('command', '').strip()
        user_id = session.get('user_id')
        
        if not command:
            return jsonify({"error": "No command provided"})
        
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404
        
        # Get user command history for context
        user_commands = []
        previous_commands = CommandHistory.query.filter_by(user_id=user_id).order_by(
            CommandHistory.timestamp.desc()
        ).limit(10).all()
        
        for cmd in previous_commands:
            if cmd.command:
                user_commands.append(cmd.command)
        
        # Interpret the command
        interpreted_command = interpret_command(command, user_commands)
        
        # Store command in history
        new_command = CommandHistory(
            user_id=user_id,
            command=command,
            processed_command=json.dumps(interpreted_command)
        )
        db.session.add(new_command)
        db.session.commit()
        
        # Return the interpreted command
        return jsonify(interpreted_command)
    
    except Exception as e:
        logger.error(f"Error processing command: {str(e)}")
        return jsonify({"error": str(e)}), 500

# Admin dashboard to manage users
@app.route('/admin')
@login_required
def admin_dashboard():
    # Check if user is admin
    user_id = session.get('user_id')
    user = User.query.get(user_id)
    
    if not user or user.role != 'admin':
        return jsonify({"error": "Unauthorized access"}), 403
    
    # Get all users (except current admin)
    users = User.query.filter(User.id != user_id).all()
    
    # HTML for admin dashboard
    admin_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Admin Dashboard</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background-color: #1e293b; color: white; padding: 20px; }}
            .container {{ max-width: 1000px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }}
            h1, h2 {{ margin-top: 0; }}
            table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
            th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #475569; }}
            th {{ background-color: #0f172a; }}
            tr:hover {{ background-color: #475569; }}
            .action-btn {{ padding: 8px 12px; margin: 0 5px; border-radius: 8px; border: none; cursor: pointer; }}
            .edit-btn {{ background-color: #0ea5e9; color: white; }}
            .delete-btn {{ background-color: #ef4444; color: white; }}
            .back-btn {{ display: inline-block; margin-top: 20px; padding: 10px 15px; background-color: #475569; color: white; text-decoration: none; border-radius: 8px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>Admin Dashboard</h1>
            <h2>User Management</h2>
            
            <table>
                <tr>
                    <th>Username</th>
                    <th>Role</th>
                    <th>Created</th>
                    <th>Last Login</th>
                    <th>Actions</th>
                </tr>
    """
    
    for u in users:
        created_date = u.created_at.strftime('%Y-%m-%d') if u.created_at else 'N/A'
        last_login = u.last_login.strftime('%Y-%m-%d %H:%M') if u.last_login else 'Never'
        
        admin_html += f"""
                <tr>
                    <td>{u.username}</td>
                    <td>{u.role}</td>
                    <td>{created_date}</td>
                    <td>{last_login}</td>
                    <td>
                        <button class="action-btn edit-btn" onclick="location.href='/admin/edit_user/{u.id}'">Edit</button>
                        <button class="action-btn delete-btn" onclick="confirmDelete({u.id}, '{u.username}')">Delete</button>
                    </td>
                </tr>
        """
    
    admin_html += f"""
            </table>
            
            <button class="action-btn edit-btn" onclick="location.href='/admin/add_user'">Add New User</button>
            <a href="/home" class="back-btn">Back to Robot Control</a>
        </div>
        
        <script>
            function confirmDelete(userId, username) {{
                if (confirm(`Are you sure you want to delete user: ${{username}}?`)) {{
                    location.href = `/admin/delete_user/${{userId}}`;
                }}
            }}
        </script>
    </body>
    </html>
    """
    
    return admin_html

# New endpoint for ESP32 communication
@app.route('/api/robot_command', methods=['GET', 'POST'])
def robot_command():
    # Simple authentication using API key instead of session-based auth
    api_key = request.headers.get('X-API-Key')
    if not api_key or api_key != '1234' :
        return jsonify({"error": "Invalid API key"}), 401
    
    # For GET requests, return the latest command for the robot
    if request.method == 'GET':
        # Find the robotics user
        robotics_user = User.query.filter_by(username='robotics').first()
        
        if not robotics_user:
            return jsonify({"error": "Robotics user not found"}), 404
        
        # Get the most recent command
        latest_command = CommandHistory.query.filter_by(user_id=robotics_user.id).order_by(
            CommandHistory.timestamp.desc()
        ).first()
        
        if latest_command and latest_command.processed_command:
            try:
                return jsonify(json.loads(latest_command.processed_command))
            except:
                pass
            
        return jsonify({"error": "No commands available"}), 404
    
    # For POST requests, allow the ESP32 to send status updates
    elif request.method == 'POST':
        try:
            data = request.get_json()
            # Process status update from ESP32
            logger.info(f"Received status update from ESP32: {data}")
            
            # Here you could store status updates in a database table
            # For now, just log it
            
            return jsonify({"status": "received"}), 200
        except Exception as e:
            logger.error(f"Error processing ESP32 status update: {str(e)}")
            return jsonify({"error": str(e)}), 400

# Initialize the database if this file is run directly
if __name__ == '__main__':
    initialize_database()
    # For development, otherwise use production WSGI server
    app.run(debug=True, host='0.0.0.0', port=5000)

# Additional admin routes for user management
@app.route('/admin/add_user', methods=['GET', 'POST'])
@login_required
def add_user():
    # Check if user is admin
    user_id = session.get('user_id')
    user = User.query.get(user_id)
    
    if not user or user.role != 'admin':
        return jsonify({"error": "Unauthorized access"}), 403
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        
        # Validate input
        if not username or len(username) < 3:
            return render_template_string(ADD_USER_HTML, error="Username must be at least 3 characters")
        
        if not password or len(password) < 6:
            return render_template_string(ADD_USER_HTML, error="Password must be at least 6 characters")
        
        # Check if username exists
        if User.query.filter_by(username=username).first():
            return render_template_string(ADD_USER_HTML, error="Username already exists")
        
        # Create new user
        new_user = User(username=username, role=role)
        new_user.set_password(password)
        
        db.session.add(new_user)
        db.session.commit()
        
        return redirect(url_for('admin_dashboard'))
    
    # GET request - show the form
    return render_template_string(ADD_USER_HTML, error=None)

@app.route('/admin/edit_user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def edit_user(user_id):
    # Check if current user is admin
    admin_id = session.get('user_id')
    admin = User.query.get(admin_id)
    
    if not admin or admin.role != 'admin':
        return jsonify({"error": "Unauthorized access"}), 403
    
    # Get the user to edit
    user_to_edit = User.query.get(user_id)
    if not user_to_edit:
        return redirect(url_for('admin_dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        new_password = request.form.get('password', '')
        role = request.form.get('role', 'user')
        
        # Validate username
        if not username or len(username) < 3:
            return render_template_string(
                EDIT_USER_HTML, 
                user=user_to_edit,
                error="Username must be at least 3 characters"
            )
        
        # Check if new username already exists (if changed)
        if username != user_to_edit.username and User.query.filter_by(username=username).first():
            return render_template_string(
                EDIT_USER_HTML, 
                user=user_to_edit,
                error="Username already exists"
            )
        
        # Update user
        user_to_edit.username = username
        user_to_edit.role = role
        
        # Update password if provided
        if new_password:
            if len(new_password) < 6:
                return render_template_string(
                    EDIT_USER_HTML, 
                    user=user_to_edit,
                    error="Password must be at least 6 characters"
                )
            user_to_edit.set_password(new_password)
        
        db.session.commit()
        return redirect(url_for('admin_dashboard'))
    
    # GET request - show the form
    return render_template_string(EDIT_USER_HTML, user=user_to_edit, error=None)

@app.route('/admin/delete_user/<int:user_id>')
@login_required
def delete_user(user_id):
    # Check if current user is admin
    admin_id = session.get('user_id')
    admin = User.query.get(admin_id)
    
    if not admin or admin.role != 'admin':
        return jsonify({"error": "Unauthorized access"}), 403
    
    # Prevent self-deletion
    if admin_id == user_id:
        return redirect(url_for('admin_dashboard'))
    
    # Get the user to delete
    user_to_delete = User.query.get(user_id)
    if user_to_delete:
        # Delete associated data
        CommandHistory.query.filter_by(user_id=user_id).delete()
        RateLimit.query.filter_by(user_id=user_id).delete()
        
        # Delete the user
        db.session.delete(user_to_delete)
        db.session.commit()
    
    return redirect(url_for('admin_dashboard'))

# HTML Templates for user management
ADD_USER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Add New User</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; background-color: #1e293b; color: white; padding: 20px; }
        .container { max-width: 500px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        h1 { margin-top: 0; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; }
        input, select { width: 100%; padding: 10px; font-size: 16px; border-radius: 8px; border: none; background-color: #475569; color: white; }
        button { padding: 12px 20px; margin-top: 10px; background-color: #0ea5e9; color: white; border: none; border-radius: 8px; cursor: pointer; }
        .error { color: #f87171; margin-top: 10px; }
        .back-btn { display: inline-block; margin-top: 20px; padding: 10px 15px; background-color: #475569; color: white; text-decoration: none; border-radius: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Add New User</h1>
        
        {% if error %}
        <p class="error">{{ error }}</p>
        {% endif %}
        
        <form method="post">
            <div class="form-group">
                <label for="username">Username:</label>
                <input type="text" id="username" name="username" required minlength="3">
            </div>
            
            <div class="form-group">
                <label for="password">Password:</label>
                <input type="password" id="password" name="password" required minlength="6">
            </div>
            
            <div class="form-group">
                <label for="role">Role:</label>
                <select id="role" name="role">
                    <option value="user">User</option>
                    <option value="admin">Admin</option>
                </select>
            </div>
            
            <button type="submit">Add User</button>
        </form>
        
        <a href="/admin" class="back-btn">Back to Admin Dashboard</a>
    </div>
</body>
</html>
"""

EDIT_USER_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Edit User</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { font-family: 'Segoe UI', sans-serif; background-color: #1e293b; color: white; padding: 20px; }
        .container { max-width: 500px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
        h1 { margin-top: 0; }
        .form-group { margin-bottom: 15px; }
        label { display: block; margin-bottom: 5px; }
        input, select { width: 100%; padding: 10px; font-size: 16px; border-radius: 8px; border: none; background-color: #475569; color: white; }
        button { padding: 12px 20px; margin-top: 10px; background-color: #0ea5e9; color: white; border: none; border-radius: 8px; cursor: pointer; }
        .error { color: #f87171; margin-top: 10px; }
        .note { color: #94a3b8; margin-top: 5px; font-size: 14px; }
        .back-btn { display: inline-block; margin-top: 20px; padding: 10px 15px; background-color: #475569; color: white; text-decoration: none; border-radius: 8px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Edit User</h1>
        
        {% if error %}
        <p class="error">{{ error }}</p>
        {% endif %}
        
        <form method="post">
            <div class="form-group">
                <label for="username">Username:</label>
                <input type="text" id="username" name="username" value="{{ user.username }}" required minlength="3">
            </div>
            
            <div class="form-group">
                <label for="password">New Password:</label>
                <input type="password" id="password" name="password">
                <p class="note">Leave blank to keep current password</p>
            </div>
            
            <div class="form-group">
                <label for="role">Role:</label>
                <select id="role" name="role">
                    <option value="user" {% if user.role == 'user' %}selected{% endif %}>User</option>
                    <option value="admin" {% if user.role == 'admin' %}selected{% endif %}>Admin</option>
                </select>
            </div>
            
            <button type="submit">Update User</button>
        </form>
        
        <a href="/admin" class="back-btn">Back to Admin Dashboard</a>
    </div>
</body>
</html>
"""
