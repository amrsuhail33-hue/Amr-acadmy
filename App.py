import csv
import io
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# مفتاح الـ API الخاص بك
AI_API_KEY = "AQ.Ab8RN6JVJWxcADzJkxYA2To2c6k4yFw8FN9LS5zw7yeuS42Ghw"

# هذا هو الرابط الذي نجح في الـ cURL الذي جربته أنت
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

# [دالة get_data_from_sheet ودالة home كما هي في الكود السابق بالضبط]
# (قم بنسخ الدوال من الكود السابق وضعها هنا)

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        user_message = request.json.get('message', '')
        if not user_message: return jsonify({"reply": "الرجاء كتابة سؤال."})
        
        # إضافة المفتاح في الـ Headers بدلاً من الرابط (كما فعلت أنت في الـ cURL)
        headers = {
            'Content-Type': 'application/json',
            'X-goog-api-key': AI_API_KEY
        }
        
        payload = {
            "contents": [{
                "parts": [{"text": f"أنت مساعد ذكي لـ Amr Suhail Academy. تساعد طلاب الهندسة والفيزياء في الشرح باللغة العربية. سؤال الطالب: {user_message}"}]
            }]
        }
        
        response = requests.post(API_URL, headers=headers, json=payload)
        
        if response.status_code == 200:
            return jsonify({"reply": response.json()['candidates'][0]['content']['parts'][0]['text']})
        else:
            return jsonify({"reply": f"خطأ الاتصال: {response.status_code}"}), 500
    except Exception as e:
        return jsonify({"reply": f"خطأ داخلي: {str(e)}"}), 500

if __name__ == '__main__':
    app.run()
