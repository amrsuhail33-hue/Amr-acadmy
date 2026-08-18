import os
import csv
import io
import time
import json
import base64
import requests
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify,
    send_from_directory, redirect, url_for, session
)
from flask_sqlalchemy import SQLAlchemy
from groq import Groq

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'amr_suhail_academy_secure_secret_key_2026')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# ==========================================
# إعداد قاعدة البيانات ومعالجة الرابط لـ Supabase عبر pg8000
# ==========================================
raw_db_url = os.environ.get('DATABASE_URL', 'sqlite:///academy_qa.db').strip()

if raw_db_url.startswith("postgres://"):
    db_url = raw_db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif raw_db_url.startswith("postgresql://"):
    db_url = raw_db_url.replace("postgresql://", "postgresql+pg8000://", 1)
else:
    db_url = raw_db_url

db_url = db_url.replace("postgresql+psycopg2://", "postgresql+pg8000://")

if "?sslmode=" in db_url:
    db_url = db_url.split("?sslmode=")[0]
elif "&sslmode=" in db_url:
    db_url = db_url.split("&sslmode=")[0]

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if "sqlite" not in db_url:
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123")
ASSISTANT_PASSWORD = os.environ.get("ASSISTANT_PASSWORD", "AhmedAI")

db = SQLAlchemy(app)

# ==========================================
# دالة حساب التقدير الأكاديمي للكلية
# ==========================================
def calculate_grade_estimation(score):
    try:
        score = float(score)
        if 90 <= score <= 100:
            return "ممتاز"
        elif 80 <= score < 90:
            return "جيد جداً"
        elif 65 <= score < 80:
            return "جيد"
        elif 50 <= score < 65:
            return "مقبول"
        elif 0 <= score < 50:
            return "راسب"
        else:
            return "درجة غير صالحة"
    except (ValueError, TypeError):
        return "خطأ في القيمة"

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
        date_str = self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        return {
            "id": self.id,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "student_name": self.student_name,
            "question_text": self.question_text,
            "answer_text": self.answer_text,
            "is_answered": self.is_answered,
            "created_at": date_str
        }

class PlatformUpdate(db.Model):
    __tablename__ = 'platform_updates'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    tag = db.Column(db.String(50), default="ميزة جديدة")
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        date_str = self.created_at.strftime("%Y-%m-%d") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d")
        return {
            "id": self.id,
            "title": self.title,
            "tag": self.tag,
            "description": self.description,
            "date": date_str
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
        date_str = self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d %H:%M")
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
            "created_at": date_str
        }

with app.app_context():
    try:
        db.create_all()
        if PlatformUpdate.query.count() == 0:
            db.session.add(PlatformUpdate(
                title="إطلاق نظام الأسئلة والشريك الرسمي",
                tag="تحديث رئيسي 🚀",
                description="أصبح بإمكانك الآن طرح استفساراتك مباشرة في صفحة كل درس والحصول على إجابة المعلم المعتمدة."
            ))
            db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"Startup DB Status: {e}")

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
        if not (session.get('is_admin') or session.get('is_assistant')):
            return jsonify({"status": "error", "message": "غير مصرح لك برفع أو تعديل المحتوى"}), 403
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# AI APIs & Sheets with In-Memory Fallback Cache
# ==========================================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "llama-3.3-70b-versatile"

CSV_URLS = [
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=0",
    "https://docs.google.com/spreadsheets/d/14nAqGlyk4RZkKW7P_Vk_aEKmyZsbcWWP_BAS9WYWT80/export?format=csv&gid=1031832429"
]

CACHED_DATA = {}
LAST_FETCH_TIME = 0

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

def get_data_from_sheet():
    global CACHED_DATA, LAST_FETCH_TIME
    
    # تحديث الكاش كل دقيقتين كحد أدنى لتجنب ضغط الطلبات وبطء السيرفر
    now = time.time()
    if CACHED_DATA and (now - LAST_FETCH_TIME < 120):
        return CACHED_DATA

    data = {}
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        res1 = requests.get(f"{CSV_URLS[0]}&t={int(now)}", headers=headers, timeout=25)
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

        res2 = requests.get(f"{CSV_URLS[1]}&t={int(now)}", headers=headers, timeout=25)
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

        try:
            custom_items = CustomLesson.query.all()
            for item in custom_items:
                c_year, c_term, c_subject, c_chapter, c_type = item.year.strip(), item.term.strip(), item.subject_name.strip(), item.chapter_name.strip() or "عام", item.content_type.strip()
                if c_year not in data: data[c_year] = {"name": c_year, "terms": {}}
                if c_term not in data[c_year]["terms"]: data[c_year]["terms"][term] = {"name": c_term, "subjects": {}}
                if c_subject not in data[c_year]["terms"][term]["subjects"]:
                    data[c_year]["terms"][term]["subjects"][c_subject] = {"id": f"{c_year}_{c_term}_{c_subject}".replace(" ", "_"),"name": c_subject,"types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}}
                subj_data = data[c_year]["terms"][term]["subjects"][c_subject]
                if c_type == "videos":
                    if c_chapter not in subj_data["types"]["videos"]["chapters"]: subj_data["types"]["videos"]["chapters"][c_chapter] = {"name": c_chapter, "lessons": []}
                    subj_data["types"]["videos"]["chapters"][c_chapter]["lessons"].append({"title": item.title, "video": item.url, "download": item.download_url, "notes": item.notes, "custom_id": item.id})
                elif c_type == "files":
                    if c_chapter not in subj_data["types"]["files"]["chapters"]: subj_data["types"]["files"]["chapters"][chapter] = {"name": c_chapter, "lessons": []}
                    subj_data["types"]["files"]["chapters"][c_chapter]["lessons"].append({"title": item.title, "file": item.url, "download": item.download_url, "notes": item.notes, "custom_id": item.id})
        except Exception as db_err:
            db.session.rollback()
            print(f"Custom lessons load fallback: {db_err}")

        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
                    s["types"]["quiz"]["chapters"] = list(s["types"]["quiz"]["chapters"].values())

        if data:
            CACHED_DATA = data
            LAST_FETCH_TIME = now
            return data

    except Exception as e: 
        print(f"Error fetching data: {e}")

    # في حال حدوث timeout أو مشكلة اتصال، نرجع آخر بيانات ناجحة
    return CACHED_DATA if CACHED_DATA else data

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

        try:
            updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
        except Exception:
            db.session.rollback()
            updates = []

        if subject_id and content_type and chapter_name:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            for c in s["types"][content_type]["chapters"]:
                                if c["name"] == chapter_name:
                                    try:
                                        if is_admin: 
                                            questions = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name).order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
                                        else: 
                                            questions = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name, is_answered=True).order_by(LessonQuestion.created_at.desc()).all()
                                    except Exception:
                                        db.session.rollback()
                                        questions = []
                                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, updates=updates, is_admin=is_admin, is_assistant=is_assistant)
        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], updates=updates, is_admin=is_admin, is_assistant=is_assistant)
        if subject_id:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id: 
                            return render_template('index.html', view='select_type', subject=s, updates=updates, is_admin=is_admin, is_assistant=is_assistant)
        if year and term:
            if year in DATA and term in DATA[year]["terms"]: 
                return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year, updates=updates, is_admin=is_admin, is_assistant=is_assistant)
        if year:
            if year in DATA: 
                return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year, updates=updates, is_admin=is_admin, is_assistant=is_assistant)

        return render_template('index.html', view='years', data=DATA, updates=updates, is_admin=is_admin, is_assistant=is_assistant)
    except Exception as e: 
        return f"Error: {str(e)}"

# ==========================================
# مصادقة الإدارة والمساعد
# ==========================================
@app.route('/admin/login', methods=['POST', 'GET'])
def admin_login():
    if request.method == 'GET':
        return render_template('index.html', view='login')
        
    data = request.get_json(silent=True) or request.form or {}
    pwd = (data.get('password') or '').strip()
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
    return jsonify({"status": "success"})

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
        data = request.get_json(silent=True) or request.form or {}
        year = (data.get('year') or '').strip()
        term = (data.get('term') or '').strip()
        subject_name = (data.get('subject_name') or '').strip()
        chapter_name = (data.get('chapter_name') or 'عام').strip() or 'عام'
        content_type = (data.get('content_type') or 'videos').strip()
        title = (data.get('title') or '').strip()
        url = (data.get('url') or '').strip()
        download_url = (data.get('download_url') or '').strip()
        notes = (data.get('notes') or '').strip()

        if not year or not term or not subject_name or not title: 
            return jsonify({"status": "error", "message": "البيانات الأساسية مطلوبة"}), 400
        if not url and not download_url: 
            return jsonify({"status": "error", "message": "يجب إرفاق رابط مشاهدة/ملف أو رابط تحميل"}), 400

        db.session.add(CustomLesson(
            year=year, term=term, subject_name=subject_name,
            chapter_name=chapter_name, content_type=content_type,
            title=title, url=url, download_url=download_url, notes=notes
        ))
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت الإضافة بنجاح وتحديث الموقع فورياً!"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/edit-content/<int:content_id>', methods=['POST'])
@can_manage_content
def edit_custom_content(content_id):
    try:
        item = CustomLesson.query.get(content_id)
        if not item:
            return jsonify({"status": "error", "message": "الدرس أو الملف غير موجود بقاعدة البيانات"}), 404
            
        data = request.get_json(silent=True) or request.form or {}
        item.year = (data.get('year') or '').strip()
        item.term = (data.get('term') or '').strip()
        item.subject_name = (data.get('subject_name') or '').strip()
        item.chapter_name = (data.get('chapter_name') or 'عام').strip() or 'عام'
        item.content_type = (data.get('content_type') or 'videos').strip()
        item.title = (data.get('title') or '').strip()
        item.url = (data.get('url') or '').strip()
        item.download_url = (data.get('download_url') or '').strip()
        item.notes = (data.get('notes') or '').strip()
        
        if not item.year or not item.term or not item.subject_name or not item.title:
            return jsonify({"status": "error", "message": "البيانات الأساسية مطلوبة"}), 400
            
        db.session.commit()
        return jsonify({"status": "success", "message": "تم التعديل بنجاح!"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/custom-content', methods=['GET'])
@can_manage_content
def get_custom_content(): 
    try:
        return jsonify([l.to_dict() for l in CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()])
    except Exception as e:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/delete-content/<int:content_id>', methods=['POST'])
@can_manage_content
def delete_custom_content(content_id):
    try:
        db.session.delete(CustomLesson.query.get_or_404(content_id))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# الأسئلة والتحديثات (للمسؤول فقط)
# ==========================================
@app.route('/ask-question', methods=['POST'])
def submit_question():
    try:
        data = request.get_json(silent=True) or request.form or {}
        subject_id = data.get('subject_id')
        chapter_name = data.get('chapter_name')
        student_name = (data.get('student_name') or 'طالب').strip() or 'طالب'
        question_text = (data.get('question_text') or '').strip()

        if not question_text or not subject_id or not chapter_name: 
            return jsonify({"status": "error", "message": "بيانات غير مكتملة"}), 400
            
        db.session.add(LessonQuestion(
            subject_id=subject_id, chapter_name=chapter_name,
            student_name=student_name, question_text=question_text
        ))
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إرسال السؤال بنجاح"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/questions', methods=['GET'])
@admin_required
def get_pending_questions(): 
    try:
        return jsonify([q.to_dict() for q in LessonQuestion.query.filter_by(is_answered=False).order_by(LessonQuestion.created_at.desc()).all()])
    except Exception as e:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/reply/<int:question_id>', methods=['POST'])
@admin_required
def reply_question(question_id):
    try:
        q = LessonQuestion.query.get_or_404(question_id)
        data = request.get_json(silent=True) or request.form or {}
        ans = (data.get('answer_text') or '').strip()
        if not ans: 
            return jsonify({"status": "error", "message": "الرد مطلوب"}), 400
        q.answer_text = ans
        q.is_answered = True
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر الرد بنجاح"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-question/<int:question_id>', methods=['POST'])
@admin_required
def delete_question(question_id):
    try:
        db.session.delete(LessonQuestion.query.get_or_404(question_id))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/updates', methods=['GET'])
def get_updates(): 
    try:
        updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
        return jsonify([u.to_dict() for u in updates])
    except Exception as e:
        db.session.rollback()
        print(f"Error fetching updates: {e}")
        try:
            db.create_all()
        except Exception:
            pass
        return jsonify([])

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_update():
    try:
        data = request.get_json(silent=True) or request.form or {}
        title = (data.get('title') or '').strip()
        tag = (data.get('tag') or 'ميزة جديدة').strip()
        description = (data.get('description') or '').strip()
        if not title or not description: 
            return jsonify({"status": "error", "message": "العنوان والتفاصيل مطلوبة"}), 400
        db.session.add(PlatformUpdate(title=title, tag=tag, description=description))
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر التحديث بنجاح"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-update/<int:update_id>', methods=['POST'])
@admin_required
def delete_update(update_id):
    try:
        db.session.delete(PlatformUpdate.query.get_or_404(update_id))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e: 
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# حساب التقديرات الأكاديمية
# ==========================================
@app.route('/calculate-grade', methods=['POST'])
def calculate_grade():
    try:
        data = request.get_json(silent=True) or request.form or {}
        score = data.get('score')
        if score is None:
            return jsonify({"status": "error", "message": "يرجى إدخال الدرجة"}), 400
        
        grade = calculate_grade_estimation(score)
        return jsonify({"status": "success", "score": score, "grade": grade})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# خدمات الذكاء الاصطناعي والمخطط عبر Groq
# ==========================================
@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        data = request.get_json(silent=True) or request.form or {}
        user_message = (data.get('message') or '').strip()
        
        messages = [
            {
                "role": "system", 
                "content": "أنت المساعد الأكاديمي الذكي للأكاديمية. وظيفتك الإجابة بوضوح وبأسلوب علمي وهندسي مبسط، وشرح خطوات الحل للطلاب خطوة بخطوة باللغة العربية."
            },
            {
                "role": "user", 
                "content": user_message if user_message else "مرحبا"
            }
        ]

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.6,
        )
        
        reply = response.choices[0].message.content
        return jsonify({"reply": reply})

    except Exception as e:
        return jsonify({"reply": f"عذراً، حدث خطأ أثناء المعالجة: {str(e)}"}), 500

@app.route('/planner')
def planner(): 
    return render_template('planner.html')

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    try:
        data = request.get_json(silent=True) or {}
        discipline = (data.get('discipline') or 'متوازن').strip()

        discipline_profiles = {
            "مرن": (
                "PACE: FLEXIBLE & LIGHT\n"
                "- Daily study workload: 1.5 to 2.5 hours total.\n"
                "- Tasks count: 1 to 2 light tasks per active day.\n"
                "- Task style: Conceptual reading, high-level summaries, and gentle reviews without academic stress."
            ),
            "متوازن": (
                "PACE: BALANCED & STANDARD\n"
                "- Daily study workload: 3.5 to 5 hours total.\n"
                "- Tasks count: 2 to 3 structured tasks per active day.\n"
                "- Task style: Standard lecture study, chapter breakdowns, and moderate practice exercise solving."
            ),
            "مكثف": (
                "PACE: INTENSIVE EXAM BOOTCAMP\n"
                "- Daily study workload: 6 to 8.5+ hours total (maximize study time around sleep & busy hours).\n"
                "- Tasks count: 3 to 5 deep-focus tasks per active day.\n"
                "- Task style: Comprehensive chapter mastery, intense problem-solving sessions, past exam papers, and rigorous timed drills."
            )
        }

        selected_profile = discipline_profiles.get(discipline, discipline_profiles["متوازن"])

        system_instruction = (
            "You are an expert academic planner. Generate a highly realistic, customized daily study schedule in pure JSON format.\n\n"
            f"SELECTED DISCIPLINE MODE & CONSTRAINTS:\n{selected_profile}\n\n"
            "STRICT RULES:\n"
            "1. Output ONLY a valid JSON object matching this exact schema:\n"
            "{\n"
            '  "plans": [\n'
            '    {\n'
            '      "day_name": "السبت",\n'
            '      "date": "YYYY-MM-DD",\n'
            '      "is_off_day": false,\n'
            '      "tasks": [\n'
            '        {"title": "اسم المادة أو المهمة والموضوع بتفصيل واضح ومحفز", "allocated_hours": 2}\n'
            '      ]\n'
            '    }\n'
            '  ]\n'
            "}\n"
            "2. Make sure the total daily hours and task depths strictly reflect the chosen discipline level.\n"
            "3. Distribute the requested subjects logically across all active days from Start Date to Target Date.\n"
            "4. For requested Weekly Off-Days, set 'is_off_day': true and 'tasks': [].\n"
            "5. If Quran reading (الورد) is enabled, include it as an extra task (~0.5 hour).\n"
            "6. If Quran memorization is enabled, allocate proper time for the requested page count.\n"
            "7. All task titles, subjects, and day names must be written in clear, professional Arabic."
        )

        user_prompt = (
            f"Start Date: {data.get('current_date')}\n"
            f"Target Date: {data.get('target_date')}\n"
            f"Subjects/Courses: {data.get('subjects')}\n"
            f"Requested Discipline Mode: {discipline}\n"
            f"Sleep Hours: {data.get('sleep_hours')}\n"
            f"Busy/Work Hours: {data.get('busy_hours')}\n"
            f"Weekly Off-Days: {', '.join(data.get('off_days', []))}\n"
            f"Include Daily Wird: {'Yes' if data.get('wird') else 'No'}\n"
            f"Quran Memorization: {'Yes, ' + str(data.get('quran_pages', 1)) + ' pages' if data.get('quran_enabled') else 'No'}"
        )

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.65
        )

        reply_text = response.choices[0].message.content
        return jsonify(json.loads(reply_text))

    except Exception as e:
        return jsonify({"error": f"Error generating plan: {str(e)}"}), 500

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    try:
        data = request.get_json(silent=True) or {}
        missed = data.get('missed_tasks', [])
        remaining = data.get('remaining_plans', [])

        system_instruction = (
            "You are an academic rescheduling assistant. Re-distribute uncompleted tasks smartly into the remaining study days.\n"
            "STRICT RULES:\n"
            "1. Output ONLY a valid JSON object with the key 'plans' containing the updated days array.\n"
            "2. Preserve the exact structure: [{'day_name': '...', 'date': '...', 'is_off_day': false, 'tasks': [{'title': '...', 'allocated_hours': 1.5}]}]\n"
            "3. Do not drop any tasks; merge missed ones logically into active study days without overloading off-days."
        )

        user_prompt = (
            f"Missed Tasks: {json.dumps(missed, ensure_ascii=False)}\n"
            f"Remaining Schedule: {json.dumps(remaining, ensure_ascii=False)}"
        )

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.4
        )

        reply_text = response.choices[0].message.content
        return jsonify(json.loads(reply_text))

    except Exception as e:
        return jsonify({"error": f"Error rescheduling plan: {str(e)}"}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
