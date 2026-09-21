import os
import io
import csv
import json
import ssl
from datetime import datetime, timedelta
from functools import wraps
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.sql import text
from werkzeug.security import generate_password_hash, check_password_hash
import math
from groq import Groq

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'amr_suhail_academy_secure_key_2026')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=14)

# ==========================================
# Database Connection Setup
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

db = SQLAlchemy(app)

# إعداد المساعد الذكي
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "openai/gpt-oss-120b"

# ==========================================
# Database Models
# ==========================================

class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    auth_user_id = db.Column(db.String(50), unique=True, nullable=True)
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

        # حماية ضد التكرار السريع لنفس العملية
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
        self.last_active = today

        db.session.commit()
        return {"level_up": self.level > old_level, "new_level": self.level, "new_points": self.points}

    def to_dict(self):
        return {
            "student_id": self.student_id,
            "level": self.level,
            "points": self.points,
            "total_points": self.total_points,
            "streak_days": self.streak_days,
            "show_in_leaderboard": self.show_in_leaderboard,
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

    def to_dict(self):
        return {
            "id": self.id,
            "points": self.points,
            "reason": self.reason,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
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
    status = db.Column(db.String(20), default='approved')  # draft, pending_review, approved, rejected
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
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
            "created_by": self.created_by
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
    correct_answer = db.Column(db.String(100), nullable=True)
    explanation = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved')
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "year": self.year,
            "term": self.term,
            "college": self.college or "",
            "subject_name": self.subject_name,
            "chapter_name": self.chapter_name,
            "question_text": self.question_text,
            "option_a": self.option_a or "",
            "option_b": self.option_b or "",
            "option_c": self.option_c or "",
            "option_d": self.option_d or "",
            "correct_answer": self.correct_answer or "",
            "explanation": self.explanation or "",
            "status": self.status or 'approved'
        }

class QuizAttempt(db.Model):
    __tablename__ = 'quiz_attempts'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    subject_name = db.Column(db.String(150), nullable=False)
    chapter_name = db.Column(db.String(150), nullable=True)
    exam_plan_id = db.Column(db.Integer, nullable=True)
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

class Progress(db.Model):
    __tablename__ = 'progress'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    status = db.Column(db.String(20), default='not_started')
    progress_percent = db.Column(db.Integer, default=0)
    last_accessed = db.Column(db.DateTime, default=datetime.utcnow)

    lesson = db.relationship('CustomLesson')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "status": self.status,
            "progress_percent": self.progress_percent,
            "last_accessed": self.last_accessed.strftime("%Y-%m-%d %H:%M") if self.last_accessed else ""
        }

class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)

    lesson = db.relationship('CustomLesson')

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "lesson_id": self.lesson_id,
            "lesson": self.lesson.to_dict() if self.lesson else None,
            "added_at": self.added_at.strftime("%Y-%m-%d %H:%M") if self.added_at else ""
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
        return {
            "id": self.id,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "student_name": self.student_name,
            "question_text": self.question_text,
            "answer_text": self.answer_text,
            "is_answered": self.is_answered,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class PlatformUpdate(db.Model):
    __tablename__ = 'platform_updates'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    tag = db.Column(db.String(50), default="إضافة جديدة")
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "tag": self.tag,
            "description": self.description,
            "date": self.created_at.strftime("%Y-%m-%d") if self.created_at else ""
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

    def to_dict(self):
        return {
            "id": self.id,
            "student_id": self.student_id,
            "title": self.title,
            "planned_date": self.planned_date.strftime("%Y-%m-%d") if self.planned_date else "",
            "completed": self.completed
        }

# ==========================================
# Helpers & Role Decorators
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
            return jsonify({"status": "error", "message": "غير مصرح لك (مطلوب صلاحية مسؤول)"}), 403
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
# Structured Academic Platform Data
# ==========================================
def get_platform_data(user):
    data = {}
    try:
        query = CustomLesson.query
        if not user or user.role == 'student':
            query = query.filter_by(status='approved')
        elif user.role == 'assistant':
            query = query.filter((CustomLesson.status == 'approved') | (CustomLesson.created_by == user.id))
        
        lessons = query.order_by(CustomLesson.id.asc()).all()

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
                "status": item.status
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
    except Exception:
        db.session.rollback()

    return data

@app.after_request
def add_headers(response):
    if request.path.startswith('/') and not request.path.startswith('/static'):
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

# ==========================================
# Routes
# ==========================================

@app.route('/')
def home():
    user = get_current_user()
    data = get_platform_data(user)

    is_admin = (user.role == 'admin') if user else False
    is_assistant = (user.role == 'assistant') if user else False

    try:
        updates = PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all()
    except Exception:
        db.session.rollback()
        updates = []

    view_context = {
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
# Auth APIs (مرن ومتوافق تماماً مع Supabase)
# ==========================================

@app.route('/student/login', methods=['POST'])
def student_login():
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = str(data.get('username') or '').strip()
        password = str(data.get('password') or '').strip()

        if not username or not password:
            return jsonify({"status": "error", "message": "يرجى إدخال اسم المستخدم وكلمة المرور"}), 400

        student = Student.query.filter(Student.username.ilike(username)).first()
        if not student:
            return jsonify({"status": "error", "message": "اسم المستخدم غير موجود"}), 401

        # التحقق من كلمة المرور (تشفير أو نص عادي للحسابات السابقة)
        is_valid = False
        try:
            is_valid = student.check_password(password)
        except Exception:
            is_valid = (student.password_hash == password)

        if not is_valid and student.password_hash == password:
            is_valid = True

        if not is_valid:
            return jsonify({"status": "error", "message": "كلمة المرور غير صحيحة"}), 401

        session.permanent = True
        session['student_id'] = student.id
        session['student_name'] = student.name
        session['is_admin'] = (student.role == 'admin')
        session['is_assistant'] = (student.role == 'assistant')

        # إنشاء بروفايل في حال عدم وجوده
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

        if Student.query.filter(Student.username.ilike(username)).first():
            return jsonify({"status": "error", "message": "اسم المستخدم مسجل مسبقاً"}), 400

        student = Student(username=username, name=name, role='student')
        student.set_password(password)
        db.session.add(student)
        db.session.commit()

        profile = UserProfile(student_id=student.id)
        db.session.add(profile)
        db.session.commit()

        return jsonify({"status": "success", "message": "تم التسجيل بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/student/logout', methods=['POST'])
def student_logout():
    session.clear()
    return jsonify({"status": "success"})

# ==========================================
# نظام إدارة المحتوى والمراجعة (Review Workflow)
# ==========================================

@app.route('/admin/add-content', methods=['POST'])
@can_manage_content
def add_custom_content():
    try:
        user = get_current_user()
        data = request.get_json(silent=True) or request.form or {}

        status = 'approved' if user.role == 'admin' else 'pending_review'

        lesson = CustomLesson(
            year=data.get('year', '').strip(),
            term=data.get('term', '').strip(),
            college=data.get('college', '').strip(),
            subject_name=data.get('subject_name', '').strip(),
            chapter_name=data.get('chapter_name', 'عام').strip() or 'عام',
            content_type=data.get('content_type', 'videos').strip(),
            title=data.get('title', '').strip(),
            url=data.get('url', '').strip(),
            download_url=data.get('download_url', '').strip(),
            notes=data.get('notes', '').strip(),
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
        item.year = data.get('year', item.year).strip()
        item.term = data.get('term', item.term).strip()
        item.subject_name = data.get('subject_name', item.subject_name).strip()
        item.chapter_name = data.get('chapter_name', item.chapter_name).strip()
        item.content_type = data.get('content_type', item.content_type).strip()
        item.title = data.get('title', item.title).strip()
        item.url = data.get('url', item.url).strip()
        item.download_url = data.get('download_url', item.download_url).strip()
        item.notes = data.get('notes', item.notes).strip()

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
# المساعد الذكي، الملف الشخصي، التقدم
# ==========================================

@app.route('/ask-ai', methods=['POST'])
def ask_ai():
    try:
        data = request.get_json(silent=True) or {}
        user_message = data.get('message') or request.form.get('message') or ''
        user_message = str(user_message).strip()

        if not user_message:
            file = request.files.get('file')
            user_message = "اشرح هذا المحتوى الأكاديمي بالتفصيل" if file else "مرحباً بك"

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
                    "points": p.points
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

        prog = Progress.query.filter_by(student_id=student_id, lesson_id=lesson_id).first()
        if not prog:
            prog = Progress(student_id=student_id, lesson_id=lesson_id)
            db.session.add(prog)

        prog.status = status
        prog.progress_percent = progress_percent
        prog.last_accessed = datetime.utcnow()
        db.session.commit()
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
        return jsonify({"status": "success", "message": "تم نشر الإعلان"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/planner')
def planner():
    user = get_current_user()
    return render_template('planner.html', student_id=user.id if user else None, student_name=user.name if user else None)

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
