import csv
import io
import requests
from flask import Flask, render_template, request, jsonify
from google import genai

app = Flask(__name__)

# إعداد مفتاح الذكاء الاصطناعي الخاص بك
AI_API_KEY = "AQ.Ab8RN6IhDhOE6PoE3kG_7qenQUwSoVD8vp5GSO1PcjNR4iZvkA"
ai_client = genai.Client(api_key=AI_API_KEY) if AI_API_KEY != "YOUR_GEMINI_API_KEY" else None

CSV_URL = "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv"

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
                k = str(key).strip()
                v = str(val).strip()
                
                if "سنة" in k: year = v
                elif "ترم" in k: term = v
                elif "مادة" in k: subject = v
                elif "شبتر" in k or "فصل" in k: chapter = v
                elif "عنوان" in k or "درس" in k: title = v
                elif "رابط المقطع" in k: video_url = v
                elif "تحميل" in k: download_url = v
                elif "نوع" in k: content_type = v
                elif "رابط الملف" in k: file_url = v

            if not year or not term or not subject or not title:
                continue
            
            if not chapter: chapter = "عام"

            if year not in data:
                data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]:
                data[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in data[year]["terms"][term]["subjects"]:
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject,
                    "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}}
                }

            subj_data = data[year]["terms"][term]["subjects"][subject]

            if "فيديو" in content_type:
                if chapter not in subj_data["types"]["videos"]["chapters"]:
                    subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["videos"]["chapters"][chapter]["lessons"].append({
                    "title": title, "video": video_url, "download": download_url
                })
            elif "ملف" in content_type:
                if chapter not in subj_data["types"]["files"]["chapters"]:
                    subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({
                    "title": title, "file": file_url
                })

        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())

    except Exception as e:
        print("Error fetching data:", e)
    
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
                        if s["id"] == subject_id:
                            for c in s["types"][content_type]["chapters"]:
                                if c["name"] == chapter_name:
                                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type)

        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
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

# مسار استقبال أسئلة الطلاب والرد عبر الذكاء الاصطناعي
@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    if not ai_client:
        return jsonify({"reply": "عذراً، مساعد الذكاء الاصطناعي غير مفعل حالياً."}), 400
    
    try:
        user_message = request.json.get('message', '')
        if not user_message:
            return jsonify({"reply": "الرجاء كتابة سؤال."})
        
        prompt = f"أنت مساعد ذكاء اصطناعي ذكي لـ Amr Suhail Academy. تساعد طلاب الهندسة والفيزياء في شرح الدروس والقوانين بطريقة مبسطة وواضحة باللغة العربية. سؤال الطالب هو: {user_message}"
        
        response = ai_client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt
        )
        return jsonify({"reply": response.text})
    except Exception as e:
        return jsonify({"reply": f"حدث خطأ أثناء معالجة الطلب: {str(e)}"}), 500

if __name__ == '__main__':
    app.run()
