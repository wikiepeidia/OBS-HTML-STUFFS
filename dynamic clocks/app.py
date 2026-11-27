from flask import Flask, jsonify, request
from flask_cors import CORS
import os
from pathlib import Path
import requests
import logging

app = Flask(__name__)
CORS(app)  # Fix lỗi CORS


def _load_dotenv() -> None:
    for parent in Path(__file__).resolve().parents:
        env_path = parent / ".env"
        if env_path.is_file():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("\"'"))
            break


_load_dotenv()
API_KEY = os.getenv("API_KEY") or os.getenv("OWM_KEY", "")  # Thay thế bằng API Key mới

# Set up logging
logging.basicConfig(level=logging.DEBUG)

@app.route('/weather', methods=['GET'])
def get_weather():
    city = request.args.get('city')
    logging.debug(f"Received request for city: {city}")
    if not city:
        return jsonify({"error": "Vui lòng nhập tên thành phố!"}), 400

    url = f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={API_KEY}&units=metric"
    logging.debug(f"Request URL: {url}")
    response = requests.get(url)
    logging.debug(f"Response Status Code: {response.status_code}")
    logging.debug(f"Response JSON: {response.json()}")

    if response.status_code == 200:
        data = response.json()
        weather_data = {
            "city": data["name"],
            "temperature": data["main"]["temp"],
            "humidity": data["main"]["humidity"],
            "description": data["weather"][0]["description"],
            "wind_speed": data["wind"]["speed"],
            "icon": data["weather"][0]["icon"],  # Lấy mã icon
        }
        return jsonify(weather_data)
    else:
        return jsonify({"error": "Không thể lấy dữ liệu thời tiết!"}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)