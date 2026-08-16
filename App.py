import csv
import io
import time
import json
import base64
import requests
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
# مفتاح سري لتشفير الجلسات (Sessions)
app.secret_key = 'amr_suhail_academy_secure_secret_key_2026'

# كلمة مرور المشرف/المعلم
ADMIN_PASSWORD = "123"

# إعداد قاعدة بيانات محلية مدمجة
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///academy_qa.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# جدول الأسئلة والأجوبة
class LessonQuestion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    student_name = db.Column(db.String(100), nullable=False, default="طالب")
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text, nullable=True)
    is_answered = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "student_name": self.student_name,
            "question_text": self.question_text,
            "answer_text": self.answer_text,
            "is_answered": self.is_answered,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M")
        }

# جدول تحديثات المنصة (ما الجديد)
class PlatformUpdate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    tag = db.Column(db.String(50), default="ميزة جديدة")
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "tag": self.tag,
            "description": self.description,
            "date": self.created_at.strftime("%Y-%m-%d")
        }

# جدول المحتوى والدروس المضافة مباشرة من لوحة المعلم
class CustomLesson(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    content_type = db.Column(db.String(20), nullable=False) # 'videos' or 'files'
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text, nullable=False)
    download_url = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "year": self.year,
            "term": self.term,
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "content_type": self.content_type,
            "title": self.title,
            "url": self.url,
            "download_url": self.download_url or "",
            "notes": self.notes or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M")
        }

# إنشاء قاعدة البيانات تلقائياً
with app.app_context():
    db.create_all()
    if PlatformUpdate.query.count() == 0:
        initial_update = PlatformUpdate(
            title="إطلاق نظام الأسئلة والشريك الرسمي",
            tag="تحديث رئيسي 🚀",
            description="أصبح بإمكانك الآن طرح استفساراتك مباشرة في صفحة كل درس والحصول على إجابة المعلم المعتمدة، كما نرحب بانضمام مكتبة ميكا درايف كشريك رسمي للأكاديمية!"
        )
        db.session.add(initial_update)
        db.session.commit()

# حماية مسارات المشرف
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({"status": "error", "message": "غير مصرح لك بالوصول"}), 403
        return f(*args, **kwargs)
    return decorated_function

# مفتاح API والنماذج
AI_API_KEY = "AQ.Ab8RN6JVJWxcADzJkxYA2To2c6k4yFw8FN9LS5zw7yeuS42Ghw"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={AI_API_KEY}"
PLANNER_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={AI_API_KEY}"

# الروابط للورقتين
CSV_URLS = [
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=0",
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=1031832429"
]

def send_gemini_request(url, payload, max_retries=3, timeout=40):
    headers = {'Content-Type': 'application/json'}
    last_error = "المساعد مشغول حالياً، يرجى المحاولة بعد لحظات."

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if response.status_code == 200:
                res_json = response.json()
                candidates = res_json.get('candidates', [])
                if candidates and 'content' in candidates[0]:
                    parts = candidates[0]['content'].get('parts', [])
                    if parts and 'text' in parts[0]:
                        return parts[0]['text'], None
                return None, "استجابة غير مكتملة من النموذج."

            if response.status_code in [429, 500, 503, 504]:
                time.sleep(1.5 * (attempt + 1))
                continue
            else:
                print("API Error Response:", response.status_code, response.text)
                last_error = f"خطأ من الخادم: {response.status_code}"
                break
        except requests.exceptions.Timeout:
            time.sleep(1)
            last_error = "استغرقت المعالجة وقتاً طويلاً، يرجى إعادة المحاولة."
            continue
        except requests.exceptions.RequestException as e:
            time.sleep(1)
            last_error = str(e)
            continue

    return None, last_error

# --- مسار تفعيل التطبيق (PWA) ---
@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

def get_data_from_sheet():
    data = {}
    try:
        # 1. جلب البيانات من Google Sheets
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

        # 2. دمج المحتوى المضاف مباشرة من لوحة المعلم
        custom_items = CustomLesson.query.all()
        for item in custom_items:
            c_year = item.year.strip()
            c_term = item.term.strip()
            c_subject = item.subject_name.strip()
            c_chapter = item.chapter_name.strip() or "عام"
            c_type = item.content_type.strip()

            if c_year not in data: data[c_year] = {"name": c_year, "terms": {}}
            if c_term not in data[c_year]["terms"]: data[c_year]["terms"][c_term] = {"name": c_term, "subjects": {}}
            if c_subject not in data[c_year]["terms"][c_term]["subjects"]:
                data[c_year]["terms"][c_term]["subjects"][c_subject] = {
                    "id": f"{c_year}_{c_term}_{c_subject}".replace(" ", "_"),
                    "name": c_subject,
                    "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}
                }

            subj_data = data[c_year]["terms"][c_term]["subjects"][c_subject]

            if c_type == "videos":
                if c_chapter not in subj_data["types"]["videos"]["chapters"]:
                    subj_data["types"]["videos"]["chapters"][c_chapter] = {"name": c_chapter, "lessons": []}
                subj_data["types"]["videos"]["chapters"][c_chapter]["lessons"].append({
                    "title": item.title, "video": item.url, "download": item.download_url, "notes": item.notes, "custom_id": item.id
                })
            elif c_type == "files":
                if c_chapter not in subj_data["types"]["files"]["chapters"]:
                    subj_data["types"]["files"]["chapters"][c_chapter] = {"name": c_chapter, "lessons": []}
                subj_data["types"]["files"]["chapters"][c_chapter]["lessons"].append({
                    "title": item.title, "file": item.url, "notes": item.notes, "custom_id": item.id
                })

        # 3. تحويل القواميس إلى مصفوفات
        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
                    s["types"]["quiz"]["chapters"] = list(s["types"]["quiz"]["chapters"].values())

    except Exception as e:
        print(f"Error fetching CSV or custom data: {e}")
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

        is_admin = session.get('is_admin', False)

        if subject_id and content_type and chapter_name:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            if content_type in s["types"]:
                                for c in s["types"][content_type]["chapters"]:
                                    if c["name"] == chapter_name:
                                        if is_admin:
                                            questions = LessonQuestion.query.filter_by(
                                                subject_id=subject_id,
                                                chapter_name=chapter_name
                                            ).order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
                                        else:
                                            questions = LessonQuestion.query.filter_by(
                                                subject_id=subject_id,
                                                chapter_name=chapter_name,
                                                is_answered=True
                                            ).order_by(LessonQuestion.created_at.desc()).all()

                                        return render_template(
                                            'index.html', 
                                            view='content', 
                                            subject=s, 
                                            chapter=c, 
                                            content_type=content_type,
                                            questions=questions,
                                            is_admin=is_admin
                                        )

        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            if content_type in s["types"]:
                                return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], is_admin=is_admin)

        if subject_id:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id:
                            return render_template('index.html', view='select_type', subject=s, is_admin=is_admin)

        if year and term:
            if year in DATA and term in DATA[year]["terms"]:
                return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year, is_admin=is_admin)

        if year:
            if year in DATA:
                return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year, is_admin=is_admin)

        return render_template('index.html', view='years', data=DATA, is_admin=is_admin)
    except Exception as e:
        return f"Error: {str(e)}"

# ==========================================
# مسارات الإضافة المباشرة للدروس والملفات
# ==========================================
@app.route('/admin/add-content', methods=['POST'])
@admin_required
def add_custom_content():
    try:
        data = request.json or request.form
        year = data.get('year', '').strip()
        term = data.get('term', '').strip()
        subject_name = data.get('subject_name', '').strip()
        chapter_name = data.get('chapter_name', '').strip() or 'عام'
        content_type = data.get('content_type', 'videos').strip()
        title = data.get('title', '').strip()
        url = data.get('url', '').strip()
        download_url = data.get('download_url', '').strip()
        notes = data.get('notes', '').strip()

        if not year or not term or not subject_name or not title or not url:
            return jsonify({"status": "error", "message": "يرجى تعبئة الحقول الأساسية (السنة، الترم، المادة، العنوان، الرابط)"}), 400

        new_lesson = CustomLesson(
            year=year,
            term=term,
            subject_name=subject_name,
            chapter_name=chapter_name,
            content_type=content_type,
            title=title,
            url=url,
            download_url=download_url,
            notes=notes
        )
        db.session.add(new_lesson)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إضافة المحتوى بنجاح وظهوره في المنصة فوراً!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/custom-content', methods=['GET'])
@admin_required
def get_custom_content():
    lessons = CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()
    return jsonify([l.to_dict() for l in lessons])

@app.route('/admin/delete-content/<int:content_id>', methods=['POST', 'DELETE'])
@admin_required
def delete_custom_content(content_id):
    try:
        lesson = CustomLesson.query.get_or_404(content_id)
        db.session.delete(lesson)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف العنصر بنجاح"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# مسارات التحديثات والإشعارات (What's New API)
# ==========================================
@app.route('/api/updates', methods=['GET'])
def get_updates():
    updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
    return jsonify([u.to_dict() for u in updates])

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_update():
    try:
        data = request.json or request.form
        title = data.get('title', '').strip()
        tag = data.get('tag', 'ميزة جديدة').strip()
        description = data.get('description', '').strip()

        if not title or not description:
            return jsonify({"status": "error", "message": "العنوان والتفاصيل مطلوبة"}), 400

        new_update = PlatformUpdate(title=title, tag=tag, description=description)
        db.session.add(new_update)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر التحديث الجديد بنجاح!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-update/<int:update_id>', methods=['POST', 'DELETE'])
@admin_required
def delete_update(update_id):
    try:
        up = PlatformUpdate.query.get_or_404(update_id)
        db.session.delete(up)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف التحديث"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# مصادقة وإدارة المعلم (Admin Auth & Q&A)
# ==========================================
@app.route('/admin/login', methods=['POST'])
def admin_login():
    data = request.json or {}
    password = data.get('password', '').strip()
    if password == ADMIN_PASSWORD:
        session['is_admin'] = True
        return jsonify({"status": "success", "message": "تم تسجيل دخول المعلم بنجاح"})
    return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401

@app.route('/admin/logout', methods=['POST', 'GET'])
def admin_logout():
    session.pop('is_admin', None)
    return redirect(request.referrer or '/')

@app.route('/admin/status', methods=['GET'])
def admin_status():
    return jsonify({"is_admin": session.get('is_admin', False)})

@app.route('/ask-question', methods=['POST'])
def submit_question():
    try:
        subject_id = request.form.get('subject_id') or (request.json.get('subject_id') if request.is_json else None)
        chapter_name = request.form.get('chapter_name') or (request.json.get('chapter_name') if request.is_json else None)
        student_name = (request.form.get('student_name') if not request.is_json else request.json.get('student_name')) or 'طالب'
        question_text = (request.form.get('question_text') if not request.is_json else request.json.get('question_text')) or ''

        student_name = student_name.strip() or 'طالب'
        question_text = question_text.strip()

        if not question_text or not subject_id or not chapter_name:
            return jsonify({"status": "error", "message": "بيانات غير مكتملة"}), 400

        new_q = LessonQuestion(
            subject_id=subject_id,
            chapter_name=chapter_name,
            student_name=student_name,
            question_text=question_text
        )
        db.session.add(new_q)
        db.session.commit()

        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({"status": "success", "message": "تم إرسال السؤال بنجاح"})
        return redirect(request.referrer or '/')
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/questions', methods=['GET'])
@admin_required
def get_pending_questions():
    pending = LessonQuestion.query.filter_by(is_answered=False).order_by(LessonQuestion.created_at.desc()).all()
    return jsonify([q.to_dict() for q in pending])

@app.route('/admin/reply/<int:question_id>', methods=['POST'])
@admin_required
def reply_question(question_id):
    try:
        question = LessonQuestion.query.get_or_404(question_id)
        reply_text = request.form.get('answer_text') or (request.json.get('answer_text') if request.is_json else None)
        
        if not reply_text or not reply_text.strip():
            return jsonify({"status": "error", "message": "نص الرد مطلوب"}), 400

        question.answer_text = reply_text.strip()
        question.is_answered = True
        db.session.commit()

        if request.is_json:
            return jsonify({"status": "success", "message": "تم نشر الرد بنجاح"})
        return redirect(request.referrer or '/')
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-question/<int:question_id>', methods=['POST', 'DELETE'])
@admin_required
def delete_question(question_id):
    try:
        question = LessonQuestion.query.get_or_404(question_id)
        db.session.delete(question)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف السؤال بنجاح"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# مسارات الذكاء الاصطناعي (Gemini AI)
# ==========================================
@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        user_message = ""
        parts = []

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
                    parts.append({
                        "inlineData": {
                            "mimeType": mime_type,
                            "data": image_b64
                        }
                    })

        if not user_message and not parts:
            return jsonify({"reply": "الرجاء كتابة سؤال أو إرفاق صورة."})

        prompt_text = "أنت مساعد Amr Suhail Academy الأكاديمي. اشرح للطالب بأسلوب هندسي وفيزيائي مبسط وواضح. اكتب القوانين والمعادلات بصيغة LaTeX مستخدماً علامات $$ للمعادلات المستقلة و $ للمعادلات المضمنة.\n"
        if user_message:
            prompt_text += f"سؤال الطالب: {user_message}"
        else:
            prompt_text += "اشرح محتوى هذه الصورة والمسائل أو المخططات الموجودة فيها بالتفصيل وبأسلوب بسيط وواضح."

        parts.insert(0, {"text": prompt_text}) 
        payload = {"contents": [{"parts": parts}]}

        reply_text, error = send_gemini_request(API_URL, payload, max_retries=3, timeout=40)

        if reply_text:
            return jsonify({"reply": reply_text})
        else:
            return jsonify({"reply": error or "المساعد مشغول حالياً، يرجى المحاولة بعد لحظات."}), 500
            
    except Exception as e:
        print("Server Exception:", str(e))
        return jsonify({"reply": f"تعذر الاتصال بالخادم حالياً: {str(e)}"}), 500

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
        f"{quran_clause} أرجع JSON حصراً بهيكل plans يحتوي على day_name و date و is_off_day و tasks التي تحوي title و allocated_hours."
    )
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    
    try:
        reply_text, error = send_gemini_request(PLANNER_API_URL, payload, max_retries=3, timeout=40)
        if reply_text:
            cleaned_json = reply_text.replace('```json', '').replace('```', '').strip()
            return jsonify(json.loads(cleaned_json))
        return jsonify({"error": error or "فشل في توليد الخطة"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    data = request.json or {}
    prompt = (
        f"أعد دمج المهام التي لم تنجز: {json.dumps(data.get('missed_tasks', []), ensure_ascii=False)} "
        f"ضمن الأيام القادمة: {json.dumps(data.get('remaining_plans', []), ensure_ascii=False)} "
        f"أرجع JSON للمصفوفة المحدثة حصراً."
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"response_mime_type": "application/json"}
    }
    try:
        reply_text, error = send_gemini_request(PLANNER_API_URL, payload, max_retries=3, timeout=40)
        if reply_text:
            cleaned_json = reply_text.replace('```json', '').replace('```', '').strip()
            return jsonify(json.loads(cleaned_json))
        return jsonify({"error": error or "فشل في إعادة الجدولة"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(debug=True)
