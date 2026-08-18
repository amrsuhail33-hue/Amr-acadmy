import os
import io
import time
import json
import ssl
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
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'amr_suhail_academy_secure_secret_key_2026')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# ==========================================
# إعداد قاعدة البيانات ومعالجة الرابط لـ Supabase عبر pg8000 مع SSL
# ==========================================
raw_db_url = os.environ.get('DATABASE_URL', 'sqlite:///academy_qa.db').strip()

if raw_db_url.startswith("postgres://"):
    db_url = raw_db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif raw_db_url.startswith("postgresql://"):
    db_url = raw_db_url.replace("postgresql+pg8000://", 1)
else:
    db_url = raw_db_url

db_url = db_url.replace("postgresql+psycopg2://", "postgresql+pg8000://")

if "?" in db_url:
    db_url = db_url.split("?")[0]

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if "sqlite" not in db_url:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {
            "ssl_context": ssl_context
        }
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

# --- نموذج الطالب ---
class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

# --- نموذج المفضلة ---
class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref='favorites')
    lesson = db.relationship('CustomLesson', backref='favorited_by')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "lesson": self.lesson.to_dict() if self.lesson else None,
            "added_at": self.added_at.strftime("%Y-%m-%d %H:%M") if self.added_at else ""
        }

# --- نموذج الخطة اليومية (Schedule) - سيتم استبداله تدريجياً بـ UnifiedTask ---
class Schedule(db.Model):
    __tablename__ = 'schedules'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    planned_date = db.Column(db.Date, nullable=False)
    completed = db.Column(db.Boolean, default=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref='schedules')
    lesson = db.relationship('CustomLesson', backref='scheduled_by')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "lesson_title": self.lesson.title if self.lesson else "",
            "planned_date": self.planned_date.strftime("%Y-%m-%d") if self.planned_date else "",
            "completed": self.completed,
            "notes": self.notes or "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

# --- نموذج تقدم الدروس ---
class Progress(db.Model):
    __tablename__ = 'progress'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    status = db.Column(db.String(20), default='not_started')
    progress_percent = db.Column(db.Integer, default=0)
    last_accessed = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship('Student', backref='progress_records')
    lesson = db.relationship('CustomLesson', backref='progress_records')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "lesson_title": self.lesson.title if self.lesson else "",
            "status": self.status,
            "progress_percent": self.progress_percent,
            "last_accessed": self.last_accessed.strftime("%Y-%m-%d %H:%M") if self.last_accessed else ""
        }

# --- نموذج خطة الاختبارات (Exam Plan) - سيتم استبداله تدريجياً بـ UnifiedTask ---
class ExamPlan(db.Model):
    __tablename__ = 'exam_plans'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    plan_name = db.Column(db.String(200), nullable=False)
    exam_date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    student = db.relationship('Student', backref='exam_plans')
    items = db.relationship('ExamPlanItem', backref='exam_plan', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            "id": self.id,
            "plan_name": self.plan_name,
            "exam_date": self.exam_date.strftime("%Y-%m-%d") if self.exam_date else "",
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "items": [item.to_dict() for item in self.items]
        }

class ExamPlanItem(db.Model):
    __tablename__ = 'exam_plan_items'
    id = db.Column(db.Integer, primary_key=True)
    exam_plan_id = db.Column(db.Integer, db.ForeignKey('exam_plans.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    scheduled_date = db.Column(db.Date, nullable=False)
    completed = db.Column(db.Boolean, default=False)
    priority = db.Column(db.Integer, default=2)  # 1 عالي, 2 متوسط, 3 منخفض
    
    lesson = db.relationship('CustomLesson')

    def to_dict(self):
        return {
            "id": self.id,
            "lesson_id": self.lesson_id,
            "lesson_title": self.lesson.title if self.lesson else "",
            "scheduled_date": self.scheduled_date.strftime("%Y-%m-%d") if self.scheduled_date else "",
            "completed": self.completed,
            "priority": self.priority
        }

# ==========================================
# 🆕🆕🆕 نموذج المهام الموحدة (Unified Task) - الجديد
# ==========================================
class UnifiedTask(db.Model):
    __tablename__ = 'unified_tasks'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=True)  # يمكن أن يكون NULL للمهام المخصصة
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    planned_date = db.Column(db.Date, nullable=False)
    completed = db.Column(db.Boolean, default=False)
    priority = db.Column(db.Integer, default=2)  # 1: عالي, 2: متوسط, 3: منخفض
    source = db.Column(db.String(20), default='manual')  # 'manual', 'planner', 'exam'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    student = db.relationship('Student', backref='unified_tasks')
    lesson = db.relationship('CustomLesson')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "title": self.title,
            "description": self.description or "",
            "planned_date": self.planned_date.strftime("%Y-%m-%d") if self.planned_date else "",
            "completed": self.completed,
            "priority": self.priority,
            "source": self.source,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else ""
        }

# --- النماذج القديمة ---
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

class QuizQuestion(db.Model):
    __tablename__ = 'quiz_questions'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.Text, nullable=True)
    option_b = db.Column(db.Text, nullable=True)
    option_c = db.Column(db.Text, nullable=True)
    option_d = db.Column(db.Text, nullable=True)
    correct_answer = db.Column(db.String(50), nullable=True)
    explanation = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "year": self.year,
            "term": self.term,
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "question_text": self.question_text,
            "option_a": self.option_a or "",
            "option_b": self.option_b or "",
            "option_c": self.option_c or "",
            "option_d": self.option_d or "",
            "correct_answer": self.correct_answer or "",
            "explanation": self.explanation or ""
        }

# ==========================================
# إنشاء الجداول وبيانات أولية
# ==========================================
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

def student_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('student_id'):
            return jsonify({"status": "error", "message": "يرجى تسجيل الدخول أولاً"}), 401
        return f(*args, **kwargs)
    return decorated_function

# ==========================================
# إعداد نموذج الذكاء الاصطناعي من Groq
# ==========================================
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "openai/gpt-oss-120b"

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

def get_platform_data():
    data = {}
    try:
        lessons = CustomLesson.query.order_by(CustomLesson.id.asc()).all()
        for item in lessons:
            year = (item.year or '').strip()
            term = (item.term or '').strip()
            subject = (item.subject_name or '').strip()
            chapter = (item.chapter_name or 'عام').strip() or 'عام'
            c_type = (item.content_type or 'videos').strip()

            if not year or not term or not subject:
                continue

            if year not in data:
                data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]:
                data[year]["terms"][term] = {"name": term, "subjects": {}}
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
            
            if c_type not in subj_data["types"]:
                subj_data["types"][c_type] = {"chapters": {}}

            if chapter not in subj_data["types"][c_type]["chapters"]:
                subj_data["types"][c_type]["chapters"][chapter] = {"name": chapter, "lessons": []}

            lesson_dict = {
                "id": item.id,
                "title": item.title,
                "notes": item.notes or "",
                "custom_id": item.id
            }
            if c_type == "videos":
                lesson_dict["video"] = item.url or ""
                lesson_dict["download"] = item.download_url or ""
            elif c_type == "files":
                lesson_dict["file"] = item.url or item.download_url or ""
                lesson_dict["download"] = item.download_url or ""
            elif c_type == "quiz":
                lesson_dict["title"] = item.title or "بدء الاختبار"

            subj_data["types"][c_type]["chapters"][chapter]["lessons"].append(lesson_dict)

        for y in data.values():
            for t in y["terms"].values():
                t["subjects"] = list(t["subjects"].values())
                for s in t["subjects"]:
                    s["types"]["videos"]["chapters"] = list(s["types"]["videos"]["chapters"].values())
                    s["types"]["files"]["chapters"] = list(s["types"]["files"]["chapters"].values())
                    s["types"]["quiz"]["chapters"] = list(s["types"]["quiz"]["chapters"].values())
    except Exception as e:
        db.session.rollback()
        print(f"Error building platform data from DB: {e}")

    return data

# ==========================================
# Routes الرئيسية
# ==========================================
@app.route('/')
def home():
    try:
        DATA = get_platform_data()
        year = request.args.get('year')
        term = request.args.get('term')
        subject_id = request.args.get('subject')
        content_type = request.args.get('type')
        chapter_name = request.args.get('chapter')
        is_admin = session.get('is_admin', False)
        is_assistant = session.get('is_assistant', False)
        student_id = session.get('student_id')
        student_name = session.get('student_name')

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
                                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, updates=updates, is_admin=is_admin, is_assistant=is_assistant, student_id=student_id, student_name=student_name)
        if subject_id and content_type:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id and content_type in s["types"]:
                            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], updates=updates, is_admin=is_admin, is_assistant=is_assistant, student_id=student_id, student_name=student_name)
        if subject_id:
            for y in DATA.values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == subject_id: 
                            return render_template('index.html', view='select_type', subject=s, updates=updates, is_admin=is_admin, is_assistant=is_assistant, student_id=student_id, student_name=student_name)
        if year and term:
            if year in DATA and term in DATA[year]["terms"]: 
                return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year, updates=updates, is_admin=is_admin, is_assistant=is_assistant, student_id=student_id, student_name=student_name)
        if year:
            if year in DATA: 
                return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year, updates=updates, is_admin=is_admin, is_assistant=is_assistant, student_id=student_id, student_name=student_name)

        return render_template('index.html', view='years', data=DATA, updates=updates, is_admin=is_admin, is_assistant=is_assistant, student_id=student_id, student_name=student_name)
    except Exception as e: 
        return f"Error: {str(e)}"

# ==========================================
# استرجاع بنك الأسئلة الخاص بالاختبارات
# ==========================================
@app.route('/api/quiz', methods=['GET'])
def get_quiz():
    subject_name = request.args.get('subject')
    chapter_name = request.args.get('chapter')
    try:
        query = QuizQuestion.query
        if subject_name:
            query = query.filter_by(subject_name=subject_name)
        if chapter_name:
            query = query.filter_by(chapter_name=chapter_name)
        questions = query.all()
        return jsonify([q.to_dict() for q in questions])
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

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
# إدارة ورفع وتعديل المحتوى
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
        if not url and not download_url and content_type != 'quiz': 
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
# الأسئلة والتحديثات
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
        student_id = session.get('student_id')
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
        plan_data = json.loads(reply_text)

        # ==========================================
        # 🆕 حفظ المهام في UnifiedTask تلقائياً
        # ==========================================
        if student_id and plan_data.get('plans'):
            try:
                for day in plan_data['plans']:
                    day_date = day.get('date')
                    if not day_date:
                        continue
                    try:
                        planned_date = datetime.strptime(day_date, '%Y-%m-%d').date()
                    except ValueError:
                        continue
                    for task in day.get('tasks', []):
                        task_title = task.get('title', '').strip()
                        if not task_title:
                            continue
                        # البحث عن درس مطابق في قاعدة البيانات (اختياري)
                        lesson = CustomLesson.query.filter(
                            CustomLesson.title.ilike(f'%{task_title}%')
                        ).first()
                        
                        unified_task = UnifiedTask(
                            student_id=student_id,
                            lesson_id=lesson.id if lesson else None,
                            title=task_title,
                            planned_date=planned_date,
                            source='planner',
                            priority=2,
                            completed=False
                        )
                        db.session.add(unified_task)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Error saving planner tasks: {e}")

        return jsonify({"status": "success", "plan": plan_data})

    except Exception as e:
        return jsonify({"error": f"Error generating plan: {str(e)}"}), 500

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
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
        plan_data = json.loads(reply_text)

        # ==========================================
        # 🆕 تحديث المهام المعاد توزيعها في UnifiedTask
        # ==========================================
        if student_id and plan_data.get('plans'):
            try:
                # حذف المهام القديمة غير المكتملة (اختياري)
                # بدلاً من الحذف، نقوم بتحديث تواريخها
                for day in plan_data['plans']:
                    day_date = day.get('date')
                    if not day_date:
                        continue
                    try:
                        planned_date = datetime.strptime(day_date, '%Y-%m-%d').date()
                    except ValueError:
                        continue
                    for task in day.get('tasks', []):
                        task_title = task.get('title', '').strip()
                        if not task_title:
                            continue
                        # البحث عن المهمة القديمة وتحديث تاريخها
                        existing_task = UnifiedTask.query.filter(
                            UnifiedTask.student_id == student_id,
                            UnifiedTask.title == task_title,
                            UnifiedTask.completed == False
                        ).first()
                        if existing_task:
                            existing_task.planned_date = planned_date
                        else:
                            # إنشاء مهمة جديدة إذا لم تكن موجودة
                            unified_task = UnifiedTask(
                                student_id=student_id,
                                title=task_title,
                                planned_date=planned_date,
                                source='planner',
                                priority=2,
                                completed=False
                            )
                            db.session.add(unified_task)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
                print(f"Error rescheduling tasks: {e}")

        return jsonify({"status": "success", "plan": plan_data})

    except Exception as e:
        return jsonify({"error": f"Error rescheduling plan: {str(e)}"}), 500

# ==========================================
# 🆕🆕🆕 واجهات برمجية للمهام الموحدة (Unified Tasks)
# ==========================================

@app.route('/api/tasks', methods=['GET'])
@student_required
def get_unified_tasks():
    """جلب جميع مهام الطالب الموحدة"""
    try:
        student_id = session['student_id']
        source = request.args.get('source')
        query = UnifiedTask.query.filter_by(student_id=student_id)
        if source:
            query = query.filter_by(source=source)
        tasks = query.order_by(UnifiedTask.planned_date).all()
        return jsonify([t.to_dict() for t in tasks])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks', methods=['POST'])
@student_required
def add_unified_task():
    """إضافة مهمة جديدة (يدوية أو من AI)"""
    try:
        data = request.get_json(silent=True) or {}
        student_id = session['student_id']
        
        title = (data.get('title') or '').strip()
        planned_date = data.get('planned_date')
        source = data.get('source', 'manual')
        priority = data.get('priority', 2)
        lesson_id = data.get('lesson_id')
        description = data.get('description', '')

        if not title or not planned_date:
            return jsonify({"status": "error", "message": "العنوان والتاريخ مطلوبان"}), 400

        try:
            planned_date_obj = datetime.strptime(planned_date, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400

        # التحقق من وجود الدرس إذا تم إرسال lesson_id
        if lesson_id:
            lesson = CustomLesson.query.get(lesson_id)
            if not lesson:
                return jsonify({"status": "error", "message": "الدرس غير موجود"}), 404

        task = UnifiedTask(
            student_id=student_id,
            lesson_id=lesson_id,
            title=title,
            description=description,
            planned_date=planned_date_obj,
            source=source,
            priority=priority,
            completed=False
        )
        db.session.add(task)
        db.session.commit()
        
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks/<int:task_id>', methods=['PUT'])
@student_required
def update_unified_task(task_id):
    """تحديث مهمة (تحديد مكتمل، تعديل التاريخ، إلخ)"""
    try:
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != session['student_id']:
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل هذه المهمة"}), 403

        data = request.get_json(silent=True) or {}
        
        if 'completed' in data:
            task.completed = bool(data['completed'])
            # إذا تم إكمال المهمة، نضيف احتفالاً (سيتم التعامل معه في الواجهة)
        if 'planned_date' in data:
            try:
                task.planned_date = datetime.strptime(data['planned_date'], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400
        if 'title' in data:
            task.title = data['title'].strip()
        if 'description' in data:
            task.description = data['description'].strip()
        if 'priority' in data:
            task.priority = int(data['priority'])
        
        task.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
@student_required
def delete_unified_task(task_id):
    """حذف مهمة"""
    try:
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != session['student_id']:
            return jsonify({"status": "error", "message": "غير مصرح لك بحذف هذه المهمة"}), 403
        
        db.session.delete(task)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف المهمة"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks/sync', methods=['POST'])
@student_required
def sync_unified_tasks():
    """مزامنة مجموعة من المهام (للاستخدام مع localStorage)"""
    try:
        student_id = session['student_id']
        data = request.get_json(silent=True) or {}
        tasks_data = data.get('tasks', [])
        
        for task_data in tasks_data:
            # التحقق مما إذا كانت المهمة موجودة بالفعل
            task_id = task_data.get('id')
            if task_id:
                task = UnifiedTask.query.get(task_id)
                if task and task.student_id == student_id:
                    # تحديث المهمة الموجودة
                    if 'completed' in task_data:
                        task.completed = bool(task_data['completed'])
                    if 'planned_date' in task_data:
                        try:
                            task.planned_date = datetime.strptime(task_data['planned_date'], '%Y-%m-%d').date()
                        except ValueError:
                            continue
                    task.updated_at = datetime.utcnow()
                    continue
            
            # إضافة مهمة جديدة
            title = task_data.get('title', '').strip()
            planned_date = task_data.get('planned_date')
            if not title or not planned_date:
                continue
            try:
                planned_date_obj = datetime.strptime(planned_date, '%Y-%m-%d').date()
            except ValueError:
                continue
            
            new_task = UnifiedTask(
                student_id=student_id,
                lesson_id=task_data.get('lesson_id'),
                title=title,
                description=task_data.get('description', ''),
                planned_date=planned_date_obj,
                source=task_data.get('source', 'manual'),
                priority=task_data.get('priority', 2),
                completed=task_data.get('completed', False)
            )
            db.session.add(new_task)
        
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت المزامنة بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# 🆕🆕 واجهات خطة الاختبارات (تحديث للعمل مع UnifiedTask)
# ==========================================
@app.route('/api/generate-exam-plan', methods=['POST'])
@student_required
def generate_exam_plan():
    try:
        data = request.get_json(silent=True) or {}
        student_id = session['student_id']
        plan_name = (data.get('plan_name') or '').strip()
        exam_date = data.get('exam_date')
        lesson_ids = data.get('lesson_ids', [])

        if not plan_name or not exam_date or not lesson_ids:
            return jsonify({"status": "error", "message": "اسم الاختبار والتاريخ والدروس مطلوبة"}), 400

        try:
            exam_date_obj = datetime.strptime(exam_date, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400

        lessons = CustomLesson.query.filter(CustomLesson.id.in_(lesson_ids)).all()
        if not lessons:
            return jsonify({"status": "error", "message": "لم يتم العثور على الدروس المحددة"}), 404

        lessons_info = []
        for l in lessons:
            est_minutes = 30 if l.content_type == 'videos' else 20
            if 'محاضرة' in l.title or 'شرح' in l.title:
                est_minutes = 45
            lessons_info.append({
                "id": l.id,
                "title": l.title,
                "type": l.content_type,
                "estimated_minutes": est_minutes
            })

        today = datetime.now().date()
        delta = (exam_date_obj - today).days
        if delta < 1:
            return jsonify({"status": "error", "message": "تاريخ الاختبار يجب أن يكون في المستقبل"}), 400

        prompt = f"""
        أنت مخطط أكاديمي ذكي متخصص في إعداد خطط مكثفة للاختبارات.
        
        اسم الاختبار: {plan_name}
        تاريخ الاختبار: {exam_date} (عدد الأيام المتاحة: {delta} يوماً)
        
        قائمة الدروس المطلوب مراجعتها:
        {json.dumps(lessons_info, ensure_ascii=False, indent=2)}
        
        المطلوب:
        1. توزيع الدروس على الأيام المتاحة بشكل متوازن.
        2. تحديد أولوية كل درس (عالٍ، متوسط، منخفض) بناءً على صعوبة المادة ووقتها المقدر.
        3. اقتراح وقت لكل درس بالدقائق.
        4. إذا كان عدد الأيام أكبر من عدد الدروس، وزع الدروس مع فترات مراجعة إضافية.
        5. إذا كان عدد الدروس أكبر من الأيام، ضغط الجدول مع تحديد أولويات.
        
        أخرج النتيجة بصيغة JSON بهذا الهيكل الدقيق:
        {{
            "plan": [
                {{
                    "day": "2026-08-20",
                    "tasks": [
                        {{"lesson_id": 1, "title": "مقدمة في الدايدود", "priority": "عال", "minutes": 45}}
                    ]
                }}
            ]
        }}
        تأكد من أن اليوم الأول هو أقرب يوم (اليوم أو غداً) وأن جميع الدروس مغطاة.
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "أنت مخطط أكاديمي ذكي. أخرج فقط JSON صالح."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.5
        )

        plan_data = json.loads(response.choices[0].message.content)

        # ==========================================
        # 🆕 حفظ في UnifiedTask بدلاً من ExamPlanItem
        # ==========================================
        priority_map = {"عال": 1, "متوسط": 2, "منخفض": 3}
        for day_obj in plan_data.get('plan', []):
            day_date_str = day_obj.get('day')
            if not day_date_str:
                continue
            try:
                day_date = datetime.strptime(day_date_str, '%Y-%m-%d').date()
            except ValueError:
                continue
            for task in day_obj.get('tasks', []):
                lesson_id = task.get('lesson_id')
                if not lesson_id:
                    continue
                lesson = CustomLesson.query.get(lesson_id)
                if not lesson:
                    continue
                
                unified_task = UnifiedTask(
                    student_id=student_id,
                    lesson_id=lesson_id,
                    title=task.get('title', lesson.title),
                    description=f"خطة اختبار: {plan_name} - أولوية: {task.get('priority', 'متوسط')} - {task.get('minutes', 30)} دقيقة",
                    planned_date=day_date,
                    source='exam',
                    priority=priority_map.get(task.get('priority', 'متوسط'), 2),
                    completed=False
                )
                db.session.add(unified_task)
        db.session.commit()

        return jsonify({
            "status": "success",
            "plan": plan_data
        })

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# واجهات قديمة (للتوافق مع الكود القديم)
# ==========================================

@app.route('/api/exam-plans', methods=['GET'])
@student_required
def get_exam_plans():
    """جلب خطط الاختبارات (من UnifiedTask) مع فلتر source='exam'"""
    try:
        student_id = session['student_id']
        tasks = UnifiedTask.query.filter_by(student_id=student_id, source='exam').order_by(UnifiedTask.planned_date).all()
        # تجميع المهام حسب اسم الخطة (من description)
        plans = {}
        for task in tasks:
            plan_name = "خطة اختبار"
            if task.description and 'خطة اختبار:' in task.description:
                parts = task.description.split(' - ')
                if parts:
                    plan_name = parts[0].replace('خطة اختبار:', '').strip()
            if plan_name not in plans:
                plans[plan_name] = {
                    "plan_name": plan_name,
                    "exam_date": task.planned_date.strftime("%Y-%m-%d"),
                    "items": []
                }
            plans[plan_name]["items"].append(task.to_dict())
        
        return jsonify(list(plans.values()))
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/exam-plans/<int:plan_id>', methods=['DELETE'])
@student_required
def delete_exam_plan(plan_id):
    """حذف خطة اختبار (حذف جميع المهام ذات المصدر exam والمرتبطة بنفس الخطة)"""
    try:
        student_id = session['student_id']
        # البحث عن المهام ذات المصدر exam
        tasks = UnifiedTask.query.filter_by(student_id=student_id, source='exam').all()
        # تجميع حسب الاسم
        plans = {}
        for task in tasks:
            plan_name = "خطة اختبار"
            if task.description and 'خطة اختبار:' in task.description:
                parts = task.description.split(' - ')
                if parts:
                    plan_name = parts[0].replace('خطة اختبار:', '').strip()
            if plan_name not in plans:
                plans[plan_name] = []
            plans[plan_name].append(task.id)
        
        # حذف الخطة المطلوبة
        plan_names = list(plans.keys())
        if plan_id <= len(plan_names):
            plan_name = plan_names[plan_id - 1]
            task_ids = plans.get(plan_name, [])
            for task_id in task_ids:
                task = UnifiedTask.query.get(task_id)
                if task and task.student_id == student_id:
                    db.session.delete(task)
            db.session.commit()
            return jsonify({"status": "success"})
        else:
            return jsonify({"status": "error", "message": "الخطة غير موجودة"}), 404
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/exam-plan-item/<int:item_id>', methods=['PUT'])
@student_required
def toggle_exam_item_complete(item_id):
    """تحديث حالة إكمال مهمة في خطة الاختبارات"""
    try:
        task = UnifiedTask.query.get(item_id)
        if not task:
            return jsonify({"status": "error", "message": "العنصر غير موجود"}), 404
        if task.student_id != session['student_id']:
            return jsonify({"status": "error", "message": "غير مصرح"}), 403
        data = request.get_json(silent=True) or {}
        task.completed = bool(data.get('completed', True))
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# واجهات إدارة الخطة اليومية (للتوافق مع الكود القديم)
# ==========================================
@app.route('/api/schedule', methods=['GET'])
@student_required
def get_schedule():
    """جلب المهام اليومية (من UnifiedTask مع source='manual')"""
    try:
        student_id = session['student_id']
        tasks = UnifiedTask.query.filter_by(student_id=student_id, source='manual').order_by(UnifiedTask.planned_date).all()
        return jsonify([t.to_dict() for t in tasks])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/schedule', methods=['POST'])
@student_required
def add_schedule():
    """إضافة مهمة يومية (يدوية)"""
    try:
        data = request.get_json(silent=True) or {}
        student_id = session['student_id']
        lesson_id = data.get('lesson_id')
        planned_date = data.get('planned_date')
        notes = data.get('notes', '')

        if not lesson_id or not planned_date:
            return jsonify({"status": "error", "message": "معرف الدرس والتاريخ مطلوبان"}), 400

        lesson = CustomLesson.query.get(lesson_id)
        if not lesson:
            return jsonify({"status": "error", "message": "الدرس غير موجود"}), 404

        try:
            planned_date_obj = datetime.strptime(planned_date, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400

        # التحقق من عدم وجود مهمة مكررة
        existing = UnifiedTask.query.filter_by(
            student_id=student_id,
            lesson_id=lesson_id,
            planned_date=planned_date_obj,
            source='manual'
        ).first()
        if existing:
            return jsonify({"status": "error", "message": "هذه المهمة موجودة بالفعل"}), 400

        task = UnifiedTask(
            student_id=student_id,
            lesson_id=lesson_id,
            title=lesson.title,
            description=notes,
            planned_date=planned_date_obj,
            source='manual',
            priority=2,
            completed=False
        )
        db.session.add(task)
        db.session.commit()
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/schedule/<int:schedule_id>', methods=['PUT'])
@student_required
def update_schedule(schedule_id):
    """تحديث مهمة يومية"""
    try:
        task = UnifiedTask.query.get(schedule_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != session['student_id']:
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل هذه المهمة"}), 403

        data = request.get_json(silent=True) or {}
        if 'completed' in data:
            task.completed = bool(data['completed'])
        if 'planned_date' in data:
            try:
                task.planned_date = datetime.strptime(data['planned_date'], '%Y-%m-%d').date()
            except ValueError:
                return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400
        if 'notes' in data:
            task.description = data['notes'].strip()

        task.updated_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/schedule/<int:schedule_id>', methods=['DELETE'])
@student_required
def delete_schedule(schedule_id):
    """حذف مهمة يومية"""
    try:
        task = UnifiedTask.query.get(schedule_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != session['student_id']:
            return jsonify({"status": "error", "message": "غير مصرح لك بحذف هذه المهمة"}), 403
        db.session.delete(task)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# واجهات إدارة تقدم الدروس (Progress)
# ==========================================
@app.route('/api/progress', methods=['GET'])
@student_required
def get_progress():
    try:
        student_id = session['student_id']
        progress = Progress.query.filter_by(student_id=student_id).all()
        return jsonify([p.to_dict() for p in progress])
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/progress', methods=['POST'])
@student_required
def add_or_update_progress():
    try:
        data = request.get_json(silent=True) or {}
        lesson_id = data.get('lesson_id')
        status = data.get('status', 'not_started')
        progress_percent = data.get('progress_percent', 0)

        if not lesson_id:
            return jsonify({"status": "error", "message": "معرف الدرس مطلوب"}), 400

        lesson = CustomLesson.query.get(lesson_id)
        if not lesson:
            return jsonify({"status": "error", "message": "الدرس غير موجود"}), 404

        student_id = session['student_id']
        progress = Progress.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()
        if not progress:
            progress = Progress(student_id=student_id, lesson_id=lesson_id)
            db.session.add(progress)

        if status in ['not_started', 'in_progress', 'completed']:
            progress.status = status
        if isinstance(progress_percent, int) and 0 <= progress_percent <= 100:
            progress.progress_percent = progress_percent
        progress.last_accessed = datetime.utcnow()

        db.session.commit()
        return jsonify({"status": "success", "progress": progress.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# تشغيل التطبيق
# ==========================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
