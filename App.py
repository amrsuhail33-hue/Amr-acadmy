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
    send_from_directory, redirect, url_for, session,
    make_response
)
from flask_sqlalchemy import SQLAlchemy
from groq import Groq
from werkzeug.security import generate_password_hash, check_password_hash
from webauthn import generate_registration_options, verify_registration_response
from webauthn import generate_authentication_options, verify_authentication_response
from webauthn.helpers.structs import (
    RegistrationCredential, AuthenticationCredential,
    AuthenticatorSelectionCriteria, UserVerificationRequirement,
    ResidentKeyRequirement
)
from webauthn.helpers import bytes_to_base64url, base64url_to_bytes
import uuid
import math

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'amr_suhail_academy_secure_secret_key_2026')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# ==========================================
# إعداد قاعدة البيانات
# ==========================================
raw_db_url = os.environ.get('DATABASE_URL', 'sqlite:///academy_qa.db').strip()

if raw_db_url.startswith("postgres://"):
    db_url = raw_db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif raw_db_url.startswith("postgresql://"):
    db_url = raw_db_url.replace("postgresql://", "postgresql+pg8000://", 1)
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
# دالة حساب التقدير الأكاديمي
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

class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    profile = db.relationship('UserProfile', backref='student', uselist=False)
    credentials = db.relationship('WebAuthnCredential', backref='student', lazy='dynamic')

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

class UserProfile(db.Model):
    __tablename__ = 'user_profiles'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), unique=True, nullable=False)
    level = db.Column(db.Integer, default=1)
    points = db.Column(db.Integer, default=0)
    total_points = db.Column(db.Integer, default=0)
    show_in_leaderboard = db.Column(db.Boolean, default=True)
    streak_days = db.Column(db.Integer, default=0)
    last_active = db.Column(db.Date, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def calculate_level(self):
        if self.points < 0:
            self.points = 0
        new_level = math.floor(self.points / 100) + 1
        if new_level < 1:
            new_level = 1
        return new_level

    def add_points(self, points, reason="نشاط عام"):
        self.points += points
        self.total_points += points if points > 0 else 0
        old_level = self.level
        self.level = self.calculate_level()
        self.updated_at = datetime.utcnow()
        transaction = PointTransaction(
            student_id=self.student_id,
            points=points,
            reason=reason,
            level_before=old_level,
            level_after=self.level
        )
        db.session.add(transaction)
        today = datetime.utcnow().date()
        if self.last_active == today - timedelta(days=1):
            self.streak_days += 1
        elif self.last_active != today:
            self.streak_days = 1
        self.last_active = today
        db.session.commit()
        return {"level_up": self.level > old_level, "new_level": self.level, "new_points": self.points}

    def deduct_points(self, points, reason="خصم"):
        if points <= 0:
            return
        self.points -= points
        if self.points < 0:
            self.points = 0
        old_level = self.level
        self.level = self.calculate_level()
        self.updated_at = datetime.utcnow()
        transaction = PointTransaction(
            student_id=self.student_id,
            points=-points,
            reason=reason,
            level_before=old_level,
            level_after=self.level
        )
        db.session.add(transaction)
        db.session.commit()
        return {"level_down": self.level < old_level, "new_level": self.level, "new_points": self.points}

    def to_dict(self):
        return {
            "student_id": self.student_id,
            "level": self.level,
            "points": self.points,
            "total_points": self.total_points,
            "streak_days": self.streak_days,
            "show_in_leaderboard": self.show_in_leaderboard,
            "level_progress": (self.points % 100) / 100 * 100 if self.points > 0 else 0,
            "next_level_points": (math.floor(self.points / 100) + 1) * 100
        }

class PointTransaction(db.Model):
    __tablename__ = 'point_transactions'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    points = db.Column(db.Integer, nullable=False)
    reason = db.Column(db.String(255), nullable=False)
    level_before = db.Column(db.Integer)
    level_after = db.Column(db.Integer)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    student = db.relationship('Student')

    def to_dict(self):
        return {
            "id": self.id,
            "points": self.points,
            "reason": self.reason,
            "level_before": self.level_before,
            "level_after": self.level_after,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class UnifiedTask(db.Model):
    __tablename__ = 'unified_tasks'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    planned_date = db.Column(db.Date, nullable=False)
    completed = db.Column(db.Boolean, default=False)
    priority = db.Column(db.Integer, default=2)
    source = db.Column(db.String(20), default='manual')
    allocated_hours = db.Column(db.Float, default=1.0)
    is_wird = db.Column(db.Boolean, default=False)
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
            "allocated_hours": self.allocated_hours or 1.0,
            "is_wird": self.is_wird,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else "",
            "updated_at": self.updated_at.strftime("%Y-%m-%d %H:%M") if self.updated_at else ""
        }

class WebAuthnCredential(db.Model):
    __tablename__ = 'webauthn_credentials'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    credential_id = db.Column(db.String(255), unique=True, nullable=False)
    public_key = db.Column(db.Text, nullable=False)
    sign_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    # ❌ تم حذف الـ student relationship من هنا لمنع التعارض مع الـ backref في كلاس Student

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "credential_id": self.credential_id,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

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
    priority = db.Column(db.Integer, default=2)
    
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
# إنشاء الجداول
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
        students = Student.query.all()
        for s in students:
            if not UserProfile.query.filter_by(student_id=s.id).first():
                profile = UserProfile(student_id=s.id)
                db.session.add(profile)
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

# ==========================================
# دالة بناء بيانات المنصة
# ==========================================
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

@app.after_request
def add_header(response):
    if request.path.startswith('/') and not request.path.startswith('/static'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    if 'Safari' in request.headers.get('User-Agent', ''):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    return response

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
# استرجاع بنك الأسئلة
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

@app.route('/api/quiz/check', methods=['POST'])
@student_required
def check_quiz_answer():
    try:
        data = request.get_json()
        question_id = data.get('question_id')
        selected_answer = data.get('selected_answer')
        
        question = QuizQuestion.query.get(question_id)
        if not question:
            return jsonify({"status": "error", "message": "السؤال غير موجود"}), 404
        
        is_correct = (selected_answer == question.correct_answer)
        student_id = session['student_id']
        profile = UserProfile.query.filter_by(student_id=student_id).first()
        
        result = {"is_correct": is_correct}
        
        if is_correct:
            if profile:
                update_result = profile.add_points(5, f"إجابة صحيحة: {question.question_text[:30]}...")
                result["points_added"] = 5
                result["level_up"] = update_result.get("level_up", False)
                result["new_level"] = update_result.get("new_level", profile.level)
                result["new_points"] = update_result.get("new_points", profile.points)
        else:
            if profile:
                update_result = profile.deduct_points(3, f"إجابة خاطئة: {question.question_text[:30]}...")
                result["points_deducted"] = 3
                result["level_down"] = update_result.get("level_down", False)
                result["new_level"] = update_result.get("new_level", profile.level)
                result["new_points"] = update_result.get("new_points", profile.points)
        
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# إدارة المفضلة
# ==========================================
@app.route('/api/favorites', methods=['GET', 'POST', 'DELETE'])
@student_required
def manage_favorites():
    try:
        student_id = session['student_id']
        if request.method == 'GET':
            favs = Favorite.query.filter_by(student_id=student_id).order_by(Favorite.added_at.desc()).all()
            return jsonify([f.to_dict() for f in favs])

        data = request.get_json(silent=True) or {}
        lesson_id = data.get('lesson_id')
        if not lesson_id:
            return jsonify({"status": "error", "message": "معرف الدرس مطلوب"}), 400

        fav = Favorite.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()

        if request.method == 'POST':
            if not fav:
                new_fav = Favorite(student_id=student_id, lesson_id=lesson_id)
                db.session.add(new_fav)
                db.session.commit()
            return jsonify({"status": "success", "message": "تم الإضافة للمفضلة"})

        elif request.method == 'DELETE':
            if fav:
                db.session.delete(fav)
                db.session.commit()
            return jsonify({"status": "success", "message": "تم الحذف من المفضلة"})

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# API الملف الشخصي
# ==========================================
@app.route('/api/profile', methods=['GET'])
@student_required
def get_profile():
    try:
        student_id = session['student_id']
        student = Student.query.get(student_id)
        profile = UserProfile.query.filter_by(student_id=student_id).first()
        
        if not profile:
            profile = UserProfile(student_id=student_id)
            db.session.add(profile)
            db.session.commit()
        
        completed_tasks = UnifiedTask.query.filter_by(student_id=student_id, completed=True).count()
        total_tasks = UnifiedTask.query.filter_by(student_id=student_id).count()
        
        recent_transactions = PointTransaction.query.filter_by(student_id=student_id).order_by(PointTransaction.created_at.desc()).limit(10).all()
        
        return jsonify({
            "status": "success",
            "profile": profile.to_dict(),
            "student": student.to_dict(),
            "stats": {
                "completed_tasks": completed_tasks,
                "total_tasks": total_tasks,
                "completion_rate": round((completed_tasks / total_tasks * 100) if total_tasks > 0 else 0, 1)
            },
            "recent_transactions": [t.to_dict() for t in recent_transactions]
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/profile/visibility', methods=['POST'])
@student_required
def update_profile_visibility():
    try:
        data = request.get_json()
        show = data.get('show', True)
        student_id = session['student_id']
        profile = UserProfile.query.filter_by(student_id=student_id).first()
        if not profile:
            return jsonify({"status": "error", "message": "الملف الشخصي غير موجود"}), 404
        profile.show_in_leaderboard = bool(show)
        db.session.commit()
        return jsonify({"status": "success", "show_in_leaderboard": profile.show_in_leaderboard})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/leaderboard', methods=['GET'])
def get_leaderboard():
    try:
        profiles = UserProfile.query.filter_by(show_in_leaderboard=True).order_by(UserProfile.points.desc()).limit(50).all()
        result = []
        rank = 1
        for p in profiles:
            student = Student.query.get(p.student_id)
            if student:
                result.append({
                    "rank": rank,
                    "student_id": p.student_id,
                    "name": student.name,
                    "username": student.username,
                    "level": p.level,
                    "points": p.points,
                    "streak_days": p.streak_days
                })
                rank += 1
        return jsonify({"status": "success", "leaderboard": result})
    except Exception as e:
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
# Routes الطالب (مع دعم البصمة)
# ==========================================
@app.route('/student/register', methods=['POST'])
def student_register():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()
        name = (data.get('name') or '').strip()

        if not username or not password or not name:
            return jsonify({"status": "error", "message": "جميع الحقول مطلوبة"}), 400

        if Student.query.filter_by(username=username).first():
            return jsonify({"status": "error", "message": "اسم المستخدم موجود بالفعل"}), 400

        student = Student(username=username, name=name)
        student.set_password(password)
        db.session.add(student)
        db.session.commit()
        
        profile = UserProfile(student_id=student.id)
        db.session.add(profile)
        db.session.commit()

        return jsonify({"status": "success", "message": "تم تسجيل الطالب بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/student/login', methods=['POST'])
def student_login():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = (data.get('username') or '').strip()
        password = (data.get('password') or '').strip()

        student = Student.query.filter_by(username=username).first()
        if not student or not student.check_password(password):
            return jsonify({"status": "error", "message": "اسم المستخدم أو كلمة المرور غير صحيحة"}), 401

        session.permanent = True
        session['student_id'] = student.id
        session['student_name'] = student.name
        session.pop('is_admin', None)
        session.pop('is_assistant', None)

        return jsonify({"status": "success", "student": student.to_dict()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/student/status', methods=['GET'])
def student_status():
    if session.get('student_id'):
        student = Student.query.get(session['student_id'])
        if student:
            return jsonify({"status": "logged_in", "student": student.to_dict()})
    return jsonify({"status": "logged_out"})

@app.route('/student/logout', methods=['POST'])
def student_logout():
    try:
        session.pop('student_id', None)
        session.pop('student_name', None)
        session.pop('webauthn_challenge', None)
        session.pop('webauthn_username', None)
        session.pop('webauthn_user_id', None)
        return jsonify({"status": "success", "message": "تم تسجيل الخروج بنجاح"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# إدارة المحتوى
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
# حساب التقديرات
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
# خدمات الذكاء الاصطناعي
# ==========================================
@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        if request.is_json:
            data = request.get_json(silent=True) or {}
            user_message = (data.get('message') or '').strip()
        else:
            user_message = (request.form.get('message') or '').strip()
            file = request.files.get('file')
            if file and not user_message:
                user_message = "يرجى شرح المرفقات"

        messages = [
            {
                "role": "system", 
                "content": "أنت المساعد الأكاديمي الذكي للأكاديمية. وظيفتك الإجابة بوضوح وبأسلوب علمي وهندسي مبسط، وشرح خطوات الحل للطلاب خطوة بخطوة باللغة العربية. (ملاحظة: إذا سألك المستخدم عن صورة، اعتذر بلطف لأنك نموذج لغوي فقط حالياً واطلب منه كتابة استفساره)."
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

# ==========================================
# Route Planner
# ==========================================
@app.route('/planner')
def planner():
    student_id = session.get('student_id')
    student_name = session.get('student_name')
    return render_template('planner.html', student_id=student_id, student_name=student_name)

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
        
        if not student_id:
            return jsonify({"error": "يرجى تسجيل الدخول أولاً"}), 401
        
        discipline = (data.get('discipline') or 'متوازن').strip()
        wird_pages = data.get('wird_pages', 1)
        wird_enabled = data.get('wird', True)
        
        base_profiles = {
            "مرن": {
                "hours": "1.5 to 2.5 hours total",
                "tasks": "1 to 2 light tasks per active day",
                "style": "Conceptual reading, high-level summaries, and gentle reviews without academic stress."
            },
            "متوازن": {
                "hours": "3.5 to 5 hours total",
                "tasks": "2 to 3 structured tasks per active day",
                "style": "Standard lecture study, chapter breakdowns, and moderate practice exercise solving."
            },
            "مكثف": {
                "hours": "6 to 8.5+ hours total",
                "tasks": "3 to 5 deep-focus tasks per active day",
                "style": "Comprehensive chapter mastery, intense problem-solving sessions, past exam papers, and rigorous timed drills."
            }
        }
        
        profile = base_profiles.get(discipline, base_profiles["متوازن"])
        
        wird_text = ""
        if wird_enabled and wird_pages > 0:
            wird_text = f"\n- Daily Wird (Quran reading): {wird_pages} pages every day.\n- IMPORTANT: If the user misses the daily Wird, it MUST be redistributed to the next available days as a priority task.\n- The Wird task should be included every day and marked as 'الورد اليومي (قراءة {wird_pages} صفحات)'."
        else:
            wird_text = "\n- No daily Wird required.\n"
        
        discipline_profile = (
            f"PACE: {discipline.upper()}\n"
            f"- Daily study workload: {profile['hours']}.\n"
            f"- Tasks count: {profile['tasks']}.\n"
            f"- Task style: {profile['style']}.\n"
            f"{wird_text}"
        )

        system_instruction = (
            "You are an expert academic planner. Generate a highly realistic, customized daily study schedule in pure JSON format.\n\n"
            f"SELECTED DISCIPLINE MODE & CONSTRAINTS:\n{discipline_profile}\n\n"
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
            "5. If Quran reading (الورد) is enabled, include it as a daily task with appropriate allocated hours.\n"
            "6. If Quran memorization is enabled, allocate proper time for the requested page count.\n"
            "7. All task titles, subjects, and day names must be written in clear, professional Arabic.\n"
            "8. IMPORTANT: If the user misses the daily Wird, redistribute it to the next available days."
        )

        user_prompt = (
            f"Start Date: {data.get('current_date')}\n"
            f"Target Date: {data.get('target_date')}\n"
            f"Subjects/Courses: {data.get('subjects')}\n"
            f"Requested Discipline Mode: {discipline}\n"
            f"Sleep Hours: {data.get('sleep_hours')}\n"
            f"Busy/Work Hours: {data.get('busy_hours')}\n"
            f"Weekly Off-Days: {', '.join(data.get('off_days', []))}\n"
            f"Include Daily Wird: {'Yes, ' + str(wird_pages) + ' pages' if wird_enabled and wird_pages > 0 else 'No'}\n"
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
                        allocated_hours = task.get('allocated_hours', 1.5)
                        is_wird = 'الورد' in task_title or 'قراءة' in task_title
                        
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
                            allocated_hours=allocated_hours,
                            is_wird=is_wird,
                            completed=False
                        )
                        db.session.add(unified_task)
                db.session.commit()
                return jsonify({"status": "success", "plan": plan_data})
            except Exception as e:
                db.session.rollback()
                return jsonify({"error": f"Error saving tasks: {str(e)}"}), 500

        return jsonify({"status": "success", "plan": plan_data})

    except Exception as e:
        return jsonify({"error": f"Error generating plan: {str(e)}"}), 500

@app.route('/reschedule_plan', methods=['POST'])
def reschedule_plan():
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "يرجى تسجيل الدخول أولاً"}), 401
            
        missed = data.get('missed_tasks', [])
        remaining = data.get('remaining_plans', [])

        system_instruction = (
            "You are an academic rescheduling assistant. Re-distribute uncompleted tasks smartly into the remaining study days.\n"
            "STRICT RULES:\n"
            "1. Output ONLY a valid JSON object with the key 'plans' containing the updated days array.\n"
            "2. Preserve the exact structure: [{'day_name': '...', 'date': '...', 'is_off_day': false, 'tasks': [{'title': '...', 'allocated_hours': 1.5}]}]\n"
            "3. Do not drop any tasks; merge missed ones logically into active study days without overloading off-days.\n"
            "4. Priority tasks (like Quran Wird) should be scheduled as early as possible in the remaining days."
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
                        allocated_hours = task.get('allocated_hours', 1.5)
                        is_wird = 'الورد' in task_title or 'قراءة' in task_title
                        
                        existing_task = UnifiedTask.query.filter(
                            UnifiedTask.student_id == student_id,
                            UnifiedTask.title == task_title,
                            UnifiedTask.completed == False
                        ).first()
                        if existing_task:
                            existing_task.planned_date = planned_date
                            existing_task.allocated_hours = allocated_hours
                            existing_task.is_wird = is_wird
                        else:
                            unified_task = UnifiedTask(
                                student_id=student_id,
                                title=task_title,
                                planned_date=planned_date,
                                source='planner',
                                priority=2,
                                allocated_hours=allocated_hours,
                                is_wird=is_wird,
                                completed=False
                            )
                            db.session.add(unified_task)
                db.session.commit()
            except Exception as e:
                db.session.rollback()

        return jsonify({"status": "success", "plan": plan_data})

    except Exception as e:
        return jsonify({"error": f"Error rescheduling plan: {str(e)}"}), 500

@app.route('/api/tasks/complete/<int:task_id>', methods=['POST'])
@student_required
def complete_task(task_id):
    try:
        student_id = session['student_id']
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != student_id:
            return jsonify({"status": "error", "message": "غير مصرح لك"}), 403
        
        if task.completed:
            return jsonify({"status": "error", "message": "المهمة مكتملة بالفعل"}), 400
        
        task.completed = True
        db.session.commit()
        
        profile = UserProfile.query.filter_by(student_id=student_id).first()
        points_earned = 10
        reason = f"إكمال مهمة: {task.title}"
        if task.is_wird:
            points_earned = 5
            reason = f"إنجاز الورد القرآني: {task.title}"
        
        result = profile.add_points(points_earned, reason) if profile else None
        
        return jsonify({
            "status": "success",
            "message": "تم إكمال المهمة بنجاح!",
            "points_earned": points_earned,
            "profile": profile.to_dict() if profile else None
        })
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# واجهات المهام الموحدة
# ==========================================
@app.route('/api/tasks', methods=['GET'])
@student_required
def get_unified_tasks():
    try:
        student_id = session.get('student_id')
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
    try:
        data = request.get_json(silent=True) or {}
        student_id = session['student_id']
        
        title = (data.get('title') or '').strip()
        planned_date = data.get('planned_date')
        source = data.get('source', 'manual')
        priority = data.get('priority', 2)
        lesson_id = data.get('lesson_id')
        description = data.get('description', '')
        allocated_hours = data.get('allocated_hours', 1.0)
        is_wird = data.get('is_wird', False)

        if not title or not planned_date:
            return jsonify({"status": "error", "message": "العنوان والتاريخ مطلوبان"}), 400

        try:
            planned_date_obj = datetime.strptime(planned_date, '%Y-%m-%d').date()
        except ValueError:
            return jsonify({"status": "error", "message": "صيغة التاريخ غير صحيحة"}), 400

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
            allocated_hours=allocated_hours,
            is_wird=is_wird,
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
    try:
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != session['student_id']:
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل هذه المهمة"}), 403

        data = request.get_json(silent=True) or {}
        
        if 'completed' in data:
            is_completed = bool(data['completed'])
            if is_completed and not task.completed:
                profile = UserProfile.query.filter_by(student_id=session['student_id']).first()
                if profile:
                    points = 5 if task.is_wird else 10
                    profile.add_points(points, f"إكمال مهمة: {task.title}")
            task.completed = is_completed
            
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
        if 'allocated_hours' in data:
            task.allocated_hours = float(data['allocated_hours'])
        if 'is_wird' in data:
            task.is_wird = bool(data['is_wird'])
        
        task.updated_at = datetime.utcnow()
        db.session.commit()
        
        return jsonify({"status": "success", "task": task.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks/<int:task_id>', methods=['DELETE'])
@student_required
def delete_unified_task(task_id):
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
    try:
        student_id = session['student_id']
        data = request.get_json(silent=True) or {}
        tasks_data = data.get('tasks', [])
        
        for task_data in tasks_data:
            task_id = task_data.get('id')
            if task_id:
                task = UnifiedTask.query.get(task_id)
                if task and task.student_id == student_id:
                    if 'completed' in task_data:
                        is_completed = bool(task_data['completed'])
                        if is_completed and not task.completed:
                            profile = UserProfile.query.filter_by(student_id=student_id).first()
                            if profile:
                                points = 5 if task.is_wird else 10
                                profile.add_points(points, f"إكمال مهمة: {task.title}")
                        task.completed = is_completed
                    if 'planned_date' in task_data:
                        try:
                            task.planned_date = datetime.strptime(task_data['planned_date'], '%Y-%m-%d').date()
                        except ValueError:
                            continue
                    if 'allocated_hours' in task_data:
                        task.allocated_hours = float(task_data['allocated_hours'])
                    if 'is_wird' in task_data:
                        task.is_wird = bool(task_data['is_wird'])
                    task.updated_at = datetime.utcnow()
                    continue
            
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
                allocated_hours=task_data.get('allocated_hours', 1.0),
                is_wird=task_data.get('is_wird', False),
                completed=task_data.get('completed', False)
            )
            db.session.add(new_task)
        
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت المزامنة بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/lessons', methods=['GET'])
@student_required
def get_all_lessons():
    try:
        lessons = CustomLesson.query.order_by(CustomLesson.subject_name, CustomLesson.chapter_name).all()
        result = []
        for l in lessons:
            result.append({
                "id": l.id,
                "title": l.title,
                "subject_name": l.subject_name,
                "chapter_name": l.chapter_name,
                "content_type": l.content_type,
                "year": l.year,
                "term": l.term
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

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
                        {{"lesson_id": 1, "title": "مقدمة في الدايدود", "priority": "عال", "minutes": 45, "allocated_hours": 0.75}}
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
                allocated_hours = task.get('allocated_hours', task.get('minutes', 30) / 60.0)
                
                unified_task = UnifiedTask(
                    student_id=student_id,
                    lesson_id=lesson_id,
                    title=task.get('title', lesson.title),
                    description=f"خطة اختبار: {plan_name} - أولوية: {task.get('priority', 'متوسط')} - {task.get('minutes', 30)} دقيقة",
                    planned_date=day_date,
                    source='exam',
                    priority=priority_map.get(task.get('priority', 'متوسط'), 2),
                    allocated_hours=allocated_hours,
                    is_wird=False,
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

        old_progress = progress.progress_percent
        if status in ['not_started', 'in_progress', 'completed']:
            progress.status = status
        if isinstance(progress_percent, int) and 0 <= progress_percent <= 100:
            progress.progress_percent = progress_percent
        progress.last_accessed = datetime.utcnow()
        db.session.commit()

        if progress.progress_percent == 100 and old_progress < 100:
            profile = UserProfile.query.filter_by(student_id=student_id).first()
            if profile:
                profile.add_points(10, f"إكمال الدرس: {lesson.title}")
                db.session.commit()

        return jsonify({"status": "success", "progress": progress.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# WebAuthn (تسجيل الدخول بالبصمة)
# ==========================================
RP_ID = os.environ.get('RP_ID', 'amr-test-w4qb.onrender.com') 
RP_NAME = "Amr Suhail Academy"
ORIGIN = os.environ.get('ORIGIN', 'https://amr-test-w4qb.onrender.com')

@app.route('/webauthn/register/begin', methods=['POST'])
def webauthn_register_begin():
    try:
        data = request.get_json()
        username = data.get('username')
        if not username:
            return jsonify({"status": "error", "message": "اسم المستخدم مطلوب"}), 400

        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "المستخدم غير موجود"}), 404

        registration_options = generate_registration_options(
            rp_id=RP_ID,
            rp_name=RP_NAME,
            user_id=str(student.id).encode(),
            user_name=student.username,
            user_display_name=student.name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                user_verification=UserVerificationRequirement.PREFERRED,
                resident_key=ResidentKeyRequirement.PREFERRED,
            ),
        )

        session['webauthn_challenge'] = registration_options.challenge
        session['webauthn_user_id'] = str(student.id)

        return jsonify({
            "status": "success",
            "options": {
                "challenge": registration_options.challenge,
                "rp": {
                    "name": registration_options.rp.name,
                    "id": registration_options.rp.id,
                },
                "user": {
                    "id": registration_options.user.id,
                    "name": registration_options.user.name,
                    "displayName": registration_options.user.display_name,
                },
                "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
                "authenticatorSelection": {
                    "authenticatorAttachment": "platform",
                    "residentKey": "preferred",
                    "userVerification": "preferred",
                },
                "attestation": "none",
                "excludeCredentials": [
                    {"id": cred.credential_id, "type": "public-key"}
                    for cred in WebAuthnCredential.query.filter_by(student_id=student.id).all()
                ]
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webauthn/register/complete', methods=['POST'])
def webauthn_register_complete():
    try:
        data = request.get_json()
        credential = data.get('credential')
        user_id = session.get('webauthn_user_id')
        challenge = session.get('webauthn_challenge')

        if not user_id or not challenge:
            return jsonify({"status": "error", "message": "انتهت الجلسة"}), 400

        student = Student.query.get(int(user_id))
        if not student:
            return jsonify({"status": "error", "message": "المستخدم غير موجود"}), 404

        credential_id = base64url_to_bytes(credential['id'])
        client_data_json = base64url_to_bytes(credential['response']['clientDataJSON'])
        attestation_object = base64url_to_bytes(credential['response']['attestationObject'])

        verification = verify_registration_response(
            credential=RegistrationCredential(
                id=credential_id,
                response={
                    "clientDataJSON": client_data_json,
                    "attestationObject": attestation_object,
                },
                type="public-key",
            ),
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            require_user_verification=False,
        )

        new_cred = WebAuthnCredential(
            student_id=student.id,
            credential_id=bytes_to_base64url(verification.credential_id),
            public_key=verification.credential_public_key,
            sign_count=verification.sign_count,
        )
        db.session.add(new_cred)
        db.session.commit()

        session.pop('webauthn_challenge', None)
        session.pop('webauthn_user_id', None)

        return jsonify({"status": "success", "message": "تم تسجيل البصمة بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webauthn/login/begin', methods=['POST'])
def webauthn_login_begin():
    try:
        data = request.get_json()
        username = data.get('username')
        if not username:
            return jsonify({"status": "error", "message": "اسم المستخدم مطلوب"}), 400

        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "المستخدم غير موجود"}), 404

        credentials = WebAuthnCredential.query.filter_by(student_id=student.id).all()
        if not credentials:
            return jsonify({"status": "error", "message": "لا توجد بصمة مسجلة لهذا المستخدم"}), 400

        authentication_options = generate_authentication_options(
            rp_id=RP_ID,
            allow_credentials=[
                {"id": base64url_to_bytes(c.credential_id), "type": "public-key"}
                for c in credentials
            ],
            user_verification=UserVerificationRequirement.PREFERRED,
        )

        session['webauthn_challenge'] = authentication_options.challenge
        session['webauthn_username'] = username

        return jsonify({
            "status": "success",
            "options": {
                "challenge": authentication_options.challenge,
                "rpId": authentication_options.rp_id,
                "allowCredentials": [
                    {"id": c.credential_id, "type": "public-key"}
                    for c in credentials
                ],
                "userVerification": "preferred",
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/webauthn/login/complete', methods=['POST'])
def webauthn_login_complete():
    try:
        data = request.get_json()
        credential = data.get('credential')
        username = session.get('webauthn_username')
        challenge = session.get('webauthn_challenge')

        if not username or not challenge:
            return jsonify({"status": "error", "message": "انتهت الجلسة"}), 400

        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "المستخدم غير موجود"}), 404

        cred_id = credential['id']
        db_cred = WebAuthnCredential.query.filter_by(credential_id=cred_id).first()
        if not db_cred:
            return jsonify({"status": "error", "message": "البيانات غير صالحة"}), 400

        verification = verify_authentication_response(
            credential=AuthenticationCredential(
                id=base64url_to_bytes(cred_id),
                response={
                    "clientDataJSON": base64url_to_bytes(credential['response']['clientDataJSON']),
                    "authenticatorData": base64url_to_bytes(credential['response']['authenticatorData']),
                    "signature": base64url_to_bytes(credential['response']['signature']),
                    "userHandle": None,
                },
                type="public-key",
            ),
            expected_challenge=challenge,
            expected_rp_id=RP_ID,
            expected_origin=ORIGIN,
            credential_public_key=db_cred.public_key,
            credential_current_sign_count=db_cred.sign_count,
            require_user_verification=False,
        )

        db_cred.sign_count = verification.new_sign_count
        db.session.commit()

        session.permanent = True
        session['student_id'] = student.id
        session['student_name'] = student.name
        session.pop('is_admin', None)
        session.pop('is_assistant', None)

        session.pop('webauthn_challenge', None)
        session.pop('webauthn_username', None)

        return jsonify({"status": "success", "student": student.to_dict()})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health')
def health_check():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected" if db.engine else "disconnected"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
