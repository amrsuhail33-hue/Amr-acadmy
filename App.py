import csv
import io
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# المفتاح الجديد والمفعل
AI_API_KEY = "AQ.Ab8RN6JVJWxcADzJkxYA2To2c6k4yFw8FN9LS5zw7yeuS42Ghw"
# رابط الـ API المباشر للمودل المستقر
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={AI_API_KEY}"

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

def get_data_from_sheet():
    data = {}
    try:
        response = requests.get(CSV_URL)
        if response.status_code != 200: return {}
        csv_text = response.content.decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(csv_text))
        for row in csv_reader:
            year = term = subject = chapter = title = video_url = download_url = content_type = ""
            for key, val in row.items():
                if not key or val is None: continue
                k = str(key).strip(); v = str(val).strip()
                if "سنة" in k: year = v
                elif "ترم" in k: term = v
                elif "مادة" in k: subject = v
                elif "شبتر" in k or "فصل" in k: chapter = v
                elif "عنوان" in k or "درس" in k: title = v
                elif "رابط المقطع" in k: video_url = v
                elif "تحميل" in k: download_url = v
                elif "نوع" in k: content_type = v
            if not year or not term or not subject or not title: continue
            if not chapter: chapter = "عام"
            if year not in data: data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]: data[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in data[year]["terms"][term]["subjects"]:
                data[year]["terms"][term]["subjects"][subject] = {"id": f"{year}_{term}_{subject}".replace(" ", "_"), "name": subject, "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}}}
            subj_data = data[year]["terms"][term]["subjects"][subject]
            if "فيديو" in content_type:
                if chapter not in subj_data["types"]["videos"]["chapters"]: subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["videos"]["chapters"][chapter]["lessons"].append({"title": title, "video": video_url, "download": download_url})
            elif "ملف" in content_type:
                if chapter not in subj_data["types"]["files"]["chapters"]: subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({"title": title, "file": download_url})
    except: pass
    return data

@app.route('/')
def home():
    DATA = get_data_from_sheet()
    return render_template('index.html', view='years', data=DATA)

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        user_message = request.json.get('message', '')
        if not user_message: return jsonify({"reply": "الرجاء كتابة سؤال."})
        
        headers = {'Content-Type': 'application/json'}
        payload = {
            "contents": [{"parts": [{"text": f"أنت مساعد ذكاء اصطناعي لـ Amr Suhail Academy. تساعد طلاب الهندسة والفيزياء في شرح الدروس والقوانين باللغة العربية. سؤال الطالب: {user_message}"}]}]
        }
        
        response = requests.post(API_URL, headers=headers, json=payload)
        if response.status_code == 200:
            return jsonify({"reply": response.json()['candidates'][0]['content']['parts'][0]['text']})
        else:
            return jsonify({"reply": f"خطأ الاتصال: {response.status_code}"}), 500
    except Exception as e:
        return jsonify({"reply": f"خطأ: {str(e)}"}), 500

if __name__ == '__main__':
    app.run()
