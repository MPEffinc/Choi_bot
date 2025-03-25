from flask import Flask, request
import os

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    if 'ref' in data and data['ref'] == 'refs/heads/main':
        os.system("~/choi_bot/update_and_restart.sh")
        return "Updated!", 200
    return "No changes", 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

