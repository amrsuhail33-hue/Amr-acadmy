import csv
import io
import time
import json
import base64
import requests
from flask import Flask, render_template, request, jsonify, send_from_directory

app = Flask(__name__)

# مفتاح API والنماذج المحدثة والمستقرة
AI_API_KEY = "AQ.Ab8RN6JVJWxcADzJkxYA2To2c6k4yFw8FN9LS5zw7yeuS42Ghw"

# النماذج الرسمية السريعة والاحتياطية
PRIMARY_CHAT_MODEL = "gemini-2.5-flash"
FALLBACK_CHAT_MODEL = "gemini-2.5-flash-lite"
PLANNER_MODEL = "gemini-2.5-flash-lite"

# الروابط للورقتين
CSV_URLS = [
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=0",
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=1031832429"
]

def make_gemini_request(payload, primary_model=PRIMARY_CHAT_MODEL, fallback_model=FALLBACK_CHAT_MODEL, max_retries=2):
    """
    دالة موحدة لإرسال الطلبات مع إعادة المحاولة التلقائية والتحويل للنموذج الاحتياطي عند الازدحام.
    """
    models_to_try = [primary_model]
    if fallback_model and fallback_model != primary_model:
        models_to_try.append(fallback_model)

    headers = {'Content-Type': 'application/json'}

    for model in models_to_try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={AI_API_KEY}"
        
        for attempt in range(max_retries):
            try:
                response = requests.post(url, headers=headers, json=payload, timeout=25)
                
                # نجاح
                if response.status_code == 200:
                    res_json = response.json()
                    candidates = res_json.get('candidates', [])
                    if candidates and 'content' in candidates[0]:
                        parts = candidates[0]['content'].get('parts', [])
                        if parts and 'text' in parts[0]:
                            return parts[0]['text'], None
                    return None, "استجابة غير مكتملة من النموذج."

                # خطأ ازدحام أو كثرة طلبات (429 أو 503) -> انتظار تصاعدي وإعادة المحاولة
                if response.status_code in [429, 500, 503, 504]:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                else:
                    # أي خطأ آخر، كسر الحلقة وتجربة النموذج الاحتياطي
                    break

            except requests.exceptions.RequestException:
                time.sleep(1)
                continue

    return None, "الخوادم تشهد ضغطاً مؤقتاً، يرجى المحاولة مرة أخرى بعد ثوانٍ."

# --- مسار تفعيل التطبيق (PWA) ---
@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

def get_data_from_sheet():
    data = {}
    try:
        res1 = requests.get(f"{CSV_URLS[0]}&t={int(time.time())}", timeout=10)
        if res1.status_code == 200:
            reader1 = csv.DictReader(io.StringIO(res1.content.decode('utf-8')))
            for row in reader1:
                year=term=subject=chapter=title=content_type=video_url=download_url=file_url=notes=""
                for k, v in row.items():
                    if not k or v is None: continue
                    k = str(k).strip().lower()
                    v = str(v).strip()
                    if "سنة" in k: year = v
                    elif "ترم" in k: term = v
                    elif "مادة" in k: subject = v
                    elif "شبتر" in k or "فصل" in k: chapter = v
                    elif "نوع" in k: content_type = v
                    elif "عنوان" in k or "درس" in k: title = v
                    elif "رابط المقطع" in k: video_url = v
                    elif "تحميل" in k: download_url = v
                    elif "رابط الملف" in k: file_url = v
                    elif "ملاحظ" in k: notes = v

                if not year or not term or not subject or not title: continue
                if not chapter: chapter = "عام"

                if year not in data: data[year] = {"name": year, "terms": {}}
                if term not in data[year]["terms"]: data[year]["terms"][term] = {"name": term, "subjects": {}}
                if subject not in data[year]["terms"][term]["subjects"]:
                    data[year]["terms"][term]["subjects"][subject] = {
                        "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                        "name": subject,
                        "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}
                    }

                subj_data = data[year]["terms"][term]["subjects"][subject]

                if "فيديو" in content_type:
                    if chapter not in subj_data["types"]["videos"]["chapters"]:
                        subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                    subj_data["types"]["videos"]["chapters"][chapter]["lessons"].append({
                        "title": title, "video": video_url, "download": download_url, "notes": notes
                    })
                elif "ملف" in content_type:
                    if chapter not in subj_data["types"]["files"]["chapters"]:
                        subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                    subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({
                        "title": title, "file": file_url or download_url, "notes": notes
                    })

        res2 = requests.get(f"{CSV_URLS[1]}&t={int(time.time())}", timeout=10)
        if res2.status_code == 200:
            reader2 = csv.reader(io.StringIO(res2.content.decode('utf-8')))
            next(reader2, None)
            for row in reader2:
                if len(row) < 4: continue 
                
                year, term, subject, chapter = str(row[0]).strip(), str(row[1]).strip(), str(row[2]).strip(), str(row[3]).strip()

                if not year or not term or not subject or not chapter: continue

                if year not in data: data[year] = {"name": year, "terms": {}}
                if term not in data[year]["terms"]: data[year]["terms"][term] = {"name": term, "subjects": {}}
                if subject not in data[year]["terms"][term]["subjects"]:
                    data[year]["terms"][term]["subjects"][subject] = {
                        "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                        "name": subject,
                        "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}
                    }

                subj_data = data[year]["terms"][term]["subjects"][subject]

                if chapter not in subj_data["types"]["quiz"]["chapters"]:
                    subj_data["types"]["quiz"]["chapters"][chapter] = {
                        "name": chapter, "lessons": [{"title": "بدء الاختبار"}]
                    }

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


# ==========================================
# مسارات الموقع الأساسي
# ==========================================
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
                            if content_type in s["types"]:
                                for c in s["types"][content_type]["chapters"]:
                                    if c["name"] == chapter_name:
                                        return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type)

        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            if content_type in s["types"]:
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
        user_message = ""
        image_parts = []

        if request.is_json:
            user_message = request.json.get('message', '').strip()
        else:
            user_message = request.form.get('message', '').strip()
            if 'file' in request.files:
                file = request.files['file']
                if file and file.filename != '':
                    image_bytes = file.read()
                    image_b64 = base64.b64encode(image_bytes).decode('utf-8')
                    mime_type = file.mimetype or 'image/jpeg'
                    image_parts.append({
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": image_b64
                        }
                    })

        if not user_message and not image_parts:
            return jsonify({"reply": "الرجاء كتابة سؤال أو إرفاق صورة."})

        if not user_message:
            user_message = "اشرح محتوى هذه الصورة والمسائل أو المخططات الموجودة فيها بالتفصيل وبأسلوب هندسي واضح."

        prompt_text = (
            f"أنت مساعد Amr Suhail Academy الأكاديمي. اشرح للطالب بأسلوب هندسي وفيزيائي مبسط وواضح. "
            f"اكتب القوانين والمعادلات بصيغة LaTeX مستخدماً $$ للمعادلات المستقلة و $ للمعادلات المضمنة.\n"
            f"سؤال الطالب: {user_message}"
        )

        parts = [{"text": prompt_text}] + image_parts
        payload = {"contents": [{"parts": parts}]}

        reply_text, error = make_gemini_request(payload, primary_model=PRIMARY_CHAT_MODEL, fallback_model=FALLBACK_CHAT_MODEL)
        
        if reply_text:
            return jsonify({"reply": reply_text})
        else:
            return jsonify({"reply": error or "تعذر استلام رد حالياً."}), 500
            
    except Exception as e:
        return jsonify({"reply": f"حدث خطأ داخلي: {str(e)}"}), 500

# ==========================================
# مسارات المخطط الذكي (Smart Planner)
# ==========================================
@app.route('/planner')
def planner():
    return render_template('planner.html')

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    data = request.json or {}
    quran_clause = f"- أضف مهمة حفظ {data.get('quran_pages')} صفحة قرآنية يومياً." if data.get('quran_enabled') else ""
    prompt = (
        f"أنت نظام أكاديمي احترافي. ولد خطة دراسية من {data.get('current_date')} لـ {data.get('target_date')}. "
        f"المواد: {data.get('subjects')}. الانضباط: {data.get('discipline')}. "
        f"أيام الراحة: {', '.join(data.get('off_days', []))}. الورد القرآني: {'نصف ساعة' if data.get('wird') else 'لا يوجد'}. "
        f"{quran_clause} أرجع مصفوفة JSON حصراً بهيكل plans."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    try:
        reply_text, error = make_gemini_request(payload, primary_model=PLANNER_MODEL, fallback_model=PRIMARY_CHAT_MODEL)
        if reply_text:
            cleaned_json = reply_text.replace('```json', '').replace('```', '').strip()
            return jsonify(json.loads(cleaned_json))
        return jsonify({"error": error or "failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    data = request.json or {}
    prompt = (
        f"أعد دمج المهام التي لم تنجز: {json.dumps(data.get('missed_tasks', []), ensure_ascii=False)} "
        f"ضمن الأيام القادمة: {json.dumps(data.get('remaining_plans', []), ensure_ascii=False)} "
        f"أرجع مصفوفة JSON المحدثة حصراً."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    try:
        reply_text, error = make_gemini_request(payload, primary_model=PLANNER_MODEL, fallback_model=PRIMARY_CHAT_MODEL)
        if reply_text:
            cleaned_json = reply_text.replace('```json', '').replace('```', '').strip()
            return jsonify(json.loads(cleaned_json))
        return jsonify({"error": error or "failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
