import os
import csv
import io
import time
import json
import base64
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'amr_suhail_academy_secure_secret_key_2026')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# إعداد قاعدة البيانات التلقائي ومعالجة الرابط لـ Supabase/Render
db_url = os.environ.get('DATABASE_URL', 'sqlite:///academy_qa.db').strip()
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql+psycopg2://", 1)
elif db_url.startswith("postgresql://") and not db_url.startswith("postgresql+psycopg2://"):
    db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# كلمات المرور للصلاحيات
ADMIN_PASSWORD = "123"
ASSISTANT_PASSWORD = "AhmedAI"  # تم تعديل كلمة مرور المساعد

db = SQLAlchemy(app)

# ==========================================
# Database Models
# ==========================================
class LessonQuestion(db.Model):
    __tablename__ = 'lesson_questions'
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

class PlatformUpdate(db.Model):
    __tablename__ = 'platform_updates'
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

class CustomLesson(db.Model):
    __tablename__ = 'custom_lessons'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    content_type = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text, nullable=True)
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
            "url": self.url or "",
            "download_url": self.download_url or "",
            "notes": self.notes or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M")
        }

with app.app_context():
    db.create_all()
    if PlatformUpdate.query.count() == 0:
        db.session.add(PlatformUpdate(
            title="إطلاق نظام الأسئلة والشريك الرسمي",
            tag="تحديث رئيسي 🚀",
            description="أصبح بإمكانك الآن طرح استفساراتك مباشرة في صفحة كل درس والحصول على إجابة المعلم المعتمدة."
        ))
        db.session.commit()

# ==========================================
# Auth Decorators
# ==========================================
def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({"status": "error", "message": "غير مصرح لك بالوصول"}), 403
        return f(*args, **kwargs)
    return decorated_function

def can_manage_content(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # مسموح للمسؤول أو المساعد
        if not (session.get('is_admin') or session.get('is_assistant')):
            return jsonify({"status": "error", "message": "غير مصرح لك برفع أو تعديل المحتوى"}), 403
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# AI APIs & Sheets
# ==========================================
AI_API_KEY = "AQ.Ab8RN6JVJWxcADzJkxYA2To2c6k4yFw8FN9LS5zw7yeuS42Ghw"
API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent?key={AI_API_KEY}"
PLANNER_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent?key={AI_API_KEY}"

CSV_URLS = [
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=0",
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=1031832429"
]

def send_gemini_request(url, payload, max_retries=3, timeout=40):
    headers = {'Content-Type': 'application/json'}
    last_error = "المساعد مشغول حالياً."
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
                return None, "استجابة غير مكتملة."
            if response.status_code in [429, 500, 503, 504]:
                time.sleep(1.5 * (attempt + 1))
                continue
            else:
                last_error = f"خطأ من الخادم: {response.status_code}"
                break
        except Exception as e:
            time.sleep(1)
            last_error = str(e)
            continue
    return None, last_error

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
                year, term, subject, chapter, content_type, title, video_url, download_url, file_url, notes = "", "", "", "", "", "", "", "", "", ""
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
                    if chapter not in subj_data["types"]["videos"]["chapters"]: subj_data["types"]["videos"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                    subj_data["types"]["videos"]["chapters"][chapter]["lessons"].append({"title": title, "video": video_url, "download": download_url, "notes": notes})
                elif "ملف" in content_type:
                    if chapter not in subj_data["types"]["files"]["chapters"]: subj_data["types"]["files"]["chapters"][chapter] = {"name": chapter, "lessons": []}
                    subj_data["types"]["files"]["chapters"][chapter]["lessons"].append({"title": title, "file": file_url or download_url, "notes": notes})

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
                    data[year]["terms"][term]["subjects"][subject] = {"id": f"{year}_{term}_{subject}".replace(" ", "_"), "name": subject, "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}}
                subj_data = data[year]["terms"][term]["subjects"][subject]
                if chapter not in subj_data["types"]["quiz"]["chapters"]: subj_data["types"]["quiz"]["chapters"][chapter] = {"name": chapter, "lessons": [{"title": "بدء الاختبار"}]}

        # دمج الدروس المضافة يدوياً من قاعدة البيانات
        for item in CustomLesson.query.all():
            c_year, c_term, c_subject, c_chapter, c_type = item.year.strip(), item.term.strip(), item.subject_name.strip(), item.chapter_name.strip() or "عام", item.content_type.strip()
            if c_year not in data: data[c_year] = {"name": c_year, "terms": {}}
            if c_term not in data[c_year]["terms"]: data[c_year]["terms"][c_term] = {"name": c_term, "subjects": {}}
            if c_subject not in data[c_year]["terms"][c_term]["subjects"]:
                data[c_year]["terms"][c_term]["subjects"][c_subject] = {"id": f"{c_year}_{c_term}_{c_subject}".replace(" ", "_"),"name": c_subject,"types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}}
            subj_data = data[c_year]["terms"][c_term]["subjects"][c_subject]
            if c_type == "videos":
                if c_chapter not in subj_data["types"]["videos"]["chapters"]: subj_data["types"]["videos"]["chapters"][c_chapter] = {"name": c_chapter, "lessons": []}
                subj_data["types"]["videos"]["chapters"][c_chapter]["lessons"].append({"title": item.title, "video": item.url, "download": item.download_url, "notes": item.notes, "custom_id": item.id})
            elif c_type == "files":
                if c_chapter not in subj_data["types"]["files"]["chapters"]: subj_data["types"]["files"]["chapters"][c_chapter] = {"name": c_chapter, "lessons": []}
                subj_data["types"]["files"]["chapters"][c_chapter]["lessons"].append({"title": item.title, "file": item.url, "download": item.download_url, "notes": item.notes, "custom_id": item.id})

        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
                    s["types"]["quiz"]["chapters"] = list(s["types"]["quiz"]["chapters"].values())
    except Exception as e: print(f"Error fetching data: {e}")
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
        is_admin = session.get('is_admin', False)
        is_assistant = session.get('is_assistant', False)

        if subject_id and content_type and chapter_name:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            for c in s["types"][content_type]["chapters"]:
                                if c["name"] == chapter_name:
                                    if is_admin: questions = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name).order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
                                    else: questions = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name, is_answered=True).order_by(LessonQuestion.created_at.desc()).all()
                                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, is_admin=is_admin, is_assistant=is_assistant)
        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], is_admin=is_admin, is_assistant=is_assistant)
        if subject_id:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id: return render_template('index.html', view='select_type', subject=s, is_admin=is_admin, is_assistant=is_assistant)
        if year and term:
            if year in DATA and term in DATA[year]["terms"]: return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year, is_admin=is_admin, is_assistant=is_assistant)
        if year:
            if year in DATA: return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year, is_admin=is_admin, is_assistant=is_assistant)

        return render_template('index.html', view='years', data=DATA, is_admin=is_admin, is_assistant=is_assistant)
    except Exception as e: return f"Error: {str(e)}"

# ==========================================
# مصادقة الإدارة والمساعد
# ==========================================
@app.route('/admin/login', methods=['POST'])
def admin_login():
    data = request.json or request.form or {}
    pwd = data.get('password', '').strip()
    session.permanent = True

    if pwd == ADMIN_PASSWORD:
        session['is_admin'] = True
        session['is_assistant'] = False
        return jsonify({"status": "success", "role": "admin", "message": "تم تسجيل دخول المسؤول بنجاح"})
    elif pwd == ASSISTANT_PASSWORD:
        session['is_admin'] = False
        session['is_assistant'] = True
        return jsonify({"status": "success", "role": "assistant", "message": "تم تسجيل دخول المساعد بنجاح"})

    return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401

@app.route('/admin/logout', methods=['POST', 'GET'])
def admin_logout():
    session.pop('is_admin', None)
    session.pop('is_assistant', None)
    return redirect('/')

@app.route('/admin/status', methods=['GET'])
def admin_status(): 
    return jsonify({
        "is_admin": session.get('is_admin', False),
        "is_assistant": session.get('is_assistant', False)
    })

# ==========================================
# إدارة ورفع وتعديل المحتوى (للمسؤول والمساعد)
# ==========================================
@app.route('/admin/add-content', methods=['POST'])
@can_manage_content
def add_custom_content():
    try:
        data = request.json or request.form
        year, term, subject_name = data.get('year', '').strip(), data.get('term', '').strip(), data.get('subject_name', '').strip()
        chapter_name, content_type, title = data.get('chapter_name', '').strip() or 'عام', data.get('content_type', 'videos').strip(), data.get('title', '').strip()
        url, download_url, notes = data.get('url', '').strip(), data.get('download_url', '').strip(), data.get('notes', '').strip()

        if not year or not term or not subject_name or not title: return jsonify({"status": "error", "message": "البيانات الأساسية مطلوبة"}), 400
        if not url and not download_url: return jsonify({"status": "error", "message": "يجب إرفاق رابط مشاهدة/ملف أو رابط تحميل"}), 400

        db.session.add(CustomLesson(year=year, term=term, subject_name=subject_name, chapter_name=chapter_name, content_type=content_type, title=title, url=url, download_url=download_url, notes=notes))
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت الإضافة بنجاح وتحديث الموقع فورياً!"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

# دالة التعديل التي كانت مفقودة وتسبب الخطأ
@app.route('/admin/edit-content/<int:content_id>', methods=['POST'])
@can_manage_content
def edit_custom_content(content_id):
    try:
        item = CustomLesson.query.get_or_404(content_id)
        data = request.json or request.form
        
        item.year = data.get('year', '').strip()
        item.term = data.get('term', '').strip()
        item.subject_name = data.get('subject_name', '').strip()
        item.chapter_name = data.get('chapter_name', '').strip() or 'عام'
        item.content_type = data.get('content_type', 'videos').strip()
        item.title = data.get('title', '').strip()
        item.url = data.get('url', '').strip()
        item.download_url = data.get('download_url', '').strip()
        item.notes = data.get('notes', '').strip()
        
        if not item.year or not item.term or not item.subject_name or not item.title:
            return jsonify({"status": "error", "message": "البيانات الأساسية مطلوبة"}), 400
            
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث المحتوى بنجاح!"})
    except Exception as e: 
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/custom-content', methods=['GET'])
@can_manage_content
def get_custom_content(): return jsonify([l.to_dict() for l in CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()])

@app.route('/admin/delete-content/<int:content_id>', methods=['POST'])
@can_manage_content
def delete_custom_content(content_id):
    try:
        db.session.delete(CustomLesson.query.get_or_404(content_id))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception: return jsonify({"status": "error"}), 500

# ==========================================
# الأسئلة والتحديثات (للمسؤول فقط)
# ==========================================
@app.route('/ask-question', methods=['POST'])
def submit_question():
    try:
        data = request.form if request.form else request.json
        subject_id, chapter_name = data.get('subject_id'), data.get('chapter_name')
        student_name = (data.get('student_name') or 'طالب').strip() or 'طالب'
        question_text = (data.get('question_text') or '').strip()

        if not question_text or not subject_id or not chapter_name: return jsonify({"status": "error", "message": "بيانات غير مكتملة"}), 400
        db.session.add(LessonQuestion(subject_id=subject_id, chapter_name=chapter_name, student_name=student_name, question_text=question_text))
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إرسال السؤال بنجاح"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/questions', methods=['GET'])
@admin_required
def get_pending_questions(): return jsonify([q.to_dict() for q in LessonQuestion.query.filter_by(is_answered=False).order_by(LessonQuestion.created_at.desc()).all()])

@app.route('/admin/reply/<int:question_id>', methods=['POST'])
@admin_required
def reply_question(question_id):
    try:
        q = LessonQuestion.query.get_or_404(question_id)
        ans = (request.json or request.form).get('answer_text', '').strip()
        if not ans: return jsonify({"status": "error", "message": "الرد مطلوب"}), 400
        q.answer_text = ans; q.is_answered = True
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر الرد بنجاح"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-question/<int:question_id>', methods=['POST'])
@admin_required
def delete_question(question_id):
    try:
        db.session.delete(LessonQuestion.query.get_or_404(question_id))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception: return jsonify({"status": "error"}), 500

@app.route('/api/updates', methods=['GET'])
def get_updates(): return jsonify([u.to_dict() for u in PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()])

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_update():
    try:
        data = request.json or request.form
        title, tag, description = data.get('title', '').strip(), data.get('tag', 'ميزة جديدة').strip(), data.get('description', '').strip()
        if not title or not description: return jsonify({"status": "error", "message": "العنوان والتفاصيل مطلوبة"}), 400
        db.session.add(PlatformUpdate(title=title, tag=tag, description=description))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-update/<int:update_id>', methods=['POST'])
@admin_required
def delete_update(update_id):
    try:
        db.session.delete(PlatformUpdate.query.get_or_404(update_id))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception: return jsonify({"status": "error"}), 500

# ==========================================
# خدمات الذكاء الاصطناعي والمخطط
# ==========================================
@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        user_message = (request.json.get('message', '') if request.is_json else request.form.get('message', '')).strip()
        parts = []
        if not request.is_json and 'file' in request.files and request.files['file'].filename != '':
            file = request.files['file']
            parts.append({"inlineData": {"mimeType": file.mimetype or 'image/jpeg', "data": base64.b64encode(file.read()).decode('utf-8')}})
        prompt = "أنت مساعد الأكاديمية الأكاديمي. اشرح للطالب بأسلوب هندسي وفيزيائي مبسط وواضح.\n" + (f"سؤال: {user_message}" if user_message else "اشرح الصورة.")
        parts.insert(0, {"text": prompt})
        reply, error = send_gemini_request(API_URL, {"contents": [{"parts": parts}]})
        return jsonify({"reply": reply}) if reply else (jsonify({"reply": error}), 500)
    except Exception as e: return jsonify({"reply": str(e)}), 500

@app.route('/planner')
def planner(): return render_template('planner.html')

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    data = request.json or {}
    prompt = f"ولد خطة من {data.get('current_date')} لـ {data.get('target_date')}. المواد: {data.get('subjects')}. أيام الراحة: {', '.join(data.get('off_days', []))}. الورد: {'نعم' if data.get('wird') else 'لا'}. أرجع JSON فقط."
    reply, err = send_gemini_request(PLANNER_API_URL, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"response_mime_type": "application/json"}})
    return jsonify(json.loads(reply.replace('```json','').replace('```','').strip())) if reply else (jsonify({"error": err}), 500)

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    data = request.json or {}
    prompt = f"أعد دمج المهام التي لم تنجز: {json.dumps(data.get('missed_tasks',[]))} ضمن الأيام: {json.dumps(data.get('remaining_plans',[]))} أرجع JSON."
    reply, err = send_gemini_request(PLANNER_API_URL, {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"response_mime_type": "application/json"}})
    return jsonify(json.loads(reply.replace('```json','').replace('```','').strip())) if reply else (jsonify({"error": err}), 500)

if __name__ == '__main__':
    app.run(debug=True)
