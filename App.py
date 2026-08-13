import csv
import io
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# مفتاح الـ API الخاص بك
AI_API_KEY = "AQ.Ab8RN6JVJWxcADzJkxYA2To2c6k4yFw8FN9LS5zw7yeuS42Ghw"
API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

def get_data_from_sheet():
    data = {}
    try:
        response = requests.get(CSV_URL, timeout=10)
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
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject, "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}}
                }
            subj_data = data[year]["terms"][term]["subjects"][subject]
            
            if "فيديو" in content_type:
                if chapter not in subj_data["types"]["videos"]["chapters"]: subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["videos"]["chapters"][chapter]["lessons"].append({"title": title, "video": video_url, "download": download_url})
            elif "ملف" in content_type:
                if chapter not in subj_data["types"]["files"]["chapters"]: subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({"title": title, "file": download_url})

        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
    except Exception as e:
        print(f"Error fetching CSV: {e}")
    return data

@app.route('/')
def home():
    try:
        DATA = get_data_from_sheet()
        year = request.args.get('year')
        term = request.args.get('term')
        subject_id = request.args.get('subject')
        content_type = request.args.get('type')
        chapter_name = request.args.get('chapter')

        # 1. عرض محتوى الدروس (فيديوهات أو ملفات)
        if subject_id and content_type and chapter_name:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            for c in s["types"][content_type]["chapters"]:
                                if c["name"] == chapter_name:
                                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type)

        # 2. عرض الشباتر/الفصول داخل المادة
        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"])

        # 3. اختيار نوع المحتوى (فيديو أو ملفات) للمادة
        if subject_id:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            return render_template('index.html', view='select_type', subject=s)

        # 4. عرض المواد داخل الترم
        if year and term:
            if year in DATA and term in DATA[year]["terms"]:
                return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year)

        # 5. عرض الأترام داخل السنة (مثل السنة الثانية -> الترم الأول والثاني)
        if year:
            if year in DATA:
                return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year)

        # 6. الصفحة الرئيسية (عرض السنوات)
        return render_template('index.html', view='years', data=DATA)
    except Exception as e:
        return f"Error: {str(e)}"

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        user_message = request.json.get('message', '')
        if not user_message: return jsonify({"reply": "الرجاء كتابة سؤال."})
        
        headers = {'Content-Type': 'application/json', 'X-goog-api-key': AI_API_KEY}
        prompt_text = (f"أنت مساعد Amr Suhail Academy. اشرح للطالب في الهندسة والفيزياء بأسلوب بسيط جداً. "
                       f"اكتب القوانين والمعادلات بأرقام وحروف عادية واضحة دون أي رموز برمجة معقدة. "
                       f"سؤال الطالب: {user_message}")
        
        payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
        response = requests.post(API_URL, headers=headers, json=payload, timeout=12)
        
        if response.status_code == 200:
            reply_text = response.json()['candidates'][0]['content']['parts'][0]['text']
            return jsonify({"reply": reply_text})
        else:
            return jsonify({"reply": "المساعد مشغول قليلاً، حاول مجدداً."}), 500
    except Exception as e:
        return jsonify({"reply": "تعذر الاتصال بالخادم حالياً."}), 500

if __name__ == '__main__':
    app.run()
