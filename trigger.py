from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import openai
import json
from dotenv import load_dotenv
import os

load_dotenv()
print("API Key Loaded:", os.getenv("OPENAI_API_KEY"))
app = Flask(__name__)
app.secret_key = "your_secret_key"  # Used for session management

# 🔑 Replace with your OpenAI API Key

# Presaved users (Username: Password)
USERS = {
    "maen": "maen",
    "user1": "password1",
    "robotics": "securepass"
}

def interpret_command(command):
    """
    Sends a human language movement command to GPT-3.5-Turbo and returns structured movement parameters.
    """
    prompt = f"""
You are an AI that converts human movement instructions into structured JSON commands for a 4-wheeled robot.

**Example of Supported Shapes:**
- Square (4 straight lines + 4 turns)
- Triangle (3 straight lines + 3 turns)
- Circle (smooth curved movement)
- Pentagon, Hexagon (straight lines + turns)

### JSON Output Format:
- "mode": Type of movement ("linear", "rotate", "arc", "stop")
- "direction": Movement direction ("forward", "backward", "left", "right")
- "speed": Speed in meters per second (m/s)
- "distance": Distance in meters (if applicable)
- "time": Duration in seconds (if applicable)
- "rotation": Rotation angle in degrees (if applicable)
- "turn_radius": Radius for curved movements (if applicable)
- "stop_condition": When to stop ("time", "distance", "obstacle")

---

### Convert the following user command into JSON format:

User: "{command}"
AI Output:
"""
    try:
        response = openai.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "system", "content": "You are an AI that only returns structured JSON output for robot commands. Do not return text or explanations, only valid JSON."},
                      {"role": "user", "content": prompt}],
            temperature=0.4
        )

        raw_output = response.choices[0].message.content
        print("\n🔍 Raw AI Output:\n", raw_output)

        try:
            parsed_data = json.loads(raw_output)
            return parsed_data
        except json.JSONDecodeError:
            print("⚠️ AI returned non-JSON response!")
            return {"error": "AI did not return a valid JSON response.", "raw_output": raw_output}

    except Exception as e:
        print(f"⚠️ API Error: {e}")
        return {"error": str(e)}

@app.route('/')
def login():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Login</title>
        <style>
            body { font-family: Arial, sans-serif; text-align: center; background-color: #1e293b; color: white; padding: 50px; }
            input, button { padding: 12px; font-size: 16px; margin: 8px; border-radius: 10px; border: none; }
            button { background-color: #007bff; color: white; cursor: pointer; }
            button:hover { background-color: #0056b3; }
        </style>
    </head>
    <body>
        <h2>Login</h2>
        <form action="/auth" method="post">
            <input type="text" name="username" placeholder="Username" required><br>
            <input type="password" name="password" placeholder="Password" required><br>
            <button type="submit">Login</button>
        </form>
    </body>
    </html>
    """

@app.route('/auth', methods=['POST'])
def auth():
    username = request.form['username']
    password = request.form['password']

    if username in USERS and USERS[username] == password:
        session['user'] = username
        return redirect(url_for('home'))
    else:
        return "<h3>Invalid Credentials! <a href='/'>Try Again</a></h3>"

@app.route('/logout')
def logout():
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/home')
def home():
    if 'user' not in session:
        return redirect(url_for('login'))
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Chat UI</title>
        <style>
            body { font-family: Arial, sans-serif; background-color: #1e293b; color: white; text-align: center; padding: 20px; }
            .chatbox { max-width: 600px; margin: auto; background: #334155; padding: 20px; border-radius: 15px; box-shadow: 0 4px 8px rgba(0, 0, 0, 0.2); }
            input { width: 70%; padding: 12px; border-radius: 10px; border: none; }
            button { width: 15%; padding: 12px; border-radius: 10px; border: none; background-color: #007bff; color: white; cursor: pointer; }
            pre { text-align: left; background: #0f172a; padding: 15px; border-radius: 10px; color: #f1f5f9; overflow-x: auto; }
        </style>
        <script>
            let listening = false;

            function startTriggerWordListening() {
                const recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
                recognition.continuous = true;
                recognition.lang = 'en-US';
                recognition.interimResults = false;

                recognition.onresult = function(event) {
                    const transcript = event.results[event.results.length - 1][0].transcript.trim().toLowerCase();
                    if (transcript.includes("robot")) {
                        listening = true;
                        console.log("✅ Trigger word 'robot' detected. Now listening for command...");
                        recognition.stop();
                        setTimeout(startListening, 500); // start command input
                    }
                };
                recognition.start();
            }

            function startListening() {
                const recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
                recognition.onresult = function(event) {
                    document.getElementById('command').value = event.results[0][0].transcript;
                };
                recognition.start();
            }

            document.addEventListener('DOMContentLoaded', function () {
                startTriggerWordListening();
                document.getElementById('commandForm').addEventListener('submit', function(event) {
                    event.preventDefault();
                    let formData = new FormData(this);
                    fetch('/send_command', {
                        method: 'POST',
                        body: formData
                    })
                    .then(response => response.json())
                    .then(data => {
                        const output = JSON.stringify(data, null, 4);
                        document.getElementById('response').textContent = output;
                    })
                    .catch(error => {
                        document.getElementById('response').textContent = "⚠️ Error: " + error;
                    });
                });
            });
        </script>
    </head>
    <body>
        <div class="chatbox">
            <h1>Robot Chat</h1>
            <form id="commandForm" method="POST" action="/send_command">
                <input type="text" id="command" name="command" placeholder="Enter movement command..." required>
                <button type="button" onclick="startListening()">🎤</button>
                <button type="submit">Send</button>
            </form>
            <h2>Generated Robot Commands:</h2>
            <pre id="response"></pre>
            <a href="/logout" style="display: block; margin-top: 10px; color: #ff4444; text-decoration: none;">Logout</a>
        </div>
    </body>
    </html>
    """

@app.route('/send_command', methods=['POST'])
def send_command():
    if 'user' not in session:
        return jsonify({"error": "Unauthorized access"}), 403
    command = request.form['command']
    response = interpret_command(command)
    return jsonify(response)

if __name__ == '__main__':
    app.run(host="127.0.0.1", port=5000, debug=True)
