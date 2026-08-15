import csv
import io
import requests
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

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
            year = term = subject = chapter = title = video_url = download_url = content_type = file_url = notes = ""
            question = opt1 = opt2 = opt3 = opt4 = ans_idx = ""
            
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
                elif "رابط الملف" in k: file_url = v
                elif "ملاحظ" in k: notes = v  
                elif "نوع" in k: content_type = v
                elif "Question" in k or "سؤال" in k: question = v
                elif "Option 1" in k: opt1 = v
                elif "Option 2" in k: opt2 = v
                elif "Option 3" in k: opt3 = v
                elif "Option 4" in k: opt4 = v
                elif "Correct Answer" in k or "الإجابة" in k: ans_idx = v
            
            if not year or not term or not subject: continue
            if not chapter: chapter = "عام"

            if year not in data: data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]: data[year]["terms"][term] = {"name": term, "subjects": {}}
            
            if subject not in data[year]["terms"][term]["subjects"]:
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject, 
                    "types": {
                        "videos": {"chapters": {}}, 
                        "files": {"chapters": {}},
                        "quiz": {"chapters": {}}
                    }
                }
            subj_data = data[year]["terms"][term]["subjects"][subject]
            
            # معالجة الفيديوهات
            if "فيديو" in content_type:
                if not title: continue
                if chapter not in subj_data["types"]["videos"]["chapters"]: subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["videos"]["chapters"][chapter]["lessons"].append({
                    "title": title, "video": video_url, "download": download_url, "notes": notes
                })
            # معالجة الملفات
            elif "ملف" in content_type:
                if not title: continue
                if chapter not in subj_data["types"]["files"]["chapters"]: subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({
                    "title": title, "file": file_url or download_url, "notes": notes
                })
            # معالجة الاختبارات (الأسئلة)
            elif "إختبار" in content_type or "اختبار" in content_type:
                if chapter not in subj_data["types"]["quiz"]["chapters"]: 
                    subj_data["types"]["quiz"]["chapters"][chapter] = {"name": chapter, "questions": []}
                if question:
                    try:
                        ans_int = int(float(ans_idx))
                    except:
                        ans_int = 0
                    subj_data["types"]["quiz"]["chapters"][chapter]["questions"].append({
                        "q": question,
                        "opts": [opt1, opt2, opt3, opt4],
                        "ans": ans_int
                    })

        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
                    s["types"]["quiz"]["chapters"] = list(s["types"]["quiz"]["chapters"].values())
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

        if subject_id and content_type and chapter_name:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            for c in s["types"][content_type]["chapters"]:
                                if c["name"] == chapter_name:
                                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type)

        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"])

        if subject_id:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            return render_template('index.html', view='select_type', subject=s)

        if year and term:
            if year in DATA and term in DATA[year]["terms"]:
                return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year)

        if year:
            if year in DATA:
                return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year)

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
