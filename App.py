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
app.secret_key = os.environ.get('SECRET_KEY', 'amr_suhail_academy_secure_secret_key_2026')
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

# ==========================================
# Database Setup (Supabase / Postgres compatible)
# ==========================================
raw_db_url = os.environ.get('DATABASE_URL', 'sqlite:///academy_qa.db').strip()
if raw_db_url.startswith("postgres://"):
    db_url = raw_db_url.replace("postgres://", "postgresql+pg8000://", 1)
elif raw_db_url.startswith("postgresql://"):
    db_url = raw_db_url.replace("postgresql://", "postgresql+pg8000://", 1)
else:
    db_url = raw_db_url
db_url = db_url.replace("postgresql+psycopg2://", "postgresql+pg8000://")
if "?" in db_url: db_url = db_url.split("?")[0]

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

if "sqlite" not in db_url:
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "connect_args": {"ssl_context": ssl_context}
    }

db = SQLAlchemy(app)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "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")
groq_client = Groq(api_key=GROQ_API_KEY)
GROQ_MODEL = "openai/gpt-oss-120b"

# ==========================================
# Database Models
# ==========================================
class Student(db.Model):
    __tablename__ = 'students'
    id = db.Column(db.Integer, primary_key=True)
    auth_user_id = db.Column(db.String(50), unique=True, nullable=True) # Supabase Auth UUID
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(20), default='student') # student, assistant, admin
    college = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    profile = db.relationship('UserProfile', backref='student', uselist=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    def to_dict(self):
        return {"id": self.id, "username": self.username, "name": self.name, "role": self.role, "college": self.college}

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
    
    def calculate_level(self):
        if self.points < 0: self.points = 0
        new_level = math.floor(self.points / 100) + 1
        return max(new_level, 1)

    def add_points(self, points, reason="نشاط عام"):
        if points <= 0: return {"level_up": False, "new_level": self.level, "new_points": self.points}
        
        # Prevent exact duplicate transactions in a short time frame for safety
        recent = PointTransaction.query.filter_by(student_id=self.student_id, reason=reason).first()
        if recent and "محاولة اختبار" not in reason: 
            return {"level_up": False, "new_level": self.level, "new_points": self.points}

        self.points += points
        self.total_points += points
        old_level = self.level
        self.level = self.calculate_level()
        
        transaction = PointTransaction(
            student_id=self.student_id, points=points, reason=reason,
            level_before=old_level, level_after=self.level
        )
        db.session.add(transaction)
        
        today = datetime.utcnow().date()
        if self.last_active == today - timedelta(days=1): self.streak_days += 1
        elif self.last_active != today: self.streak_days = 1
        self.last_active = today
        db.session.commit()
        return {"level_up": self.level > old_level, "new_level": self.level, "new_points": self.points}
        
    def to_dict(self):
        return {
            "student_id": self.student_id, "level": self.level, "points": self.points,
            "total_points": self.total_points, "streak_days": self.streak_days,
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
    status = db.Column(db.String(20), default='approved') # draft, pending_review, approved, rejected
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "year": self.year, "term": self.term, "college": self.college,
            "subject_name": self.subject_name, "chapter_name": self.chapter_name,
            "content_type": self.content_type, "title": self.title, "url": self.url or "",
            "download_url": self.download_url or "", "notes": self.notes or "",
            "status": self.status, "created_by": self.created_by
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
    correct_answer = db.Column(db.String(50), nullable=True)
    explanation = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved') # draft, pending_review, approved, rejected
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id, "subject_name": self.subject_name, "chapter_name": self.chapter_name,
            "question_text": self.question_text, "option_a": self.option_a or "",
            "option_b": self.option_b or "", "option_c": self.option_c or "",
            "option_d": self.option_d or "", "correct_answer": self.correct_answer or "",
            "explanation": self.explanation or "", "status": self.status
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# (Keep UnifiedTask, Favorite, Progress, PlatformUpdate models exactly as they were in your code)
# ... [أبقيت بقية الجداول مثل Progress وغيرها كما هي للاختصار]
class Progress(db.Model):
    __tablename__ = 'progress'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    status = db.Column(db.String(20), default='not_started')
    progress_percent = db.Column(db.Integer, default=0)
    last_accessed = db.Column(db.DateTime, default=datetime.utcnow)

class Favorite(db.Model):
    __tablename__ = 'favorites'
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=False)
    lesson_id = db.Column(db.Integer, db.ForeignKey('custom_lessons.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    lesson = db.relationship('CustomLesson')

class PlatformUpdate(db.Model):
    __tablename__ = 'platform_updates'
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    tag = db.Column(db.String(50), default="ميزة جديدة")
    description = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    def to_dict(self): return {"id": self.id, "title": self.title, "tag": self.tag, "description": self.description}

with app.app_context():
    try:
        db.create_all()
    except Exception as e:
        db.session.rollback()

# ==========================================
# Auth & Roles Decorators
# ==========================================
def current_user():
    user_id = session.get('student_id')
    return Student.query.get(user_id) if user_id else None

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = current_user()
        if not user or user.role != 'admin':
            return jsonify({"status": "error", "message": "غير مصرح لك بالوصول"}), 403
        return f(*args, **kwargs)
    return decorated_function

def can_manage_content(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = current_user()
        if not user or user.role not in ['admin', 'assistant']:
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
# Data Fetcher (Filtered by Status)
# ==========================================
def get_platform_data(user):
    data = {}
    try:
        # Students only see approved content. Assistants see approved + their own. Admins see all.
        query = CustomLesson.query
        if not user:
            query = query.filter_by(status='approved')
        elif user.role == 'student':
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

            if not year or not term or not subject: continue

            if year not in data: data[year] = {"name": year, "terms": {}}
            if term not in data[year]["terms"]: data[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in data[year]["terms"][term]["subjects"]:
                data[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"), "name": subject,
                    "types": {"videos": {"chapters": {}}, "files": {"chapters": {}}, "quiz": {"chapters": {}}}
                }
            subj_data = data[year]["terms"][term]["subjects"][subject]
            if c_type not in subj_data["types"]: subj_data["types"][c_type] = {"chapters": {}}
            if chapter not in subj_data["types"][c_type]["chapters"]:
                subj_data["types"][c_type]["chapters"][chapter] = {"name": chapter, "lessons": []}

            lesson_dict = {
                "id": item.id, "title": item.title, "notes": item.notes or "", "status": item.status
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

# ==========================================
# Main Routes
# ==========================================
@app.route('/')
def home():
    user = current_user()
    DATA = get_platform_data(user)
    
    is_admin = user.role == 'admin' if user else False
    is_assistant = user.role == 'assistant' if user else False
    
    view_args = {
        'updates': PlatformUpdate.query.order_by(PlatformUpdate.created_at.desc()).all(),
        'is_admin': is_admin, 'is_assistant': is_assistant,
        'student_id': user.id if user else None,
        'student_name': user.name if user else None,
        'data': DATA
    }
    
    subject_id = request.args.get('subject')
    content_type = request.args.get('type')
    chapter_name = request.args.get('chapter')
    year = request.args.get('year')
    term = request.args.get('term')

    if subject_id and content_type and chapter_name:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id and content_type in s["types"]:
                        for c in s["types"][content_type]["chapters"]:
                            if c["name"] == chapter_name:
                                q_query = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name)
                                questions = q_query.order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all() if is_admin else q_query.filter_by(is_answered=True).order_by(LessonQuestion.created_at.desc()).all()
                                return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, **view_args)
    if subject_id and content_type:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id: return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], **view_args)
    if subject_id:
        for y in DATA.values():
            for t in y["terms"].values():
                for s in t["subjects"]:
                    if s["id"] == subject_id: return render_template('index.html', view='select_type', subject=s, **view_args)
    if year and term and year in DATA and term in DATA[year]["terms"]: 
        return render_template('index.html', view='subjects', subjects=DATA[year]["terms"][term]["subjects"], y_id=year, **view_args)
    if year and year in DATA: 
        return render_template('index.html', view='terms', terms=DATA[year]["terms"], y_id=year, **view_args)
    return render_template('index.html', view='years', **view_args)

# ==========================================
# New Auth / Login (Role-based)
# ==========================================
@app.route('/student/login', methods=['POST'])
def student_login():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    
    # Check Admin/Assistant hardcoded passwords first as fallback, but migrate to actual DB accounts
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "123")
    ASSISTANT_PASSWORD = os.environ.get("ASSISTANT_PASSWORD", "AhmedAI")
    
    if password == ADMIN_PASSWORD and username == "admin":
        # Create temp admin in DB if not exists
        admin = Student.query.filter_by(username='admin').first()
        if not admin:
            admin = Student(username='admin', name='الإدارة', role='admin')
            admin.set_password(password)
            db.session.add(admin)
            db.session.commit()
            db.session.add(UserProfile(student_id=admin.id))
            db.session.commit()
        session['student_id'] = admin.id
        return jsonify({"status": "success", "role": "admin"})

    student = Student.query.filter_by(username=username).first()
    if not student or not student.check_password(password):
        return jsonify({"status": "error", "message": "البيانات غير صحيحة"}), 401

    session.permanent = True
    session['student_id'] = student.id
    return jsonify({"status": "success", "student": student.to_dict(), "role": student.role})

@app.route('/student/logout', methods=['POST'])
def student_logout():
    session.pop('student_id', None)
    return jsonify({"status": "success"})

@app.route('/student/register', methods=['POST'])
def student_register():
    data = request.get_json(silent=True) or {}
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    name = data.get('name', '').strip()

    if Student.query.filter_by(username=username).first():
        return jsonify({"status": "error", "message": "الاسم موجود مسبقاً"}), 400

    student = Student(username=username, name=name, role='student') # Default student
    student.set_password(password)
    db.session.add(student)
    db.session.commit()
    db.session.add(UserProfile(student_id=student.id))
    db.session.commit()
    return jsonify({"status": "success"})

# ==========================================
# Review & Content Management (Admin & Assistant)
# ==========================================
@app.route('/admin/add-content', methods=['POST'])
@can_manage_content
def add_custom_content():
    user = current_user()
    data = request.get_json(silent=True) or {}
    
    # If Assistant, content goes to pending. If Admin, it is approved immediately.
    status = 'approved' if user.role == 'admin' else 'pending_review'
    
    lesson = CustomLesson(
        year=data.get('year', ''), term=data.get('term', ''), college=data.get('college', ''),
        subject_name=data.get('subject_name', ''), chapter_name=data.get('chapter_name', 'عام'),
        content_type=data.get('content_type', 'videos'), title=data.get('title', ''),
        url=data.get('url', ''), download_url=data.get('download_url', ''),
        notes=data.get('notes', ''), status=status, created_by=user.id
    )
    db.session.add(lesson)
    db.session.commit()
    msg = "تمت الإضافة بنجاح!" if status == 'approved' else "تم رفع المحتوى وإرساله للمراجعة."
    return jsonify({"status": "success", "message": msg})

@app.route('/admin/review-queue', methods=['GET'])
@admin_required
def get_review_queue():
    lessons = CustomLesson.query.filter_by(status='pending_review').all()
    quizzes = QuizQuestion.query.filter_by(status='pending_review').all()
    return jsonify({
        "lessons": [l.to_dict() for l in lessons],
        "quizzes": [q.to_dict() for q in quizzes]
    })

@app.route('/admin/approve-content/<string:content_type>/<int:item_id>', methods=['POST'])
@admin_required
def approve_content(content_type, item_id):
    data = request.get_json(silent=True) or {}
    action = data.get('action', 'approve') # approve or reject
    new_status = 'approved' if action == 'approve' else 'rejected'
    
    if content_type == 'lesson':
        item = CustomLesson.query.get_or_404(item_id)
    else:
        item = QuizQuestion.query.get_or_404(item_id)
        
    item.status = new_status
    db.session.commit()
    return jsonify({"status": "success", "message": f"تم {'الاعتماد' if action=='approve' else 'الرفض'} بنجاح"})

# ==========================================
# Quiz & Exam Attempts (Separated XP logic)
# ==========================================
@app.route('/api/quiz', methods=['GET'])
def get_quiz():
    chapter_name = request.args.get('chapter')
    user = current_user()
    query = QuizQuestion.query.filter_by(chapter_name=chapter_name)
    
    # Students only see approved questions
    if not user or user.role == 'student':
        query = query.filter_by(status='approved')
        
    questions = query.all()
    return jsonify([q.to_dict() for q in questions])

@app.route('/api/quiz/attempt', methods=['POST'])
@student_required
def save_quiz_attempt():
    user = current_user()
    data = request.get_json()
    
    score = int(data.get('score', 0))
    total = int(data.get('total_questions', 1))
    perc = float(data.get('percentage', 0))
    subject = data.get('subject_name', 'مادة عامة')
    chapter = data.get('chapter_name', 'عام')
    
    # Save the independent attempt
    attempt = QuizAttempt(
        student_id=user.id, subject_name=subject, chapter_name=chapter,
        score=score, total_questions=total, percentage=perc
    )
    db.session.add(attempt)
    db.session.commit()
    
    # XP Gamification Logic (Only award XP once per chapter pass to avoid spam)
    profile = UserProfile.query.filter_by(student_id=user.id).first()
    xp_result = None
    if perc >= 50: # Passed
        reason = f"اجتياز اختبار: {chapter}"
        # Check if already rewarded
        existing = PointTransaction.query.filter_by(student_id=user.id, reason=reason).first()
        if not existing:
            xp_result = profile.add_points(20, reason)
            attempt.xp_awarded = 20
            db.session.commit()
            
    return jsonify({"status": "success", "attempt_id": attempt.id, "xp_awarded": xp_result is not None})

# ==========================================
# Import Questions (CSV / Excel support)
# ==========================================
@app.route('/admin/import-questions', methods=['POST'])
@can_manage_content
def import_questions():
    if 'file' not in request.files:
        return jsonify({"status": "error", "message": "لم يتم إرفاق ملف"}), 400
    
    file = request.files['file']
    if not file.filename.endswith('.csv'):
        return jsonify({"status": "error", "message": "يجب أن يكون الملف بصيغة CSV"}), 400
        
    user = current_user()
    status = 'approved' if user.role == 'admin' else 'pending_review'
    
    try:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        csv_input = csv.DictReader(stream)
        count = 0
        for row in csv_input:
            q = QuizQuestion(
                year=row.get('year', 'عام'), term=row.get('term', 'عام'), college=row.get('college', ''),
                subject_name=row.get('subject_name', 'عام'), chapter_name=row.get('chapter_name', 'عام'),
                question_text=row.get('question_text', ''),
                option_a=row.get('option_a', ''), option_b=row.get('option_b', ''),
                option_c=row.get('option_c', ''), option_d=row.get('option_d', ''),
                correct_answer=row.get('correct_answer', ''),
                explanation=row.get('explanation', ''),
                status=status, created_by=user.id
            )
            db.session.add(q)
            count += 1
        db.session.commit()
        msg = f"تم استيراد {count} سؤال بنجاح كـ {'معتمد' if status=='approved' else 'قيد المراجعة'}."
        return jsonify({"status": "success", "message": msg})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": f"خطأ في معالجة الملف: {str(e)}"}), 500

# (Keep API for Profile, Progress, Leaderboard, AI Chat exactly as they are)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)

