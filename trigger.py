<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Voice Authentication Login</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: #f5f5f5;
            margin: 0;
            padding: 0;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
        }
        .login-container {
            background-color: white;
            border-radius: 10px;
            box-shadow: 0 0 20px rgba(0, 0, 0, 0.1);
            width: 400px;
            padding: 30px;
            text-align: center;
        }
        h1 {
            color: #4a4a4a;
            margin-bottom: 30px;
        }
        .form-group {
            margin-bottom: 20px;
            text-align: left;
        }
        label {
            display: block;
            margin-bottom: 5px;
            color: #666;
            font-weight: 500;
        }
        select {
            width: 100%;
            padding: 12px;
            border: 1px solid #ddd;
            border-radius: 5px;
            font-size: 16px;
            background-color: #f9f9f9;
        }
        .btn {
            background-color: #4285f4;
            color: white;
            border: none;
            border-radius: 5px;
            padding: 12px 20px;
            font-size: 16px;
            cursor: pointer;
            width: 100%;
            transition: background-color 0.3s;
        }
        .btn:hover {
            background-color: #357ae8;
        }
        .btn:disabled {
            background-color: #cccccc;
            cursor: not-allowed;
        }
        .voice-controls {
            display: flex;
            flex-direction: column;
            align-items: center;
            margin-top: 20px;
        }
        .mic-btn {
            background-color: #4285f4;
            color: white;
            border: none;
            border-radius: 50%;
            width: 80px;
            height: 80px;
            font-size: 24px;
            cursor: pointer;
            margin-bottom: 15px;
            display: flex;
            align-items: center;
            justify-content: center;
            transition: all 0.3s;
        }
        .mic-btn:hover {
            background-color: #357ae8;
            transform: scale(1.05);
        }
        .mic-btn.recording {
            background-color: #ea4335;
            animation: pulse 1.5s infinite;
        }
        @keyframes pulse {
            0% {
                transform: scale(1);
            }
            50% {
                transform: scale(1.1);
            }
            100% {
                transform: scale(1);
            }
        }
        .status-message {
            height: 50px;
            margin-top: 15px;
            color: #666;
            font-weight: 500;
        }
        .login-result {
            margin-top: 20px;
            padding: 10px;
            border-radius: 5px;
            display: none;
        }
        .success {
            background-color: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .error {
            background-color: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .icon {
            font-size: 24px;
            margin-right: 8px;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <h1>Voice Authentication</h1>
        
        <div class="form-group">
            <label for="username">Select User</label>
            <select id="username">
                <option value="">Choose a user</option>
                <option value="aya">Aya</option>
                <option value="maen">Maen</option>
                <option value="tima">Tima</option>
                <option value="layan">Layan</option>
            </select>
        </div>
        
        <div class="voice-controls">
            <button id="recordButton" class="mic-btn" disabled>
                <span class="icon">🎤</span>
            </button>
            <div id="statusMessage" class="status-message">Please select a user first</div>
        </div>
        
        <div id="loginResult" class="login-result"></div>
    </div>

    <script>
        // User voice patterns (in a real app, these would be securely stored)
        const userProfiles = {
            aya: { passphrase: "my name is aya", confidence: 0.7 },
            maen: { passphrase: "my name is maen", confidence: 0.7 },
            tima: { passphrase: "my name is tima", confidence: 0.7 },
            layan: { passphrase: "my name is layan", confidence: 0.7 }
        };
        
        // DOM elements
        const usernameSelect = document.getElementById('username');
        const recordButton = document.getElementById('recordButton');
        const statusMessage = document.getElementById('statusMessage');
        const loginResult = document.getElementById('loginResult');
        
        // Speech recognition setup
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        let recognition;
        
        if (SpeechRecognition) {
            recognition = new SpeechRecognition();
            recognition.continuous = false;
            recognition.interimResults = false;
            recognition.lang = 'en-US';
            
            recognition.onstart = function() {
                recordButton.classList.add('recording');
                statusMessage.textContent = "Listening...";
                loginResult.style.display = 'none';
            };
            
            recognition.onresult = function(event) {
                const transcript = event.results[0][0].transcript.toLowerCase();
                const confidence = event.results[0][0].confidence;
                const selectedUser = usernameSelect.value;
                
                console.log(`Transcript: ${transcript}`);
                console.log(`Confidence: ${confidence}`);
                
                verifyVoice(selectedUser, transcript, confidence);
            };
            
            recognition.onerror = function(event) {
                recordButton.classList.remove('recording');
                statusMessage.textContent = `Error: ${event.error}`;
            };
            
            recognition.onend = function() {
                recordButton.classList.remove('recording');
            };
        } else {
            statusMessage.textContent = "Voice recognition not supported in this browser";
            recordButton.disabled = true;
        }
        
        // Event listeners
        usernameSelect.addEventListener('change', function() {
            if (this.value) {
                recordButton.disabled = false;
                const user = userProfiles[this.value];
                statusMessage.textContent = `Press the microphone and say: "${user.passphrase}"`;
            } else {
                recordButton.disabled = true;
                statusMessage.textContent = "Please select a user first";
            }
        });
        
        recordButton.addEventListener('click', function() {
            if (recognition) {
                try {
                    recognition.start();
                } catch (e) {
                    console.error('Recognition already started:', e);
                    recognition.stop();
                }
            }
        });
        
        // Voice verification function
        function verifyVoice(username, transcript, confidence) {
            const user = userProfiles[username];
            const expectedPhrase = user.passphrase;
            
            // Simple verification logic (in a real app, use more sophisticated voice biometrics)
            const phraseMatch = transcript.includes(expectedPhrase);
            const confidenceThreshold = user.confidence;
            
            if (phraseMatch && confidence >= confidenceThreshold) {
                authenticateSuccess(username);
            } else if (!phraseMatch) {
                authenticateFail("Voice passphrase doesn't match. Please try again.");
            } else {
                authenticateFail("Voice confidence too low. Please speak clearer and try again.");
            }
        }
        
        function authenticateSuccess(username) {
            statusMessage.textContent = "Voice authenticated!";
            loginResult.textContent = `Welcome, ${username.charAt(0).toUpperCase() + username.slice(1)}! You have been successfully logged in.`;
            loginResult.className = "login-result success";
            loginResult.style.display = 'block';
            
            // In a real app, redirect or grant access here
            setTimeout(() => {
                alert(`Authentication successful for ${username}. Redirecting to dashboard...`);
                // window.location.href = '/dashboard.html';
            }, 1500);
        }
        
        function authenticateFail(message) {
            statusMessage.textContent = "Authentication failed";
            loginResult.textContent = message;
            loginResult.className = "login-result error";
            loginResult.style.display = 'block';
        }
    </script>
</body>
</html>
