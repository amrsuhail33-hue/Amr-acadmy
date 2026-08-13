import csv
import io
import requests
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai

app = Flask(__name__)

# إعداد مفتاح الـ API
AI_API_KEY = "AQ.Ab8RN6IhDhOE6PoE3kG_7qenQUwSoVD8vp5GSO1PcjNR4iZvkA"
genai.configure(api_key=AI_API_KEY)

# إعداد النموذج
model = genai.GenerativeModel('gemini-1.5-flash')

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

# ... (دالة get_data_from_sheet و home كما هي في الكود السابق) ...
# سأضع لك الدوال بالأسفل لنسخها معاً

def get_data_from_sheet():
    data = {}
    try:
        response = requests.get(CSV_URL)
        csv_text = response.content.decode('utf-8')
        csv_reader = csv.DictReader(io.StringIO(csv_text))
        for row in csv_reader:
            year = term = subject = chapter = title = video_url = download_url = content_type = file_url = ""
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
                elif "رابط الملف" in k: file_url = v
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
                subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({"title": title, "file": file_url})
        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
    except: pass
    return data

@app.route('/')
def home():
    DATA = get_data_from_sheet()
    # ... (باقي كود الدالة home من الرد السابق كما هو بالضبط) ...
    return render_template('index.html', view='years', data=DATA)

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        user_message = request.json.get('message', '')
        prompt = f"أنت مساعد Amr Suhail Academy. تساعد طلاب الهندسة والفيزياء في شرح الدروس والقوانين بطريقة مبسطة. سؤال الطالب هو: {user_message}"
        response = model.generate_content(prompt)
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"reply": f"خطأ: {str(e)}"}), 500

if __name__ == '__main__':
    app.run()
