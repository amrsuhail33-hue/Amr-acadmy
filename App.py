import os
import io
import csv
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
from sqlalchemy import Uuid
from sqlalchemy.sql import text
from groq import Groq
from werkzeug.security import generate_password_hash, check_password_hash
import math

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or os.urandom(32)
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)

# ==========================================
# Database Setup
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
    # Render Postgres يستخدم شهادة موقعة ذاتياً، فالتحقق الافتراضي يفشل
    # (SSLCertVerificationError, tls_verify_code=19). إذا وفّرنا ملف CA عبر
    # PGSSLROOTCERT نبقي التحقق مفعّلاً، وإلا نعطّل التحقق لهذا الاتصال فقط.
    ssl_context = ssl.create_default_context()
    ca_file = os.environ.get("PGSSLROOTCERT")
    if ca_file:
        ssl_context.load_verify_locations(cafile=ca_file)
    else:
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {
            "ssl_context": ssl_context
        }
    }


db = SQLAlchemy(app)

GROQ_API_KEY = os.environ.get('GROQ_API_KEY')
groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
GROQ_MODEL = "openai/gpt-oss-120b"

# ==========================================
# Database Models
# ==========================================

class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    # نوع Uuid الأصلي: يظهر كـ UUID في Postgres (مطابق للعمود الموجود فلا حاجة لـ migration) وكـ CHAR في SQLite
    auth_user_id = db.Column(Uuid, nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), default='student')  # student, assistant, admin
    college = db.Column(db.String(150), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    profile = db.relationship('UserProfile', backref='student', uselist=False, cascade="all, delete-orphan")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        try:
            return check_password_hash(self.password_hash, password)
        except Exception:
            return self.password_hash == password

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "role": self.role or 'student',
            "college": self.college or '',
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
        return max(new_level, 1)

    def add_points(self, points, reason="نشاط عام"):
        if points <= 0:
            return {"level_up": False, "new_level": self.level, "new_points": self.points}

        recent = PointTransaction.query.filter_by(student_id=self.student_id, reason=reason).first()
        if recent and "محاولة اختبار" not in reason:
            return {"level_up": False, "new_level": self.level, "new_points": self.points}

        self.points += points
        self.total_points += points
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
        elif (self.streak_days or 0) == 0:
            # أول نشاط في اليوم نفسه الذي أُنشئ فيه الملف (last_active == today):
            # يجب أن يبدأ التتابع من 1 بدل البقاء صفراً رغم النشاط.
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

class LessonQuestion(db.Model):
    __tablename__ = 'lesson_questions'
    id = db.Column(db.Integer, primary_key=True)
    subject_id = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    student_name = db.Column(db.String(100), nullable=False, default="طالب")
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    question_text = db.Column(db.Text, nullable=False)
    answer_text = db.Column(db.Text, nullable=True)
    is_answered = db.Column(db.Boolean, default=False)
    answered_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    answered_at = db.Column(db.DateTime, nullable=True)
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
    tag = db.Column(db.String(50), default="إضافة جديدة")
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
    college = db.Column(db.String(150), nullable=True)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    content_type = db.Column(db.String(20), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    url = db.Column(db.Text, nullable=True)
    download_url = db.Column(db.Text, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved')
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        date_str = self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        return {
            "id": self.id,
            "year": self.year,
            "term": self.term,
            "college": self.college or "",
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "content_type": self.content_type,
            "title": self.title,
            "url": self.url or "",
            "download_url": self.download_url or "",
            "notes": self.notes or "",
            "status": self.status or 'approved',
            "created_by": self.created_by,
            "created_at": date_str
        }

class QuizQuestion(db.Model):
    __tablename__ = 'quiz_questions'
    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    college = db.Column(db.String(150), nullable=True)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.Text, nullable=True)
    option_b = db.Column(db.Text, nullable=True)
    option_c = db.Column(db.Text, nullable=True)
    option_d = db.Column(db.Text, nullable=True)
    correct_answer = db.Column(db.String(200), nullable=True)
    explanation = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved')
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        q_text = (self.question_text or '').strip()
        opts = [self.option_a, self.option_b, self.option_c, self.option_d]
        cleaned_opts = []
        for o in opts:
            if o and o.strip() and o.strip() != q_text:
                cleaned_opts.append(o.strip())

        # قد تُستورد correct_answer كحرف (A-D) أو رقم (1-4) بدل نص الخيار؛
        # نحوّلها إلى نص الخيار المطابق حتى تقارنها الواجهة بشكل صحيح.
        correct = (self.correct_answer or "").strip()
        if len(correct) == 1 and correct.upper() in "ABCD":
            idx = "ABCD".index(correct.upper())
            if idx < len(cleaned_opts):
                correct = cleaned_opts[idx]
        elif len(correct) == 1 and correct in "1234":
            idx = int(correct) - 1
            if idx < len(cleaned_opts):
                correct = cleaned_opts[idx]

        return {
            "id": self.id,
            "year": self.year,
            "term": self.term,
            "college": self.college or "",
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "question_text": q_text,
            "option_a": cleaned_opts[0] if len(cleaned_opts) > 0 else (self.option_a or ""),
            "option_b": cleaned_opts[1] if len(cleaned_opts) > 1 else (self.option_b or ""),
            "option_c": cleaned_opts[2] if len(cleaned_opts) > 2 else (self.option_c or ""),
            "option_d": cleaned_opts[3] if len(cleaned_opts) > 3 else (self.option_d or ""),
            "correct_answer": correct,
            "explanation": self.explanation or "",
            "status": self.status or 'approved'
        }

class QuizAttempt(db.Model):
    __tablename__ = 'quiz_attempts'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=True)
    score = db.Column(db.Integer, nullable=False)
    total_questions = db.Column(db.Integer, nullable=False)
    percentage = db.Column(db.Float, nullable=False)
    xp_awarded = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "score": self.score,
            "total_questions": self.total_questions,
            "percentage": self.percentage,
            "xp_awarded": self.xp_awarded,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

with app.app_context():
    try:
        db.create_all()
        students = Student.query.all()
        for s in students:
            if not UserProfile.query.filter_by(student_id=s.id).first():
                profile = UserProfile(student_id=s.id)
                db.session.add(profile)

        db.session.commit()

        # --- تصحيح لمرة واحدة (Dev): إجابات مخزنة خاطئة في أسئلة إلكترونك، الشبتر السادس ---
        # القيم الصحيحة تم التحقق منها مقابل نصوص خيارات كل سؤال عبر /api/quiz.
        # يطبّق التصحيح مرة واحدة فقط (على الصفوف التي ما زالت تحمل القيمة الخاطئة)
        # ثم يُحذف هذا الكتلة في التزام تنظيف لاحق.
        # ملاحظة: عمود correct_answer في قاعدة البيانات الحية varchar(50) بينما النموذج
        # String(100)، وبعض الإجابات الصحيحة أطول من 50 حرفاً؛ لذا نوسّع العمود أولاً.
        # الكتلة كلها معزولة: أي خطأ فيها يُسجَّل فقط ولا يُسقط تشغيل التطبيق.
        try:
            if "sqlite" not in db_url:
                db.session.execute(text(
                    "ALTER TABLE quiz_questions ALTER COLUMN correct_answer TYPE VARCHAR(200)"
                ))
                db.session.commit()
            # الحرف يشير إلى عمود الخيار في الصف نفسه؛ نأخذ نص الخيار من الصف
            # مباشرة فلا تعتمد المطابقة على نص مكتوب يدوياً (قد يختلف بمسافة خفية).
            # التحقق اليدوي من /api/quiz: الإجابة الصحيحة لكل سؤال هي الخيار المذكور.
            _QUIZ_ANSWER_LETTERS = {
                1: "c", 2: "b", 3: "d", 4: "b", 5: "c",
                6: "c", 7: "c", 8: "c", 9: "c", 10: "c",
            }
            _fixed_count = 0
            for _qid, _letter in _QUIZ_ANSWER_LETTERS.items():
                _q = QuizQuestion.query.filter_by(
                    id=_qid, subject_name="إلكترونك", chapter_name="الشبتر السادس"
                ).first()
                if not _q:
                    continue
                _correct = getattr(_q, "option_" + _letter, None)
                if _correct and str(_correct).strip():
                    _correct = str(_correct).strip()
                    if _q.correct_answer != _correct:
                        _q.correct_answer = _correct
                        _fixed_count += 1
            if _fixed_count:
                db.session.commit()
                print(f"تم تصحيح {_fixed_count} من إجابات اختبار إلكترونك (الشبتر السادس)", flush=True)
        except Exception as _fix_err:
            db.session.rollback()
            print(f"تعذّر تصحيح إجابات الاختبار لمرة واحدة: {_fix_err}", flush=True)
    except Exception as e:
        db.session.rollback()
        # لا تبتلع الخطأ بصمت: اطبعه ثم أعد رفعه ليفشل التشغيل بسرعة بدل العمل بجداول ناقصة
        print(f"خطأ فادح عند إنشاء جداول قاعدة البيانات: {e}", flush=True)
        raise

# ==========================================
# Helpers & Auth Decorators
# ==========================================
def get_current_user():
    uid = session.get('student_id')
    if not uid:
        return None
    return Student.query.get(uid)

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role != 'admin':
            return jsonify({"status": "error", "message": "غير مصرح لك (مطلوب مسؤول)"}), 403
        return f(*args, **kwargs)
    return decorated_function

def can_manage_content(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role not in ['admin', 'assistant']:
            return jsonify({"status": "error", "message": "غير مصرح لك بإدارة المحتوى"}), 403
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
# Platform Structured Data
# ==========================================
# تصحيح أسماء سنوات مخزنة بإملاء خاطئ في البيانات (تعرض كما هي في الواجهة)
YEAR_NAME_FIXES = {"السنة الاولئ": "السنة الأولى"}

def get_platform_data(user):
    data = {}
    try:
        query = CustomLesson.query
        if not user or user.role == 'student':
            query = query.filter_by(status='approved')
        elif user.role == 'assistant':
            query = query.filter((CustomLesson.status == 'approved') | (CustomLesson.created_by == user.id))

        lessons = query.order_by(CustomLesson.id.asc()).all()
        quiz_query = QuizQuestion.query
        if not user or user.role == 'student':
            quiz_query = quiz_query.filter_by(status='approved')
        elif user.role == 'assistant':
            quiz_query = quiz_query.filter((QuizQuestion.status == 'approved') | (QuizQuestion.created_by == user.id))
        quiz_groups = quiz_query.with_entities(QuizQuestion.year, QuizQuestion.term, QuizQuestion.subject_name, QuizQuestion.chapter_name).distinct().all()
        for item in [*lessons, *quiz_groups]:
            year = (item.year or '').strip()
            year = YEAR_NAME_FIXES.get(year, year)
            term = (item.term or '').strip()
            subject = (item.subject_name or '').strip()
            chapter = (item.chapter_name or 'عام').strip() or 'عام'
            c_type = (getattr(item, 'content_type', 'quiz') or 'videos').strip()

            if not year or not term or not subject:
                continue

            if year not in data:
                data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]:
                data[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in data[year]["terms"][term]["subjects"]:
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject, "year": year, "term": term,
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

            if not isinstance(item, CustomLesson):
                continue
            lesson_dict = {
                "id": item.id,
                "title": item.title,
                "notes": item.notes or "",
                "status": item.status,
                "custom_id": item.id
            }
            if c_type == "videos":
                lesson_dict["video"] = item.url or ""
                lesson_dict["download"] = item.download_url or ""
            elif c_type == "files":
                lesson_dict["file"] = item.url or item.download_url or ""
                lesson_dict["download"] = item.download_url or ""

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

        return None

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
    return response

# ==========================================
# Main Routes
# ==========================================

@app.route('/')
def home():
    user = get_current_user()
    data = get_platform_data(user)
    catalog_error = data is None
    data = data or {}

    is_admin = (user.role == 'admin') if user else False
    is_assistant = (user.role == 'assistant') if user else False

    try:
        updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
    except Exception:
        db.session.rollback()
        updates = []

    view_context = {
        'catalog_error': catalog_error,
        'updates': updates,
        'is_admin': is_admin,
        'is_assistant': is_assistant,
        'student_id': user.id if user else None,
        'student_name': user.name if user else None,
        'data': data
    }

    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')
    content_type = request.args.get('type')
    chapter_name = request.args.get('chapter')

    if subject_id and content_type and chapter_name:
        for y in data.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id and content_type in s["types"]:
                        for c in s["types"][content_type]["chapters"]:
                            if c["name"] == chapter_name:
                                q_query = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name)
                                if is_admin or is_assistant:
                                    questions = q_query.order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
                                else:
                                    questions = q_query.filter_by(is_answered=True).order_by(LessonQuestion.created_at.desc()).all()
                                return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, **view_context)

    if subject_id and content_type:
        for y in data.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id and content_type in s["types"]:
                        return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], **view_context)

    if subject_id:
        for y in data.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id:
                        return render_template('index.html', view='select_type', subject=s, **view_context)

    if year and term and year in data and term in data[year]["terms"]:
        return render_template('index.html', view='subjects', subjects=data[year]["terms"][term]["subjects"], y_id=year, **view_context)

    if year and year in data:
        return render_template('index.html', view='terms', terms=data[year]["terms"], y_id=year, **view_context)

    return render_template('index.html', view='years', **view_context)

# ==========================================
# Auth APIs (تسجيل الدخول والتسجيل)
# ==========================================

@app.route('/student/login', methods=['POST'])
def student_login():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()

        if not username or not password:
            return jsonify({"status": "error", "message": "يرجى إدخال اسم المستخدم وكلمة المرور"}), 400

        student = Student.query.filter(db.func.lower(Student.username) == username.lower()).first()
        if not student:
            return jsonify({"status": "error", "message": "اسم المستخدم غير موجود"}), 401

        if not student.is_active or not student.check_password(password):
            return jsonify({"status":"error","message":"تعذر تسجيل الدخول بهذه البيانات"}), 401

        session.permanent = True
        session['student_id'] = student.id
        session['student_name'] = student.name
        session['is_admin'] = (student.role == 'admin')
        session['is_assistant'] = (student.role == 'assistant')

        profile = UserProfile.query.filter_by(student_id=student.id).first()
        if not profile:
            profile = UserProfile(student_id=student.id)
            db.session.add(profile)
            db.session.commit()

        return jsonify({
            "status": "success",
            "student": student.to_dict(),
            "role": student.role or 'student'
        })
    except Exception as e:
        app.logger.exception("student_login failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطأ بالسيرفر: {str(e)}"}), 500

@app.route('/student/register', methods=['POST'])
def student_register():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()
        name = str(data.get('name') or '').strip()

        if not username or not password or not name:
            return jsonify({"status": "error", "message": "جميع الحقول مطلوبة"}), 400

        if Student.query.filter(db.func.lower(Student.username) == username.lower()).first():
            return jsonify({"status": "error", "message": "اسم المستخدم مسجل مسبقاً"}), 400

        # إنشاء الطالب بدون تمرير auth_user_id لتفادي أي خطأ في النوع مع Supabase
        student = Student(username=username, name=name, role='student')
        student.set_password(password)
        db.session.add(student)
        db.session.flush()

        profile = UserProfile(student_id=student.id)
        db.session.add(profile)
        db.session.commit()

        return jsonify({"status": "success", "message": "تم التسجيل بنجاح"})
    except Exception as e:
        app.logger.exception("student_register failed")
        db.session.rollback()
        return jsonify({"status": "error", "message": "تعذر إنشاء الحساب. حاول مرة أخرى."}), 500

@app.route('/student/logout', methods=['POST'])
def student_logout():
    session.clear()
    return jsonify({"status": "success"})

# ==========================================
# نظام إدارة المحتوى والمراجعة (Workflow)
# ==========================================

def _safe_str(value, default=""):
    """نص آمن: يعيد القيمة الافتراضية عند None، وإلا النص بعد إزالة المسافات."""
    if value is None:
        return default
    return str(value).strip()

@app.route('/admin/add-content', methods=['POST'])
@can_manage_content
def add_custom_content():
    try:
        user = get_current_user()
        data = request.get_json(silent=True) or request.form or {}

        status = 'approved' if user.role == 'admin' else 'pending_review'

        year = _safe_str(data.get('year'))
        term = _safe_str(data.get('term'))
        subject_name = _safe_str(data.get('subject_name'))
        title = _safe_str(data.get('title'))
        content_type = _safe_str(data.get('content_type'), 'videos')

        if not year or not term or not subject_name or not title:
            return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
        if content_type not in ('videos', 'files', 'quiz'):
            return jsonify({"status": "error", "message": "نوع المحتوى غير صالح (يجب أن يكون videos أو files أو quiz)"}), 400

        lesson = CustomLesson(
            year=year,
            term=term,
            college=_safe_str(data.get('college')),
            subject_name=subject_name,
            chapter_name=_safe_str(data.get('chapter_name'), 'عام') or 'عام',
            content_type=content_type,
            title=title,
            url=_safe_str(data.get('url')),
            download_url=_safe_str(data.get('download_url')),
            notes=_safe_str(data.get('notes')),
            status=status,
            created_by=user.id
        )
        db.session.add(lesson)
        db.session.commit()

        msg = "تم نشر المحتوى بنجاح!" if status == 'approved' else "تم رفع المحتوى وإرساله لطابور المراجعة."
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/custom-content', methods=['GET'])
@can_manage_content
def get_custom_content():
    user = get_current_user()
    try:
        if user.role == 'admin':
            lessons = CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()
        else:
            lessons = CustomLesson.query.filter((CustomLesson.created_by == user.id) | (CustomLesson.status == 'approved')).order_by(CustomLesson.created_at.desc()).all()
        return jsonify([l.to_dict() for l in lessons])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/edit-content/<int:content_id>', methods=['POST'])
@can_manage_content
def edit_custom_content(content_id):
    try:
        user = get_current_user()
        item = CustomLesson.query.get_or_404(content_id)

        if user.role != 'admin' and item.created_by != user.id:
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل محتوى الآخرين"}), 403

        data = request.get_json(silent=True) or request.form or {}
        item.year = _safe_str(data.get('year'), item.year)
        item.term = _safe_str(data.get('term'), item.term)
        item.subject_name = _safe_str(data.get('subject_name'), item.subject_name)
        item.chapter_name = _safe_str(data.get('chapter_name'), item.chapter_name)
        item.content_type = _safe_str(data.get('content_type'), item.content_type)
        item.title = _safe_str(data.get('title'), item.title)
        item.url = _safe_str(data.get('url'), item.url)
        item.download_url = _safe_str(data.get('download_url'), item.download_url)
        item.notes = _safe_str(data.get('notes'), item.notes)

        if not item.year or not item.term or not item.subject_name or not item.title:
            return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
        if item.content_type not in ('videos', 'files', 'quiz'):
            return jsonify({"status": "error", "message": "نوع المحتوى غير صالح (يجب أن يكون videos أو files أو quiz)"}), 400

        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث المحتوى بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-content/<int:content_id>', methods=['POST'])
@admin_required
def delete_custom_content(content_id):
    try:
        item = CustomLesson.query.get_or_404(content_id)
        # حذف المرتبطات أولاً لتفادي مخالفة المفتاح الأجنبي، وفصل المهام عن الدرس المحذوف
        Favorite.query.filter_by(lesson_id=content_id).delete(synchronize_session=False)
        Progress.query.filter_by(lesson_id=content_id).delete(synchronize_session=False)
        UnifiedTask.query.filter_by(lesson_id=content_id).update({"lesson_id": None}, synchronize_session=False)
        db.session.delete(item)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/review-queue', methods=['GET'])
@admin_required
def get_review_queue():
    try:
        lessons = CustomLesson.query.filter_by(status='pending_review').all()
        quizzes = QuizQuestion.query.filter_by(status='pending_review').all()
        return jsonify({
            "lessons": [l.to_dict() for l in lessons],
            "quizzes": [q.to_dict() for q in quizzes]
        })
    except Exception:
        db.session.rollback()
        return jsonify({"lessons": [], "quizzes": []})

@app.route('/admin/approve-content/<string:content_type>/<int:item_id>', methods=['POST'])
@admin_required
def approve_content(content_type, item_id):
    try:
        data = request.get_json(silent=True) or {}
        action = data.get('action', 'approve')
        new_status = 'approved' if action == 'approve' else 'rejected'

        if content_type == 'lesson':
            item = CustomLesson.query.get_or_404(item_id)
        else:
            item = QuizQuestion.query.get_or_404(item_id)

        item.status = new_status
        db.session.commit()
        return jsonify({"status": "success", "message": f"تم {'الاعتماد' if action == 'approve' else 'الرفض'}"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# نظام بنك الأسئلة والاختبارات وفصل XP
# ==========================================

@app.route('/api/quiz', methods=['GET'])
def get_quiz():
    chapter_name = request.args.get('chapter')
    subject_name = request.args.get('subject')
    user = get_current_user()

    try:
        query = QuizQuestion.query
        if chapter_name:
            query = query.filter_by(chapter_name=chapter_name)
        # لا نفلتر بالسنة/الترم: نصوصهما قد تختلف بين صفوف الدروس والأسئلة
        # (مثلاً خطأ إملائي في "السنة الاولئ") فيعيد الفلتر صفر سؤال رغم وجود أسئلة.
        if subject_name:
            query = query.filter_by(subject_name=subject_name)

        if not user or user.role == 'student':
            query = query.filter_by(status='approved')

        questions = query.all()
        return jsonify([q.to_dict() for q in questions])
    except Exception as e:
        db.session.rollback()
        return jsonify([])

@app.route('/api/quiz/attempt', methods=['POST'])
@student_required
def record_quiz_attempt():
    user = get_current_user()
    data = request.get_json(silent=True) or {}

    try:
        score = int(data.get('score', 0))
        total = int(data.get('total_questions', 1))
        perc = float(data.get('percentage', 0))
        subject = str(data.get('subject_name', 'مادة عامة')).strip()
        chapter = str(data.get('chapter_name', 'عام')).strip()

        attempt = QuizAttempt(
            student_id=user.id,
            subject_name=subject,
            chapter_name=chapter,
            score=score,
            total_questions=total,
            percentage=perc
        )
        db.session.add(attempt)
        db.session.commit()

        # احتساب XP مستقل (يمنح مرة واحدة فقط عند اجتياز الفصل بنجاح)
        profile = UserProfile.query.filter_by(student_id=user.id).first()
        if not profile:
            profile = UserProfile(student_id=user.id)
            db.session.add(profile)
            db.session.commit()
        xp_granted = False
        if perc >= 50 and profile:
            reason = f"اجتياز اختبار: {chapter}"
            already_rewarded = PointTransaction.query.filter_by(student_id=user.id, reason=reason).first()
            if not already_rewarded:
                profile.add_points(20, reason)
                attempt.xp_awarded = 20
                db.session.commit()
                xp_granted = True

        return jsonify({"status": "success", "attempt_id": attempt.id, "xp_awarded": xp_granted})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/import-questions', methods=['POST'])
@can_manage_content
def import_questions():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "يرجى تحديد ملف CSV"}), 400

    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({"status": "error", "message": "يجب أن يكون الملف بصيغة CSV"}), 400

    user = get_current_user()
    status = 'approved' if user.role == 'admin' else 'pending_review'

    try:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)
        count = 0
        for row in reader:
            q = QuizQuestion(
                year=row.get('year', 'السنة الأولى'),
                term=row.get('term', 'الترم الأول'),
                college=row.get('college', ''),
                subject_name=row.get('subject_name', 'عام'),
                chapter_name=row.get('chapter_name', 'عام'),
                question_text=row.get('question_text', '').strip(),
                option_a=row.get('option_a', '').strip(),
                option_b=row.get('option_b', '').strip(),
                option_c=row.get('option_c', '').strip(),
                option_d=row.get('option_d', '').strip(),
                correct_answer=row.get('correct_answer', '').strip(),
                explanation=row.get('explanation', '').strip(),
                status=status,
                created_by=user.id
            )
            if q.question_text and q.correct_answer:
                db.session.add(q)
                count += 1
        db.session.commit()
        return jsonify({"status": "success", "message": f"تم استيراد {count} سؤال بنجاح كـ ({status})"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطأ في قراءة الملف: {str(e)}"}), 500

# ==========================================
# استفسارات الطلاب (Lesson Questions)
# ==========================================

@app.route('/ask-question', methods=['POST'])
def submit_question():
    try:
        data = request.get_json(silent=True) or request.form or {}
        user = get_current_user()

        subject_id = data.get('subject_id')
        chapter_name = data.get('chapter_name')
        student_name = (data.get('student_name') or (user.name if user else 'طالب')).strip()
        question_text = (data.get('question_text') or '').strip()

        if not question_text or not subject_id or not chapter_name:
            return jsonify({"status": "error", "message": "بيانات غير مكتملة"}), 400

        q = LessonQuestion(
            subject_id=subject_id,
            chapter_name=chapter_name,
            student_name=student_name,
            student_id=user.id if user else None,
            question_text=question_text
        )
        db.session.add(q)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم إرسال السؤال بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/questions', methods=['GET'])
@can_manage_content
def get_pending_questions():
    try:
        return jsonify([q.to_dict() for q in LessonQuestion.query.filter_by(is_answered=False).order_by(LessonQuestion.created_at.desc()).all()])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/reply/<int:question_id>', methods=['POST'])
@can_manage_content
def reply_question(question_id):
    try:
        user = get_current_user()
        q = LessonQuestion.query.get_or_404(question_id)
        data = request.get_json(silent=True) or request.form or {}
        ans = (data.get('answer_text') or '').strip()

        if not ans:
            return jsonify({"status": "error", "message": "الرد مطلوب"}), 400

        q.answer_text = ans
        q.is_answered = True
        q.answered_by = user.id
        q.answered_at = datetime.utcnow()
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر الرد بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-question/<int:question_id>', methods=['POST'])
@admin_required
def delete_question(question_id):
    try:
        q = LessonQuestion.query.get_or_404(question_id)
        db.session.delete(q)
        db.session.commit()
        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# المساعد الذكي وتقديرات الدرجات
# ==========================================

def calculate_grade_estimation(score):
    """تقدير الدرجة: يقبل مقياس 0-100 أو 0-4، ويعيد التقدير كنص عربي مع الحرف."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise ValueError("الدرجة يجب أن تكون رقماً")
    if value <= 4:
        # مقياس 0-4 (معدل)
        if value >= 3.5:
            return "ممتاز (A)"
        if value >= 3.0:
            return "جيد جداً (B)"
        if value >= 2.0:
            return "جيد (C)"
        if value >= 1.0:
            return "مقبول (D)"
        return "راسب (F)"
    if value >= 90:
        return "ممتاز (A)"
    if value >= 80:
        return "جيد جداً (B)"
    if value >= 70:
        return "جيد (C)"
    if value >= 60:
        return "مقبول (D)"
    return "راسب (F)"

@app.route('/calculate-grade', methods=['POST'])
def calculate_grade():
    try:
        data = request.get_json(silent=True) or request.form or {}
        score = data.get('score')
        if score is None:
            return jsonify({"status": "error", "message": "يرجى إدخال الدرجة"}), 400

        try:
            numeric_score = float(score)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "الدرجة يجب أن تكون رقماً"}), 400
        if numeric_score < 0 or numeric_score > 100:
            return jsonify({"status": "error", "message": "الدرجة يجب أن تكون بين 0 و 100"}), 400

        grade = calculate_grade_estimation(numeric_score)
        return jsonify({"status": "success", "score": numeric_score, "grade": grade})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        user_message = data.get('message') or request.form.get('message') or ''
        user_message = str(user_message).strip()

        if not user_message:
            file = request.files.get('file')
            user_message = "اشرح هذا المحتوى بالتفصيل خطوة بخطوة" if file else "مرحباً بك"

        messages = [
            {
                "role": "system",
                "content": "أنت المساعد الأكاديمي الذكي لمنصة عمرو أكاديمي. قدّم إجابات علمية وهندسية دقيقة وبسيطة وباللغة العربية خطوة بخطوة."
            },
            {"role": "user", "content": user_message}
        ]

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=messages,
            temperature=0.5
        )

        reply = response.choices[0].message.content
        return jsonify({"reply": reply})
    except Exception as e:
        return jsonify({"reply": f"عذراً، تعذر الرد حالياً: {str(e)}"}), 500

# ==========================================
# نظام التخطيط الدراسي والذكاء الاصطناعي الكامل
# ==========================================

@app.route('/simulators')
def simulators():
    return render_template('simulators.html', student_id=session.get('student_id'))

@app.route('/planner')
def planner():
    user = get_current_user()
    return render_template('planner.html', student_id=user.id if user else None, student_name=user.name if user else None)

@app.route('/generate_plan', methods=['POST'])
def generate_plan():
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
        
        if not student_id:
            return jsonify({"error": "يرجى تسجيل الدخول أولاً"}), 401
        
        discipline = (data.get('discipline') or 'متوازن').strip()
        wird_pages = data.get('wird_pages', 1)
        wird_enabled = data.get('wird', True)
        
        base_profiles = {
            "مرن": "1 to 2 hours total per day. Maximum 1-2 small study tasks per active day.",
            "متوازن": "2 to 3.5 hours total per day. Maximum 2 moderate tasks per active day.",
            "مكثف": "4 to 5.5 hours total per day. Maximum 3 focused tasks per active day."
        }
        
        limit_desc = base_profiles.get(discipline, base_profiles["متوازن"])
        wird_text = f"Daily Wird: {wird_pages} pages every day (0.5 hour allocated)." if wird_enabled and wird_pages > 0 else "No daily Wird."

        system_instruction = (
            "You are a realistic academic study planner. DO NOT overwhelm the student.\n"
            + f"Workload Limit: {limit_desc}\n"
            + f"{wird_text}\n"
            + "STRICT RULES:\n"
            + "1. Output ONLY valid JSON: {'plans': [{'day_name': 'السبت', 'date': 'YYYY-MM-DD', 'is_off_day': false, 'tasks': [{'title': 'مهمة واقعية ومحددة', 'allocated_hours': 1.5}]}]}\n"
            + "2. Total daily study hours must NEVER exceed the limit specified above.\n"
            + "3. Only give 1 or 2 high-value distinct subjects per day. Do not repeat identical task names over and over.\n"
            + "4. Off days must have 'is_off_day': true and 'tasks': []."
        )

        user_prompt = (
            f"Start Date: {data.get('current_date')}\n"
            + f"Target Date: {data.get('target_date')}\n"
            + f"Subjects: {data.get('subjects')}\n"
            + f"Mode: {discipline}\n"
            + f"Weekly Off-Days: {', '.join(data.get('off_days', []))}\n"
            + f"Quran Wird: {'Yes, ' + str(wird_pages) + ' pages' if wird_enabled and wird_pages > 0 else 'No'}"
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

        plan_data = json.loads(response.choices[0].message.content)

        if student_id and plan_data.get('plans'):
            try:
                UnifiedTask.query.filter_by(student_id=student_id, source='planner').delete()
                
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
                        allocated_hours = float(task.get('allocated_hours', 1.0))
                        is_wird = 'الورد' in task_title or 'قراءة' in task_title
                        
                        unified_task = UnifiedTask(
                            student_id=student_id,
                            lesson_id=None,
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
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
    try:
        data = request.get_json(silent=True) or {}
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "يرجى تسجيل الدخول أولاً"}), 401
            
        missed = data.get('missed_tasks', [])
        remaining = data.get('remaining_plans', [])

        system_instruction = (
            "You are an academic rescheduling assistant. Re-distribute uncompleted tasks smartly into the remaining days without overloading the student. Max 2-3 hours daily."
        )

        user_prompt = (
            f"Missed Tasks: {json.dumps(missed, ensure_ascii=False)}\n"
            + f"Remaining Schedule: {json.dumps(remaining, ensure_ascii=False)}"
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

@app.route('/api/tasks', methods=['GET', 'POST'])
@student_required
def manage_unified_tasks():
    student_id = session.get('student_id')
    try:
        if request.method == 'GET':
            source = request.args.get('source')
            query = UnifiedTask.query.filter_by(student_id=student_id)
            if source:
                query = query.filter_by(source=source)
            tasks = query.order_by(UnifiedTask.planned_date).all()
            return jsonify([t.to_dict() for t in tasks])

        data = request.get_json(silent=True) or {}
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

@app.route('/api/tasks/<int:task_id>', methods=['PUT', 'DELETE'])
@student_required
def handle_single_task(task_id):
    student_id = session['student_id']
    task = UnifiedTask.query.get(task_id)
    if not task:
        return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
    if task.student_id != student_id:
        return jsonify({"status": "error", "message": "غير مصرح لك"}), 403

    try:
        if request.method == 'DELETE':
            db.session.delete(task)
            db.session.commit()
            return jsonify({"status": "success", "message": "تم حذف المهمة"})

        data = request.get_json(silent=True) or {}
        if 'completed' in data:
            is_completed = bool(data['completed'])
            if is_completed and not task.completed:
                profile = UserProfile.query.filter_by(student_id=student_id).first()
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

@app.route('/api/lessons', methods=['GET'])
@student_required
def get_all_lessons():
    try:
        lessons = CustomLesson.query.filter_by(status='approved').order_by(CustomLesson.subject_name, CustomLesson.chapter_name).all()
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
    if groq_client is None:
        return jsonify({"error": "خدمة الذكاء الاصطناعي غير مهيأة حاليًا"}), 503
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

        lessons_info = [{"id": l.id, "title": l.title, "type": l.content_type} for l in lessons]
        today = datetime.now().date()
        delta = (exam_date_obj - today).days
        if delta < 1:
            return jsonify({"status": "error", "message": "تاريخ الاختبار يجب أن يكون في المستقبل"}), 400

        prompt = f"""
        أنت مخطط أكاديمي ذكي. وزع الدروس التالية على {delta} يوماً باعتدال وبدون إرهاق (1-2 درس يومياً بحد أقصى ساعة ونصف يومياً).
        الدروس: {json.dumps(lessons_info, ensure_ascii=False)}
        أخرج فقط JSON: {{"plan": [{{"day": "2026-08-20", "tasks": [{{"lesson_id": 1, "title": "مقدمة", "priority": "متوسط", "allocated_hours": 1.0}}]}}]}}
        """

        response = groq_client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": "أنت مخطط أكاديمي. أخرج فقط JSON."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            temperature=0.4
        )

        plan_data = json.loads(response.choices[0].message.content)
        priority_map = {"عال": 1, "متوسط": 2, "منخفض": 3}

        for day_obj in plan_data.get('plan', []):
            day_date_str = day_obj.get('day')
            if not day_date_str: continue
            try: day_date = datetime.strptime(day_date_str, '%Y-%m-%d').date()
            except ValueError: continue

            for task in day_obj.get('tasks', []):
                lesson_id = task.get('lesson_id')
                lesson = CustomLesson.query.get(lesson_id) if lesson_id else None
                unified_task = UnifiedTask(
                    student_id=student_id,
                    lesson_id=lesson_id,
                    title=task.get('title', lesson.title if lesson else 'مراجعة'),
                    description=f"خطة اختبار: {plan_name}",
                    planned_date=day_date,
                    source='exam',
                    priority=priority_map.get(task.get('priority', 'متوسط'), 2),
                    allocated_hours=float(task.get('allocated_hours', 1.0)),
                    is_wird=False,
                    completed=False
                )
                db.session.add(unified_task)
        db.session.commit()

        return jsonify({"status": "success", "plan": plan_data})

    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# الملف الشخصي ولوحة المتصدرين والتقدم
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

        # الدروس المكتملة تُحتسب ضمن المهام: إكمال درس لا ينشئ UnifiedTask،
        # فكان عدّاد «مهام مكتملة» ونسبة الإنجاز يبقيان صفراً رغم إكمال الدروس.
        completed_lessons = Progress.query.filter_by(student_id=student_id, progress_percent=100).count()
        total_lessons = Progress.query.filter_by(student_id=student_id).count()
        completed_tasks = UnifiedTask.query.filter_by(student_id=student_id, completed=True).count() + completed_lessons
        total_tasks = UnifiedTask.query.filter_by(student_id=student_id).count() + total_lessons
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
        data = request.get_json(silent=True) or {}
        show = data.get('show', True)
        profile = UserProfile.query.filter_by(student_id=session['student_id']).first()
        if profile:
            profile.show_in_leaderboard = bool(show)
            db.session.commit()
        return jsonify({"status": "success"})
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
        try:
            lesson_id = int(lesson_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "معرف الدرس غير صالح"}), 400

        fav = Favorite.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()

        if request.method == 'POST':
            if not fav:
                db.session.add(Favorite(student_id=student_id, lesson_id=lesson_id))
                db.session.commit()
            return jsonify({"status": "success"})

        elif request.method == 'DELETE':
            if fav:
                db.session.delete(fav)
                db.session.commit()
            return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/progress', methods=['GET', 'POST'])
@student_required
def manage_progress():
    student_id = session['student_id']
    try:
        if request.method == 'GET':
            return jsonify([p.to_dict() for p in Progress.query.filter_by(student_id=student_id).all()])

        data = request.get_json(silent=True) or {}
        lesson_id = data.get('lesson_id')
        status = data.get('status', 'not_started')
        progress_percent = data.get('progress_percent', 0)
        try:
            lesson_id = int(lesson_id)
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "معرف الدرس غير صالح"}), 400

        if type(progress_percent) is not int or not 0 <= progress_percent <= 100 or not db.session.get(CustomLesson, lesson_id):
            return jsonify({"status":"error","message":"بيانات الدرس غير صالحة"}), 400
        prog = Progress.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()
        if not prog:
            prog = Progress(student_id=student_id, lesson_id=lesson_id)
            db.session.add(prog)

        old_progress = prog.progress_percent or 0
        prog.status = status
        prog.progress_percent = progress_percent
        prog.last_accessed = datetime.utcnow()
        db.session.commit()

        if prog.progress_percent == 100 and old_progress < 100:
            profile = UserProfile.query.filter_by(student_id=student_id).first()
            if profile:
                lesson = CustomLesson.query.get(lesson_id)
                lesson_title = lesson.title if lesson else "درس"
                profile.add_points(10, f"إكمال الدرس: {lesson_title}")

        return jsonify({"status": "success"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/updates', methods=['GET'])
def get_platform_updates():
    try:
        return jsonify([u.to_dict() for u in PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-update', methods=['POST'])
@admin_required
def add_platform_update():
    try:
        data = request.get_json(silent=True) or request.form or {}
        title = data.get('title', '').strip()
        tag = data.get('tag', 'هام').strip()
        description = data.get('description', '').strip()

        if not title or not description:
            return jsonify({"status": "error", "message": "العنوان والتفاصيل مطلوبة"}), 400

        db.session.add(PlatformUpdate(title=title, tag=tag, description=description))
        db.session.commit()
        return jsonify({"status": "success", "message": "تم نشر الإعلان بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health')
def health_check():
    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db.session.rollback()
        error = getattr(exc, "orig", exc)
        causes = [error]
        while causes[-1].__cause__ and causes[-1].__cause__ not in causes:
            causes.append(causes[-1].__cause__)
        tls_error = next((cause for cause in causes if isinstance(cause, ssl.SSLCertVerificationError)), None)
        details = error.args[0] if error.args else None
        sqlstate = details.get("C", "") if isinstance(details, dict) else ""
        sqlstate = sqlstate if isinstance(sqlstate, str) and len(sqlstate) == 5 and sqlstate.isalnum() else "-"
        app.logger.warning(
            "DB health failed: driver=%s errors=%s sqlstate=%s tls_verify_code=%s",
            db.engine.url.drivername,
            ",".join(type(cause).__name__ for cause in causes),
            sqlstate,
            tls_error.verify_code if tls_error else "-",
        )
        return jsonify(status="unavailable", database="disconnected"), 503
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "database": "connected" if db.engine else "disconnected"
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)




