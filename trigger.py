from flask import Flask, render_template, request, jsonify, redirect, url_for, session
import openai
import json
from dotenv import load_dotenv
import os

load_dotenv()

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
        <title>Robot Chat</title>
        <style>
            body { font-family: 'Segoe UI', sans-serif; background-color: #1e293b; color: white; text-align: center; padding: 20px; }
            .chatbox { max-width: 800px; margin: auto; background: #334155; padding: 30px; border-radius: 20px; box-shadow: 0 6px 20px rgba(0, 0, 0, 0.3); }
            input[type="text"] { width: 75%; padding: 15px; font-size: 16px; border-radius: 12px; border: none; margin: 10px 0; }
            button { padding: 15px 20px; font-size: 16px; margin: 5px; border-radius: 12px; border: none; background-color: #007bff; color: white; cursor: pointer; transition: background-color 0.3s ease; }
            button:hover { background-color: #0056b3; }
            pre { text-align: left; background: #0f172a; padding: 20px; border-radius: 12px; color: #f1f5f9; overflow-x: auto; margin-top: 20px; font-size: 14px; }
            a.logout { display: inline-block; margin-top: 20px; color: #ff5555; text-decoration: none; font-weight: bold; }
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
                    console.log("🎤 Heard:", transcript);
                    if (transcript.includes("robot")) {
                        console.log("✅ Trigger word 'robot' detected.");
                        recognition.stop();
                        startListening();
                    }
                };

                recognition.onend = function() {
                    if (!listening) {
                        console.log("🔄 Restarting trigger listener...");
                        startTriggerWordListening();
                    }
                };

                recognition.onerror = function(event) {
                    console.log("⚠️ Speech recognition error:", event.error);
                    recognition.stop();
                    setTimeout(startTriggerWordListening, 2000);
                };

                recognition.start();
            }

            function startListening() {
                listening = true;
                const recognition = new (window.SpeechRecognition || window.webkitSpeechRecognition)();
                recognition.lang = 'en-US';
                recognition.interimResults = false;

                recognition.onresult = function(event) {
                    const command = event.results[0][0].transcript.trim();
                    console.log("🎙️ Command received:", command);
                    document.getElementById('command').value = command;
                    listening = false;
                    document.getElementById('commandForm').requestSubmit();
                };

                recognition.onend = function() {
                    listening = false;
                    startTriggerWordListening();
                };

                recognition.onerror = function(event) {
                    console.log("⚠️ Error during command listening:", event.error);
                    listening = false;
                    startTriggerWordListening();
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
            <h1>🤖 Robot Chat</h1>
            <form id="commandForm" method="POST" action="/send_command">
                <input type="text" id="command" name="command" placeholder="Enter movement command..." required>
                <div>
                    <button type="button" onclick="startListening()">🎤 Speak</button>
                    <button type="submit">Send</button>
                </div>
            </form>
            <h2>🧠 Generated Robot Commands:</h2>
            <pre id="response"></pre>
            <a class="logout" href="/logout">🚪 Logout</a>
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
