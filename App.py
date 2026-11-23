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
from sqlalchemy import CheckConstraint, Uuid
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
    __table_args__ = (
        CheckConstraint(
            "role IN ('student', 'assistant', 'admin', 'college_supervisor', 'specialization_supervisor')",
            name='students_role_check',
        ),
    )
    id = db.Column(db.Integer, primary_key=True)
    # نوع Uuid الأصلي: يظهر كـ UUID في Postgres (مطابق للعمود الموجود فلا حاجة لـ migration) وكـ CHAR في SQLite
    auth_user_id = db.Column(Uuid, nullable=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    role = db.Column(db.String(50), default='student')  # student, assistant, admin, college_supervisor, specialization_supervisor (الأطول 25 حرفاً — كان String(20) فيفشل حفظه)
    college = db.Column(db.String(150), nullable=True)
    supervisor_college = db.Column(db.String(150), nullable=True)  # نطاق المشرف: اسم الكلية التي يشرف عليها
    supervisor_specialization = db.Column(db.String(150), nullable=True)  # نطاق مشرف التخصص: اسم المادة
    supervisor_subject_id = db.Column(db.Integer, nullable=True)  # نطاق مشرف التخصص الدقيق: معرّف المادة (Q4)
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
            "supervisor_college": self.supervisor_college or '',
            "supervisor_specialization": self.supervisor_specialization or '',
            "supervisor_subject_id": self.supervisor_subject_id,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class College(db.Model):
    __tablename__ = 'colleges'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M") if self.created_at else ""
        }

class Subject(db.Model):
    """المادة ككيان مستقل: الاسم + الكلية + السنة + الترم.
    قبل هذا كانت «المادة» مجرد نص subject_name على كل درس/سؤال، فتعديل
    اسمها كان يعدّل صفاً واحداً فقط وتظهر مادة مكررة. الآن إعادة التسمية
    تحدّث الكيان وكل صفوفه المرتبطة دفعة واحدة."""
    __tablename__ = 'subjects'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    college = db.Column(db.String(150), nullable=False)
    year = db.Column(db.String(100), nullable=False)
    term = db.Column(db.String(100), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('college', 'year', 'term', 'name',
                                           name='uq_subject_college_year_term_name'),)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "college": self.college,
            "year": self.year,
            "term": self.term,
            "lesson_count": _subject_lesson_count(self),
            "question_count": _subject_question_count(self),
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
    subject_id = db.Column(db.Integer, nullable=True)  # رابط المادة ككيان (جدول subjects)؛ subject_name يبقى نصاً منسوخاً للتوافق
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
            "subject_id": self.subject_id,
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
    subject_id = db.Column(db.Integer, nullable=True)  # رابط المادة ككيان (جدول subjects)؛ subject_name يبقى نصاً منسوخاً للتوافق
    chapter_name = db.Column(db.String(150), nullable=False)
    question_text = db.Column(db.Text, nullable=False)
    option_a = db.Column(db.Text, nullable=True)
    option_b = db.Column(db.Text, nullable=True)
    option_c = db.Column(db.Text, nullable=True)
    option_d = db.Column(db.Text, nullable=True)
    option_e = db.Column(db.Text, nullable=True)
    correct_answer = db.Column(db.String(200), nullable=True)
    explanation = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), default='approved')
    created_by = db.Column(db.Integer, db.ForeignKey('students.id'), nullable=True)
    review_notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        q_text = (self.question_text or '').strip()
        opts = [self.option_a, self.option_b, self.option_c, self.option_d, self.option_e]
        cleaned_opts = []
        for o in opts:
            if o and o.strip() and o.strip() != q_text:
                cleaned_opts.append(o.strip())

        # قد تُستورد correct_answer كحرف (A-E) أو رقم (1-5) بدل نص الخيار؛
        # نحوّلها إلى نص الخيار المطابق حتى تقارنها الواجهة بشكل صحيح.
        # ملاحظة: التحويل يتم على cleaned_opts بترتيب الأعمدة المخزنة الأصلي
        # (المؤشر 0 = option_a)، لأن بعض الصفوف كُتبت حروفها على هذا الأساس.
        correct = (self.correct_answer or "").strip()
        if len(correct) == 1 and correct.upper() in "ABCDE":
            idx = "ABCDE".index(correct.upper())
            if idx < len(cleaned_opts):
                correct = cleaned_opts[idx]
        elif len(correct) == 1 and correct in "12345":
            idx = int(correct) - 1
            if idx < len(cleaned_opts):
                correct = cleaned_opts[idx]

        # خلل الاستيراد القديم: كل الأسئلة المستوردة تحمل question_text = "اختبار"
        # ونص السؤال الحقيقي في option_a، فتظهر الواجهة "اختبار" عنواناً والسؤال
        # نفسه زر إجابة أول. نعرض نص السؤال الحقيقي عنواناً والخيارات الحقيقية
        # فقط، دون تغيير البيانات المخزنة أو إزاحة الحقول.
        display_q = q_text
        display_opts = cleaned_opts
        if _is_placeholder_question_title(q_text) and cleaned_opts:
            display_q = cleaned_opts[0]
            display_opts = cleaned_opts[1:]

        return {
            "id": self.id,
            "year": self.year,
            "term": self.term,
            "college": self.college or "",
            "subject_name": self.subject_name,
            "subject_id": self.subject_id,
            "chapter_name": self.chapter_name,
            "question_text": display_q,
            "option_a": display_opts[0] if len(display_opts) > 0 else "",
            "option_b": display_opts[1] if len(display_opts) > 1 else "",
            "option_c": display_opts[2] if len(display_opts) > 2 else "",
            "option_d": display_opts[3] if len(display_opts) > 3 else "",
            "option_e": display_opts[4] if len(display_opts) > 4 else "",
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
        # ترحيل أعمدة جديدة لجداول قائمة: create_all() ينشئ الجداول الناقصة فقط
        # ولا يضيف أعمدة لجداول موجودة، فأي استعلام على Student كان سيسقط التشغيل
        # (خطأ: column students.supervisor_college does not exist).
        try:
            db.session.execute(db.text("ALTER TABLE students ADD COLUMN IF NOT EXISTS supervisor_college VARCHAR(150)"))
            db.session.execute(db.text("ALTER TABLE students ADD COLUMN IF NOT EXISTS supervisor_specialization VARCHAR(150)"))
            db.session.execute(db.text("ALTER TABLE custom_lessons ADD COLUMN IF NOT EXISTS college VARCHAR(150)"))
            db.session.execute(db.text("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS college VARCHAR(150)"))
            # أعمدة ربط المواد ككيانات: create_all لا يضيف أعمدة لجداول موجودة
            db.session.execute(db.text("ALTER TABLE custom_lessons ADD COLUMN IF NOT EXISTS subject_id INTEGER"))
            db.session.execute(db.text("ALTER TABLE quiz_questions ADD COLUMN IF NOT EXISTS subject_id INTEGER"))
            db.session.execute(db.text("ALTER TABLE students ADD COLUMN IF NOT EXISTS supervisor_subject_id INTEGER"))
            # توسيع عمود الدور إلى VARCHAR(50): قيمة specialization_supervisor طولها 25 حرفاً
            # فلا تتسع في VARCHAR(20) وكان الحفظ يفشل بخطأ 500. يُتخطى على SQLite، وآمن عند
            # إعادة التشغيل لأن تعيين نفس النوع مجدداً لا يغيّر شيئاً.
            if "sqlite" not in db_url:
                db.session.execute(db.text("ALTER TABLE students ALTER COLUMN role TYPE VARCHAR(50)"))
            db.session.commit()
            # تحديث قيد فحص الدور: القيد القديم students_role_check يسمح فقط بالأدوار
            # القديمة ويرفض college_supervisor وspecialization_supervisor (خطأ 23514
            # عند تعيين مشرف). يُتخطى على SQLite، وآمن عند إعادة التشغيل لأن
            # DROP IF EXISTS ثم ADD من جديد يعيدان إنشاء القيد في كل مرة.
            if "sqlite" not in db_url:
                try:
                    db.session.execute(db.text("ALTER TABLE students DROP CONSTRAINT IF EXISTS students_role_check"))
                    db.session.commit()
                    print("تم حذف قيد الدور القديم students_role_check", flush=True)
                except Exception as _role_drop_e:
                    db.session.rollback()
                    print(f"تعذر حذف قيد الدور القديم (تجاهل): {_role_drop_e}", flush=True)
                try:
                    db.session.execute(db.text(
                        "ALTER TABLE students ADD CONSTRAINT students_role_check "
                        "CHECK (role IN ('student', 'assistant', 'admin', 'college_supervisor', 'specialization_supervisor'))"
                    ))
                    db.session.commit()
                    print("تم تحديث قيد الدور students_role_check ليشمل أدوار المشرفين", flush=True)
                except Exception as _role_add_e:
                    db.session.rollback()
                    print(f"تعذر تحديث قيد الدور (تجاهل): {_role_add_e}", flush=True)
        except Exception as _mig_e:
            db.session.rollback()
            print(f"تعذر ترحيل الأعمدة الجديدة (تجاهل — قد تكون قاعدة بيانات محلية): {_mig_e}", flush=True)
        students = Student.query.all()
        for s in students:
            if not UserProfile.query.filter_by(student_id=s.id).first():
                profile = UserProfile(student_id=s.id)
                db.session.add(profile)

        db.session.commit()

    except Exception as e:
        db.session.rollback()
        # لا تبتلع الخطأ بصمت: اطبعه ثم أعد رفعه ليفشل التشغيل بسرعة بدل العمل بجداول ناقصة
        print(f"خطأ فادح عند إنشاء جداول قاعدة البيانات: {e}", flush=True)
        raise

# بذر الكليات الافتراضية وربط المحتوى القديم بكلية الهندسة.
# كتلة مستقلة محمية بـ try/except حتى لا يُسقط أي خطأ بياناتٍ التشغيل أبداً.
with app.app_context():
    try:
        for _cname in ("كلية الهندسة", "كلية الطب"):
            if not College.query.filter_by(name=_cname).first():
                db.session.add(College(name=_cname))
        db.session.commit()
        CustomLesson.query.filter((CustomLesson.college == None) | (CustomLesson.college == "")).update({"college": "كلية الهندسة"}, synchronize_session=False)
        QuizQuestion.query.filter((QuizQuestion.college == None) | (QuizQuestion.college == "")).update({"college": "كلية الهندسة"}, synchronize_session=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"تعذر بذر الكليات وربط المحتوى القديم: {e}", flush=True)

# بذر المواد من المحتوى الحالي وربط الدروس والأسئلة بها.
# قبل هذا كانت المادة مجرد نص على كل صف؛ الآن كل (كلية، سنة، ترم، اسم مادة)
# مميز يصبح صفاً في جدول subjects، وتربط الصفوف به عبر subject_id.
# الدروس بلا اسم مادة تربط بمادة «عام» (Q3). كتلة مستقلة محمية بـ try/except
# حتى لا يُسقط أي خطأ بياناتٍ التشغيل أبداً، وهي خاملة (idempotent) عند إعادة التشغيل.
with app.app_context():
    try:
        _subject_keys = set()
        for _model in (CustomLesson, QuizQuestion):
            for _college, _year, _term, _sname in db.session.query(
                    _model.college, _model.year, _model.term, _model.subject_name).distinct().all():
                _college = (_college or '').strip() or 'كلية الهندسة'
                _year = (_year or '').strip()
                _term = (_term or '').strip()
                _sname = (_sname or '').strip() or 'عام'
                if not _year or not _term:
                    continue
                _key = (_college, _year, _term, _sname)
                if _key in _subject_keys:
                    continue
                _subject_keys.add(_key)
                if not Subject.query.filter_by(college=_college, year=_year, term=_term, name=_sname).first():
                    db.session.add(Subject(name=_sname, college=_college, year=_year, term=_term))
        db.session.commit()
        # ربط الصفوف التي بلا subject_id بمادتها المطابقة
        for _model in (CustomLesson, QuizQuestion):
            for _row in _model.query.filter(_model.subject_id == None).all():
                _college = (_row.college or '').strip() or 'كلية الهندسة'
                _year = (_row.year or '').strip()
                _term = (_row.term or '').strip()
                _sname = (_row.subject_name or '').strip() or 'عام'
                if not _year or not _term:
                    continue
                _subj = Subject.query.filter_by(college=_college, year=_year, term=_term, name=_sname).first()
                if _subj:
                    _row.subject_id = _subj.id
        db.session.commit()
        print("تم بذر المواد وربط الدروس والأسئلة بها", flush=True)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("تعذر بذر المواد وربطها")
        print(f"تعذر بذر المواد وربطها: {e}", flush=True)


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

SUPERVISOR_ROLES = ('college_supervisor', 'specialization_supervisor')

def _in_scope(user, college, subject_name=None, subject_id=None):
    """هل هذا العنصر (كلية/تخصص) ضمن نطاق المستخدم؟ المشرف الأعلى يرى كل شيء.
    لمشرف التخصص: إن وُجد supervisor_subject_id يُقارن به بدقة، وإلا يُقارن بالاسم."""
    if not user:
        return False
    if user.role == 'admin':
        return True
    if user.role == 'college_supervisor':
        return (user.supervisor_college or '') == (college or '')
    if user.role == 'specialization_supervisor':
        if subject_id is not None and getattr(user, 'supervisor_subject_id', None):
            return user.supervisor_subject_id == subject_id
        return ((user.supervisor_college or '') == (college or '')
                and (user.supervisor_specialization or '') == (subject_name or ''))
    return False

def _scope_query(query, user, model):
    """فلترة استعلام محتوى (CustomLesson أو QuizQuestion) حسب نطاق المستخدم.
    المشرف الأعلى والمساعد يبقيان على سلوكهما الحالي (الفلترة تتم في المسار نفسه)."""
    if not user or user.role == 'admin' or user.role == 'assistant':
        return query
    if user.role == 'college_supervisor':
        return query.filter(model.college == (user.supervisor_college or ''))
    if user.role == 'specialization_supervisor':
        if getattr(user, 'supervisor_subject_id', None):
            return query.filter(model.subject_id == user.supervisor_subject_id)
        return query.filter(model.college == (user.supervisor_college or ''),
                            model.subject_name == (user.supervisor_specialization or ''))
    return query

def _subject_college_info(subject_id):
    """كلية واسم مادة معرّف مادة ما (year_term_subject) من صفوف الدروس أو الأسئلة."""
    for model in (CustomLesson, QuizQuestion):
        for row in model.query.with_entities(model.year, model.term, model.subject_name, model.college).all():
            sid = f"{row.year}_{row.term}_{row.subject_name}".replace(" ", "_")
            if sid == subject_id:
                return (row.college or "كلية الهندسة", row.subject_name)
    return None

def _question_in_scope(q, user):
    """هل سؤال درس ما ضمن نطاق المشرف؟"""
    info = _subject_college_info(q.subject_id)
    if not info:
        return False
    college, subject_name = info
    return _in_scope(user, college, subject_name)

def admin_or_supervisor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role not in ('admin',) + SUPERVISOR_ROLES:
            return jsonify({"status": "error", "message": "غير مصرح لك (مطلوب مسؤول)"}), 403
        return f(*args, **kwargs)
    return decorated_function

def can_manage_content(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()
        if not user or user.role not in ['admin', 'assistant'] + list(SUPERVISOR_ROLES):
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
    # البنية: كلية -> سنوات -> فصول دراسية -> مواد -> أنواع محتوى -> فصول -> دروس
    data = {}
    try:
        # الكليات المعروفة من جدول الكليات (مرتبة حسب الإضافة)؛
        # وأي كلية تظهر في محتوى قديم تُضاف تلقائياً حتى لا يضيع محتواها.
        try:
            known_colleges = [c.name for c in College.query.order_by(College.id).all()]
        except Exception:
            known_colleges = []
        if not known_colleges:
            known_colleges = ["كلية الهندسة"]
        for _cname in known_colleges:
            data[_cname] = {"name": _cname, "years": {}}

        # المواد ككيانات: نزرع كل مادة من جدول subjects أولاً حتى تظهر
        # المواد الفارغة أيضاً على الصفحة الرئيسية مع حالة فارغة (Q1).
        _subjects_by_id = {}
        try:
            _all_subjects = Subject.query.order_by(Subject.id).all()
        except Exception:
            db.session.rollback()
            _all_subjects = []
        for _s in _all_subjects:
            _s_college = (_s.college or '').strip() or "كلية الهندسة"
            _s_year = YEAR_NAME_FIXES.get((_s.year or '').strip(), (_s.year or '').strip())
            _s_term = (_s.term or '').strip()
            _s_name = (_s.name or '').strip()
            if not _s_year or not _s_term or not _s_name:
                continue
            _subjects_by_id[_s.id] = _s
            if _s_college not in data:
                data[_s_college] = {"name": _s_college, "years": {}}
            _s_years = data[_s_college]["years"]
            if _s_year not in _s_years:
                _s_years[_s_year] = {"name": _s_year, "terms": {}}
            if _s_term not in _s_years[_s_year]["terms"]:
                _s_years[_s_year]["terms"][_s_term] = {"name": _s_term, "subjects": {}}
            _s_subs = _s_years[_s_year]["terms"][_s_term]["subjects"]
            if _s_name not in _s_subs:
                _s_subs[_s_name] = {
                    "id": f"{_s_year}_{_s_term}_{_s_name}".replace(" ", "_"),
                    "name": _s_name, "year": _s_year, "term": _s_term, "college": _s_college,
                    "subject_id": _s.id, "lesson_count": 0,
                    "types": {
                        "videos": {"chapters": {}},
                        "files": {"chapters": {}},
                        "quiz": {"chapters": {}}
                    }
                }

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
        quiz_groups = quiz_query.with_entities(QuizQuestion.year, QuizQuestion.term, QuizQuestion.subject_name, QuizQuestion.chapter_name, QuizQuestion.college).distinct().all()
        for item in [*lessons, *quiz_groups]:
            # الدروس المرتبطة بمادة تُحلّ عبر subject_id (الكلية/السنة/الترم/
            # الاسم من صف المادة نفسه)؛ أما الصفوف بلا رابط فتُجمّع بالنص كما قبل.
            _linked = _subjects_by_id.get(item.subject_id) if isinstance(item, CustomLesson) else None
            if _linked is not None:
                college = (_linked.college or '').strip() or "كلية الهندسة"
                year = YEAR_NAME_FIXES.get((_linked.year or '').strip(), (_linked.year or '').strip())
                term = (_linked.term or '').strip()
                subject = (_linked.name or '').strip()
            else:
                year = (item.year or '').strip()
                year = YEAR_NAME_FIXES.get(year, year)
                term = (item.term or '').strip()
                subject = (item.subject_name or '').strip()
                college = (getattr(item, 'college', '') or '').strip() or "كلية الهندسة"
            chapter = (item.chapter_name or 'عام').strip() or 'عام'
            c_type = (getattr(item, 'content_type', 'quiz') or 'videos').strip()

            if not year or not term or not subject:
                continue

            if college not in data:
                data[college] = {"name": college, "years": {}}
            years = data[college]["years"]
            if year not in years:
                years[year] = {"name": year, "terms": {}}
            if term not in years[year]["terms"]:
                years[year]["terms"][term] = {"name": term, "subjects": {}}
            if subject not in years[year]["terms"][term]["subjects"]:
                years[year]["terms"][term]["subjects"][subject] = {
                    "id": f"{year}_{term}_{subject}".replace(" ", "_"),
                    "name": subject, "year": year, "term": term, "college": college,
                    "subject_id": _linked.id if _linked is not None else None, "lesson_count": 0,
                    "types": {
                        "videos": {"chapters": {}},
                        "files": {"chapters": {}},
                        "quiz": {"chapters": {}}
                    }
                }

            subj_data = years[year]["terms"][term]["subjects"][subject]
            
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
            subj_data["lesson_count"] = subj_data.get("lesson_count", 0) + 1

        for c in data.values():
            for y in c["years"].values():
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
    is_supervisor = (user.role in SUPERVISOR_ROLES) if user else False

    try:
        colleges = [c.to_dict() for c in College.query.order_by(College.id).all()]
    except Exception:
        db.session.rollback()
        colleges = []
    if not colleges:
        colleges = [{"id": 0, "name": "كلية الهندسة"}]

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
        'is_supervisor': is_supervisor,
        'user_role': user.role if user else '',
        'supervisor_college': (user.supervisor_college or '') if user else '',
        'supervisor_specialization': (user.supervisor_specialization or '') if user else '',
        'student_id': user.id if user else None,
        'student_name': user.name if user else None,
        'colleges': colleges,
        'data': data
    }

    college_name = (request.args.get('college') or '').strip()
    year = request.args.get('year')
    term = request.args.get('term')
    subject_id = request.args.get('subject')
    content_type = request.args.get('type')
    chapter_name = request.args.get('chapter')

    def _find_subject(sid):
        for c in data.values():
            for y in c["years"].values():
                for t in y["terms"].values():
                    for s in t["subjects"]:
                        if s["id"] == sid:
                            return s
        return None

    def _find_year_term(yname, tname=None):
        # أول كلية تحوي هذه السنة (والترم إن حدّد) — كلية الهندسة أولاً لأنها بُذرت أولاً
        for c in data.values():
            years = c["years"]
            if yname in years:
                if tname is None:
                    return c, years[yname]
                if tname in years[yname]["terms"]:
                    return c, years[yname]
        return None, None

    if subject_id and content_type and chapter_name:
        s = _find_subject(subject_id)
        if s and content_type in s["types"]:
            for c in s["types"][content_type]["chapters"]:
                if c["name"] == chapter_name:
                    q_query = LessonQuestion.query.filter_by(subject_id=subject_id, chapter_name=chapter_name)
                    if is_admin or is_assistant:
                        questions = q_query.order_by(LessonQuestion.is_answered.asc(), LessonQuestion.created_at.desc()).all()
                    else:
                        questions = q_query.filter_by(is_answered=True).order_by(LessonQuestion.created_at.desc()).all()
                    return render_template('index.html', view='content', subject=s, chapter=c, content_type=content_type, questions=questions, **view_context)

    if subject_id and content_type:
        s = _find_subject(subject_id)
        if s and content_type in s["types"]:
            return render_template('index.html', view='chapters', subject=s, content_type=content_type, chapters=s["types"][content_type]["chapters"], **view_context)

    if subject_id:
        s = _find_subject(subject_id)
        if s:
            return render_template('index.html', view='select_type', subject=s, **view_context)

    if year and term:
        c, ydata = _find_year_term(year, term)
        if ydata:
            return render_template('index.html', view='subjects', subjects=ydata["terms"][term]["subjects"], y_id=year, college_name=c["name"], **view_context)

    if year:
        c, ydata = _find_year_term(year)
        if ydata:
            return render_template('index.html', view='terms', terms=ydata["terms"], y_id=year, college_name=c["name"], **view_context)

    if college_name and college_name in data:
        return render_template('index.html', view='years', years=data[college_name]["years"], college_name=college_name, **view_context)

    return render_template('index.html', view='colleges', **view_context)

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

def _is_placeholder_question_title(text):
    """هل نص السؤال مجرد كلمة نائبة بدل السؤال الحقيقي؟
    الملفات المستوردة وضعت كلمة "اختبار" (بالباء) في question_text
    بدل نص السؤال، وقد تظهر أيضاً بصيغة "اختيار" (بالياء). نقارن
    الحروف فقط بعد توحيد الألف والياء والكاف وحذف الحروف غير الأبجدية،
    لتجاهل الحروف غير المرئية التي قد تحملها البيانات المستوردة:
    الألف المهموزة \u0623 والمكسورة \u0625 والممدودة \u0622 وألف
    الوصلة \u0671 تصبح ألفاً عادية \u0627، والياء الفارسية \u06cc
    والألف المقصورة \u0649 تصبحان ياءً عربية \u064a، والكاف الفارسية
    \u06a9 تصبح كافاً عربية \u0643."""
    t = (text or "").strip()
    for _alef in ("\u0623", "\u0625", "\u0622", "\u0671"):
        t = t.replace(_alef, "\u0627")
    t = t.replace("\u06cc", "\u064a").replace("\u0649", "\u064a").replace("\u06a9", "\u0643")
    t = "".join(ch for ch in t if ch.isalpha())
    return t in ("اختبار", "اختيار")

def _safe_str(value, default=""):
    """نص آمن: يعيد القيمة الافتراضية عند None، وإلا النص بعد إزالة المسافات."""
    if value is None:
        return default
    return str(value).strip()

# القيم القياسية للسنة والترم (تطبيع الإدخال):
# أي صيغة مقبولة (رقم أو اسم مختصر أو كامل) تُطبع إلى القيمة القياسية عند
# إنشاء مادة أو إضافة/تعديل محتوى أو استيراد أسئلة، حتى لا تنشأ مواد مخفية
# عن الصفحة الرئيسية أو بطاقات سنة مكررة بسبب اختلاف صياغة النص الحر.
# القيم غير القابلة للتطبيع تُرفض برسالة 400 تذكر القيم المقبولة.
CANONICAL_YEARS = {
    '1': 'السنة الأولى', 'الأولى': 'السنة الأولى', 'السنة الأولى': 'السنة الأولى',
    'الاولى': 'السنة الأولى', 'السنة الاولى': 'السنة الأولى', 'السنة الاولئ': 'السنة الأولى',
    '2': 'السنة الثانية', 'الثانية': 'السنة الثانية', 'السنة الثانية': 'السنة الثانية',
    '3': 'السنة الثالثة', 'الثالثة': 'السنة الثالثة', 'السنة الثالثة': 'السنة الثالثة',
    '4': 'السنة الرابعة', 'الرابعة': 'السنة الرابعة', 'السنة الرابعة': 'السنة الرابعة',
    '5': 'السنة الخامسة', 'الخامسة': 'السنة الخامسة', 'السنة الخامسة': 'السنة الخامسة',
    '6': 'السنة السادسة', 'السادسة': 'السنة السادسة', 'السنة السادسة': 'السنة السادسة',
}
CANONICAL_TERMS = {
    '1': 'الترم الأول', 'الأول': 'الترم الأول', 'الترم الأول': 'الترم الأول', 'الفصل الأول': 'الترم الأول',
    '2': 'الترم الثاني', 'الثاني': 'الترم الثاني', 'الترم الثاني': 'الترم الثاني', 'الفصل الثاني': 'الترم الثاني',
}
_YEAR_VALUES_TEXT = '، '.join(['السنة الأولى', 'السنة الثانية', 'السنة الثالثة', 'السنة الرابعة', 'السنة الخامسة', 'السنة السادسة'])
_TERM_VALUES_TEXT = 'الترم الأول، الترم الثاني'

def _normalize_year(value):
    """تطبيع سنة دراسية إلى قيمتها القياسية، أو None إن لم تُطابق أي صيغة مقبولة."""
    return CANONICAL_YEARS.get(_safe_str(value))

def _normalize_term(value):
    """تطبيع فصل دراسي إلى قيمته القياسية، أو None إن لم تُطابق أي صيغة مقبولة."""
    return CANONICAL_TERMS.get(_safe_str(value))

@app.route('/admin/add-content', methods=['POST'])
@can_manage_content
def add_custom_content():
    try:
        user = get_current_user()
        data = request.get_json(silent=True) or request.form or {}

        status = 'approved' if user.role == 'admin' else 'pending_review'

        title = _safe_str(data.get('title'))
        content_type = _safe_str(data.get('content_type'), 'videos')

        # Flow B: المادة تُحدَّد من القائمة المنسدلة (subject_id) وتُؤخذ
        # الكلية/السنة/الترم من صف المادة نفسه.
        subject = None
        subject_id_raw = data.get('subject_id')
        if subject_id_raw not in (None, '', 0, '0'):
            try:
                subject = Subject.query.get(int(subject_id_raw))
            except (TypeError, ValueError):
                subject = None
            if not subject:
                return jsonify({"status": "error", "message": "المادة غير موجودة — اختر المادة أولاً"}), 400
            if user.role in SUPERVISOR_ROLES and not _in_scope(user, subject.college, subject.name, subject.id):
                return jsonify({"status": "error", "message": "غير مصرح لك بالإضافة إلى مادة خارج نطاقك"}), 403
            college_value = subject.college
            year = subject.year
            term = subject.term
            subject_name = subject.name
        else:
            # مسار التوافق: بلا subject_id يُقبل الاسم فقط إن طابق مادة موجودة.
            # السنة والترم يُطبَّعان إلى القيم القياسية حتى يطابقا صف المادة.
            _raw_year = _safe_str(data.get('year'))
            _raw_term = _safe_str(data.get('term'))
            year = _normalize_year(_raw_year) if _raw_year else None
            term = _normalize_term(_raw_term) if _raw_term else None
            if _raw_year and not year:
                return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — القيم المقبولة: " + _YEAR_VALUES_TEXT}), 400
            if _raw_term and not term:
                return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
            subject_name = _safe_str(data.get('subject_name'))
            # نطاق المشرف: مشرف الكلية/التخصص لا يضيف إلا ضمن نطاقه،
            # والمشرف الأعلى يختار الكلية من النموذج (الافتراضي: كلية الهندسة).
            if user.role == 'college_supervisor':
                college_value = user.supervisor_college or 'كلية الهندسة'
            elif user.role == 'specialization_supervisor':
                college_value = user.supervisor_college or 'كلية الهندسة'
                subject_name = user.supervisor_specialization or subject_name
            else:
                college_value = _safe_str(data.get('college')) or 'كلية الهندسة'
            if not College.query.filter_by(name=college_value).first():
                db.session.add(College(name=college_value))
            if not year or not term or not subject_name:
                return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
            subject = Subject.query.filter_by(college=college_value, year=year, term=term, name=subject_name).first()
            if not subject:
                return jsonify({"status": "error", "message": "اختر المادة أولاً — لا توجد مادة بهذا الاسم في الكلية والسنة والترم"}), 400
            if user.role in SUPERVISOR_ROLES and not _in_scope(user, subject.college, subject.name, subject.id):
                return jsonify({"status": "error", "message": "غير مصرح لك بالإضافة إلى مادة خارج نطاقك"}), 403

        if not title:
            return jsonify({"status": "error", "message": "السنة والترم واسم المادة والعنوان حقول مطلوبة"}), 400
        if content_type not in ('videos', 'files', 'quiz'):
            return jsonify({"status": "error", "message": "نوع المحتوى غير صالح (يجب أن يكون videos أو files أو quiz)"}), 400

        lesson = CustomLesson(
            year=year,
            term=term,
            college=college_value,
            subject_name=subject_name,
            subject_id=subject.id,
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
        app.logger.exception("فشل إضافة محتوى")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/custom-content', methods=['GET'])
@can_manage_content
def get_custom_content():
    user = get_current_user()
    try:
        if user.role == 'admin':
            lessons = CustomLesson.query.order_by(CustomLesson.created_at.desc()).all()
        elif user.role in SUPERVISOR_ROLES:
            lessons = _scope_query(CustomLesson.query, user, CustomLesson).order_by(CustomLesson.created_at.desc()).all()
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

        if user.role == 'admin':
            pass
        elif user.role in SUPERVISOR_ROLES:
            if not _in_scope(user, item.college, item.subject_name, item.subject_id):
                return jsonify({"status": "error", "message": "غير مصرح لك بتعديل محتوى خارج نطاقك"}), 403
        elif item.created_by != user.id:
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل محتوى الآخرين"}), 403

        data = request.get_json(silent=True) or request.form or {}
        # نقل الدرس إلى مادة أخرى عبر القائمة المنسدلة (subject_id):
        # الكلية/السنة/الترم/الاسم تُؤخذ من صف المادة، وهوية الدرس لا تتغير
        # فتبقى مراجع التقدّم والمفضلة والمهام سليمة.
        _move_subject_id = data.get('subject_id')
        if _move_subject_id not in (None, '', 0, '0'):
            try:
                _target = Subject.query.get(int(_move_subject_id))
            except (TypeError, ValueError):
                _target = None
            if not _target:
                return jsonify({"status": "error", "message": "المادة غير موجودة — اختر المادة أولاً"}), 400
            if user.role in SUPERVISOR_ROLES and not _in_scope(user, _target.college, _target.name, _target.id):
                return jsonify({"status": "error", "message": "غير مصرح لك بنقل المحتوى إلى مادة خارج نطاقك"}), 403
            item.subject_id = _target.id
            item.subject_name = _target.name
            item.year = _target.year
            item.term = _target.term
            item.college = _target.college
        else:
            # قيمة سنة/ترم مرسلة تُطبَّع إلى القيم القياسية؛ قيمة غير قابلة
            # للتطبيع تُرفض بدل كتابة نص حر يفك ارتباط الدرس بمادته.
            _raw_year = _safe_str(data.get('year'))
            _raw_term = _safe_str(data.get('term'))
            if _raw_year:
                _ny = _normalize_year(_raw_year)
                if not _ny:
                    return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — القيم المقبولة: " + _YEAR_VALUES_TEXT}), 400
                item.year = _ny
            if _raw_term:
                _nt = _normalize_term(_raw_term)
                if not _nt:
                    return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
                item.term = _nt
            item.college = _safe_str(data.get('college'), item.college)
            item.subject_name = _safe_str(data.get('subject_name'), item.subject_name)
        # المشرف لا ينقل المحتوى خارج نطاقه
        if user.role == 'college_supervisor':
            item.college = user.supervisor_college or item.college
        elif user.role == 'specialization_supervisor':
            item.college = user.supervisor_college or item.college
            item.subject_name = user.supervisor_specialization or item.subject_name
        # بلا نقل عبر القائمة: أعد مزامنة subject_id مع (الكلية، السنة، الترم، الاسم)
        if _move_subject_id in (None, '', 0, '0'):
            _synced = Subject.query.filter_by(college=item.college, year=item.year, term=item.term, name=item.subject_name).first()
            item.subject_id = _synced.id if _synced else None
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
        app.logger.exception("فشل تعديل محتوى: content_id=%s", content_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/delete-content/<int:content_id>', methods=['POST'])
@can_manage_content
def delete_custom_content(content_id):
    try:
        user = get_current_user()
        item = CustomLesson.query.get_or_404(content_id)
        if user.role != 'admin' and not _in_scope(user, item.college, item.subject_name):
            return jsonify({"status": "error", "message": "غير مصرح لك بحذف محتوى خارج نطاقك"}), 403
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
@admin_or_supervisor_required
def get_review_queue():
    try:
        user = get_current_user()
        lessons = _scope_query(CustomLesson.query.filter_by(status='pending_review'), user, CustomLesson).all()
        quizzes = _scope_query(QuizQuestion.query.filter_by(status='pending_review'), user, QuizQuestion).all()
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
# الكليات والمشرفون (المشرف الأعلى فقط)
# ==========================================

@app.route('/admin/colleges', methods=['GET'])
@admin_required
def get_colleges():
    try:
        return jsonify([c.to_dict() for c in College.query.order_by(College.id).all()])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-college', methods=['POST'])
@admin_required
def add_college():
    try:
        data = request.get_json(silent=True) or request.form or {}
        name = _safe_str(data.get('name'))
        if not name:
            return jsonify({"status": "error", "message": "اسم الكلية مطلوب"}), 400
        if College.query.filter_by(name=name).first():
            return jsonify({"status": "error", "message": "هذه الكلية موجودة بالفعل"}), 400
        db.session.add(College(name=name))
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إضافة الكلية بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/remove-college', methods=['POST'])
@admin_required
def remove_college():
    try:
        data = request.get_json(silent=True) or request.form or {}
        college_id = data.get('id')
        college = None
        if college_id:
            try:
                college = College.query.get(int(college_id))
            except (TypeError, ValueError):
                college = None
        if not college and _safe_str(data.get('name')):
            college = College.query.filter_by(name=_safe_str(data.get('name'))).first()
        if not college:
            return jsonify({"status": "error", "message": "الكلية غير موجودة"}), 404
        name = college.name
        if CustomLesson.query.filter_by(college=name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف كلية فيها مقررات — انقل المقررات أو احذفها أولاً"}), 400
        if QuizQuestion.query.filter_by(college=name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف كلية فيها أسئلة اختبار — احذف الأسئلة أولاً"}), 400
        if Student.query.filter(Student.role.in_(list(SUPERVISOR_ROLES)), Student.supervisor_college == name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف كلية لديها مشرفون — أزل المشرفين أولاً"}), 400
        db.session.delete(college)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف الكلية بنجاح"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# المواد ككيانات (Flow A: إضافة مادة مستقلة)
# ==========================================

def _subject_lesson_count(subj):
    """عدد الدروس المرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)."""
    n = CustomLesson.query.filter_by(subject_id=subj.id).count()
    n += CustomLesson.query.filter(
        CustomLesson.subject_id == None,
        CustomLesson.college == subj.college,
        CustomLesson.year == subj.year,
        CustomLesson.term == subj.term,
        CustomLesson.subject_name == subj.name).count()
    return n

def _subject_question_count(subj):
    """عدد أسئلة الاختبار المرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)."""
    n = QuizQuestion.query.filter_by(subject_id=subj.id).count()
    n += QuizQuestion.query.filter(
        QuizQuestion.subject_id == None,
        QuizQuestion.college == subj.college,
        QuizQuestion.year == subj.year,
        QuizQuestion.term == subj.term,
        QuizQuestion.subject_name == subj.name).count()
    return n

def _subject_lesson_titles(subj, limit=3):
    """عناوين الدروس المرتبطة بالمادة (حتى limit) لعرضها في رسائل الرفض،
    حتى يرى المستخدم ما الذي يمنع الحذف بدل «فيها دروس» فقط."""
    rows = CustomLesson.query.filter_by(subject_id=subj.id).order_by(CustomLesson.id).limit(limit).all()
    if len(rows) < limit:
        rows += CustomLesson.query.filter(
            CustomLesson.subject_id == None,
            CustomLesson.college == subj.college,
            CustomLesson.year == subj.year,
            CustomLesson.term == subj.term,
            CustomLesson.subject_name == subj.name).order_by(CustomLesson.id).limit(limit - len(rows)).all()
    titles = []
    for r in rows:
        _t = (r.title or '').strip()
        _ch = (r.chapter_name or '').strip()
        titles.append((_t + (' — ' + _ch if _ch else '')) or 'درس بدون عنوان')
    return titles

def _subject_has_lessons(subj):
    """هل توجد دروس مرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)؟"""
    return _subject_lesson_count(subj) > 0

def _subject_has_questions(subj):
    """هل توجد أسئلة اختبار مرتبطة بالمادة (عبر subject_id أو عبر الاسم القديم)؟"""
    return _subject_question_count(subj) > 0

@app.route('/admin/subjects', methods=['GET'])
@admin_or_supervisor_required
def get_subjects():
    user = get_current_user()
    try:
        query = Subject.query
        if user.role == 'college_supervisor':
            query = query.filter_by(college=(user.supervisor_college or ''))
        elif user.role == 'specialization_supervisor':
            if getattr(user, 'supervisor_subject_id', None):
                query = query.filter_by(id=user.supervisor_subject_id)
            else:
                query = query.filter_by(college=(user.supervisor_college or ''),
                                        name=(user.supervisor_specialization or ''))
        return jsonify([s.to_dict() for s in query.order_by(Subject.college, Subject.year, Subject.term, Subject.name).all()])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-subject', methods=['POST'])
@admin_or_supervisor_required
def add_subject():
    try:
        user = get_current_user()
        if user.role == 'specialization_supervisor':
            return jsonify({"status": "error", "message": "مشرف التخصص لا يمكنه إضافة مواد — المادة الخاصة بك موجودة بالفعل"}), 403
        data = request.get_json(silent=True) or request.form or {}
        name = _safe_str(data.get('name'))
        raw_year = _safe_str(data.get('year'))
        raw_term = _safe_str(data.get('term'))
        if user.role == 'college_supervisor':
            college = user.supervisor_college or 'كلية الهندسة'
        else:
            college = _safe_str(data.get('college')) or 'كلية الهندسة'
        if not name or not raw_year or not raw_term:
            return jsonify({"status": "error", "message": "اسم المادة والسنة والترم حقول مطلوبة"}), 400
        # السنة والترم يُطبَّعان إلى القيم القياسية (رقم أو مختصر أو كامل)؛
        # قيمة لا تُطابق أي صيغة مقبولة تُرفض بدل إنشاء مادة مخفية عن الموقع.
        year = _normalize_year(raw_year)
        if not year:
            return jsonify({"status": "error", "message": "السنة الدراسية غير صالحة — القيم المقبولة: " + _YEAR_VALUES_TEXT}), 400
        term = _normalize_term(raw_term)
        if not term:
            return jsonify({"status": "error", "message": "الفصل الدراسي غير صالح — القيم المقبولة: " + _TERM_VALUES_TEXT}), 400
        if not College.query.filter_by(name=college).first():
            db.session.add(College(name=college))
        if Subject.query.filter_by(college=college, year=year, term=term, name=name).first():
            return jsonify({"status": "error", "message": "هذه المادة موجودة بالفعل في نفس الكلية والسنة والترم"}), 400
        subj = Subject(name=name, college=college, year=year, term=term, created_by=user.id)
        db.session.add(subj)
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إضافة المادة بنجاح", "subject": subj.to_dict()})
    except Exception as e:
        app.logger.exception("فشل إضافة مادة")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/edit-subject/<int:subject_id>', methods=['POST'])
@admin_or_supervisor_required
def edit_subject(subject_id):
    try:
        user = get_current_user()
        subj = Subject.query.get_or_404(subject_id)
        if user.role == 'specialization_supervisor':
            return jsonify({"status": "error", "message": "مشرف التخصص لا يمكنه إعادة تسمية المواد"}), 403
        if user.role == 'college_supervisor' and (user.supervisor_college or '') != (subj.college or ''):
            return jsonify({"status": "error", "message": "غير مصرح لك بتعديل مادة خارج كليتك"}), 403
        data = request.get_json(silent=True) or request.form or {}
        new_name = _safe_str(data.get('name'))
        if not new_name:
            return jsonify({"status": "error", "message": "اسم المادة مطلوب"}), 400
        if new_name == subj.name:
            return jsonify({"status": "success", "message": "تم تحديث المادة بنجاح", "subject": subj.to_dict()})
        dup = Subject.query.filter_by(college=subj.college, year=subj.year, term=subj.term, name=new_name).first()
        if dup:
            return jsonify({"status": "error", "message": "يوجد مادة بهذا الاسم بالفعل في نفس الكلية والسنة والترم"}), 400
        subj.name = new_name
        # إعادة التسمية تحدّث كل الصفوف المرتبطة دفعة واحدة (دروس + أسئلة)
        CustomLesson.query.filter_by(subject_id=subj.id).update({"subject_name": new_name}, synchronize_session=False)
        QuizQuestion.query.filter_by(subject_id=subj.id).update({"subject_name": new_name}, synchronize_session=False)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تحديث المادة بنجاح", "subject": subj.to_dict()})
    except Exception as e:
        app.logger.exception("فشل إعادة تسمية مادة: subject_id=%s", subject_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/remove-subject', methods=['POST'])
@admin_required
def remove_subject():
    try:
        data = request.get_json(silent=True) or request.form or {}
        subject_id = data.get('id')
        subj = None
        if subject_id:
            try:
                subj = Subject.query.get(int(subject_id))
            except (TypeError, ValueError):
                subj = None
        if not subj:
            return jsonify({"status": "error", "message": "المادة غير موجودة"}), 404
        if _subject_has_lessons(subj):
            _titles = _subject_lesson_titles(subj, 3)
            _extra = (' (مثال: ' + '، '.join(_titles) + ')') if _titles else ''
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة فيها دروس — انقل الدروس أو احذفها أولاً" + _extra}), 400
        if _subject_has_questions(subj):
            _nq = _subject_question_count(subj)
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة فيها أسئلة اختبار — احذف الأسئلة أولاً (عددها: %d)" % _nq}), 400
        if Student.query.filter(Student.role == 'specialization_supervisor', Student.supervisor_subject_id == subj.id).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة لديها مشرف تخصص — أزل المشرف أولاً"}), 400
        if Student.query.filter(Student.role == 'specialization_supervisor', Student.supervisor_subject_id == None, Student.supervisor_college == subj.college, Student.supervisor_specialization == subj.name).first():
            return jsonify({"status": "error", "message": "لا يمكن حذف مادة لديها مشرف تخصص — أزل المشرف أولاً"}), 400
        db.session.delete(subj)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف المادة بنجاح"})
    except Exception as e:
        app.logger.exception("فشل حذف مادة")
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/quiz-questions', methods=['GET'])
@admin_or_supervisor_required
def get_quiz_questions():
    """قائمة أسئلة الاختبار المستوردة (لإدارتها وحذفها): المشرف الأعلى يرى
    الكل، ومشرف الكلية يرى كليته فقط، ومشرف التخصص يرى مادته فقط.
    ملاحظة: الاسم مختلف عن /admin/questions الموجود (استفسارات الطلاب)."""
    try:
        user = get_current_user()
        subject_id = request.args.get('subject_id', type=int)
        query = QuizQuestion.query
        if subject_id:
            query = query.filter(QuizQuestion.subject_id == subject_id)
        query = _scope_query(query, user, QuizQuestion)
        qs = query.order_by(QuizQuestion.subject_name, QuizQuestion.chapter_name, QuizQuestion.id).all()
        return jsonify([q.to_dict() for q in qs])
    except Exception:
        app.logger.exception("فشل جلب أسئلة الاختبار")
        db.session.rollback()
        return jsonify([])

@app.route('/admin/delete-quiz-question/<int:question_id>', methods=['POST'])
@admin_required
def delete_quiz_question(question_id):
    """حذف سؤال اختبار معتمد — المشرف الأعلى فقط (الحذف النهائي له وحده).
    بدونه تبقى المادة التي فيها أسئلة مستوردة غير قابلة للحذف من الواجهة."""
    try:
        q = QuizQuestion.query.get(question_id)
        if not q:
            return jsonify({"status": "error", "message": "السؤال غير موجود"}), 404
        db.session.delete(q)
        db.session.commit()
        return jsonify({"status": "success", "message": "تم حذف السؤال بنجاح"})
    except Exception as e:
        app.logger.exception("فشل حذف سؤال اختبار: question_id=%s", question_id)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/supervisors', methods=['GET'])
@admin_required
def get_supervisors():
    try:
        sups = Student.query.filter(Student.role.in_(list(SUPERVISOR_ROLES))).order_by(Student.created_at.desc()).all()
        return jsonify([s.to_dict() for s in sups])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/add-supervisor', methods=['POST'])
@admin_required
def add_supervisor():
    username = role = college = specialization = ""
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = _safe_str(data.get('username'))
        role = _safe_str(data.get('role'))
        college = _safe_str(data.get('college'))
        specialization = _safe_str(data.get('specialization'))
        if not username or role not in SUPERVISOR_ROLES:
            return jsonify({"status": "error", "message": "اسم المستخدم والدور حقول مطلوبة"}), 400
        if not college:
            return jsonify({"status": "error", "message": "الكلية مطلوبة"}), 400
        if role == 'specialization_supervisor' and not specialization:
            return jsonify({"status": "error", "message": "التخصص مطلوب لمشرف التخصص"}), 400
        if not College.query.filter_by(name=college).first():
            return jsonify({"status": "error", "message": "الكلية غير موجودة — أضفها أولاً"}), 400
        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "لا يوجد حساب بهذا الاسم"}), 404
        if student.role == 'admin':
            return jsonify({"status": "error", "message": "هذا الحساب مشرف أعلى بالفعل"}), 400
        subject = None
        if role == 'specialization_supervisor':
            subject_id = data.get('subject_id')
            if subject_id:
                try:
                    subject = Subject.query.get(int(subject_id))
                except (TypeError, ValueError):
                    subject = None
                if not subject:
                    return jsonify({"status": "error", "message": "المادة المختارة غير موجودة"}), 400
                if subject.college != college:
                    return jsonify({"status": "error", "message": "المادة المختارة ليست في الكلية المحددة"}), 400
                specialization = subject.name
                college = subject.college
            elif specialization:
                # توافق: حلّ الاسم إلى مادة محددة إن وُجدت مطابقة واحدة
                matches = Subject.query.filter_by(college=college, name=specialization).all()
                if len(matches) == 1:
                    subject = matches[0]
        student.role = role
        student.supervisor_college = college
        student.supervisor_specialization = specialization if role == 'specialization_supervisor' else None
        student.supervisor_subject_id = subject.id if (role == 'specialization_supervisor' and subject) else None
        db.session.commit()
        return jsonify({"status": "success", "message": "تم تعيين المشرف بنجاح", "supervisor": student.to_dict()})
    except Exception as e:
        app.logger.exception("فشل تعيين مشرف: username=%s role=%s college=%s", username, role, college)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/admin/remove-supervisor', methods=['POST'])
@admin_required
def remove_supervisor():
    username = ""
    try:
        data = request.get_json(silent=True) or request.form or {}
        username = _safe_str(data.get('username'))
        student = Student.query.filter_by(username=username).first()
        if not student:
            return jsonify({"status": "error", "message": "لا يوجد حساب بهذا الاسم"}), 404
        student.role = 'student'
        student.supervisor_college = None
        student.supervisor_specialization = None
        student.supervisor_subject_id = None
        db.session.commit()
        return jsonify({"status": "success", "message": "تمت إزالة صلاحية الإشراف"})
    except Exception as e:
        app.logger.exception("فشل إزالة مشرف: username=%s", username)
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
        # ترتيب ثابت حسب id: بدون ORDER BY كان Postgres يعيد الصفوف بترتيب غير
        # مضمون يتغير بين الطلبات، فتظهر أسئلة الاختبار بترتيب مختلف في كل محاولة.
        college_name = request.args.get('college')
        query = QuizQuestion.query.order_by(QuizQuestion.id)
        if chapter_name:
            query = query.filter_by(chapter_name=chapter_name)
        if college_name:
            query = query.filter_by(college=college_name)
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

    # مادة مختارة للاستيراد (Flow B): كل الصفوف تُفرض على هذه المادة،
    # وتُتجاهل أعمدة subject/college/year/term الخاصة بالملف (Q8).
    # النطاق يُفحص مرة واحدة هنا بدل فحص كل صف.
    subject = None
    _import_subject_id = request.form.get('subject_id')
    if _import_subject_id not in (None, '', 0, '0'):
        try:
            subject = Subject.query.get(int(_import_subject_id))
        except (TypeError, ValueError):
            subject = None
        if not subject:
            return jsonify({"status": "error", "message": "المادة غير موجودة — اختر المادة أولاً"}), 400
        if user.role in SUPERVISOR_ROLES and not _in_scope(user, subject.college, subject.name, subject.id):
            return jsonify({"status": "error", "message": "غير مصرح لك بالاستيراد إلى مادة خارج نطاقك"}), 403

    try:
        stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
        reader = csv.DictReader(stream)
        count = 0
        skipped = 0
        skipped_invalid = 0
        for row in reader:
            qt = (row.get('question_text') or '').strip()
            oa = (row.get('option_a') or '').strip()
            ob = (row.get('option_b') or '').strip()
            oc = (row.get('option_c') or '').strip()
            od = (row.get('option_d') or '').strip()
            oe = (row.get('option_e') or '').strip()
            ca = (row.get('correct_answer') or '').strip()
            # ملفات CSV القديمة كانت تضع كلمة "اختيار" في question_text والسؤال
            # الحقيقي في option_a؛ نعيد ترتيب الحقول عند الاستيراد حتى لا تتكرر
            # المشكلة: نص السؤال في question_text والإجابات في option_a..option_e.
            if (_is_placeholder_question_title(qt) or qt == "") and oa:
                # حرف/رقم الإجابة الصحيحة كان يشير إلى أعمدة الملف الأصلية
                # (option_a = نص السؤال)، فنحوّله إلى نص الإجابة قبل الإزاحة.
                # المؤشر 0 يشير إلى نص السؤال نفسه فلا يُحلّ (لا إجابة هناك).
                orig_opts = [o for o in (oa, ob, oc, od, oe) if o]
                if len(ca) == 1 and ca.upper() in "ABCDE":
                    _idx = "ABCDE".index(ca.upper())
                    if 0 < _idx < len(orig_opts):
                        ca = orig_opts[_idx]
                elif len(ca) == 1 and ca in "12345":
                    _idx = int(ca) - 1
                    if 0 < _idx < len(orig_opts):
                        ca = orig_opts[_idx]
                qt, oa, ob, oc, od, oe = oa, ob, oc, od, oe, ""
            if subject is not None:
                _row_college = subject.college
                _row_subject = subject.name
                _row_year = subject.year
                _row_term = subject.term
                _row_subject_id = subject.id
            else:
                _row_college = (row.get('college') or '').strip() or 'كلية الهندسة'
                _row_subject = (row.get('subject_name') or 'عام').strip()
                # سنة/ترم الملف تُطبَّعان إلى القيم القياسية؛ صف بسنة أو ترم
                # غير صالحين يُتخطى بدل إنشاء سؤال مرتبط بمادة مخفية.
                _row_year = _normalize_year((row.get('year') or '').strip() or 'السنة الأولى')
                _row_term = _normalize_term((row.get('term') or '').strip() or 'الترم الأول')
                if not _row_year or not _row_term:
                    skipped_invalid += 1
                    continue
                if user.role in SUPERVISOR_ROLES and not _in_scope(user, _row_college, _row_subject):
                    skipped += 1
                    continue
                # الاستيراد بلا مادة مختارة ينشئ سجل المادة تلقائياً (get-or-create)
                # حتى تظهر في قوائم الإدارة («المواد»، محدد الاستيراد، مرشح
                # الأسئلة) وتقبل إعادة التسمية والحذف؛ بلا ذلك تبقى الأسئلة
                # بلا subject_id وتظهر المادة في الصفحة الرئيسية فقط عبر
                # تجميع النص الاحتياطي. النطاق فُحص أعلاه قبل الوصول هنا،
                # فمشرف التخصص لا ينشئ إلا مادته، ومشرف الكلية إلا في كليته.
                _matched = Subject.query.filter_by(college=_row_college, year=_row_year, term=_row_term, name=_row_subject).first()
                if _matched is None:
                    if not College.query.filter_by(name=_row_college).first():
                        db.session.add(College(name=_row_college))
                    _matched = Subject(name=_row_subject, college=_row_college, year=_row_year, term=_row_term, created_by=user.id)
                    db.session.add(_matched)
                    db.session.flush()  # يمنح الصف id قبل ربط الأسئلة به
                _row_subject_id = _matched.id
            q = QuizQuestion(
                year=_row_year,
                term=_row_term,
                college=_row_college,
                subject_name=_row_subject,
                subject_id=_row_subject_id,
                chapter_name=row.get('chapter_name', 'عام'),
                question_text=qt,
                option_a=oa,
                option_b=ob,
                option_c=oc,
                option_d=od,
                option_e=oe,
                correct_answer=ca,
                explanation=row.get('explanation', '').strip(),
                status=status,
                created_by=user.id
            )
            if q.question_text and q.correct_answer:
                db.session.add(q)
                count += 1
        db.session.commit()
        _msg = f"تم استيراد {count} سؤال بنجاح كـ ({status})"
        if skipped:
            _msg += f" — تم تخطي {skipped} سؤالاً خارج نطاقك"
        if skipped_invalid:
            _msg += f" — تم تخطي {skipped_invalid} سؤالاً بسبب سنة أو ترم غير صالحين (المقبول: {_YEAR_VALUES_TEXT} / {_TERM_VALUES_TEXT})"
        return jsonify({"status": "success", "message": _msg})
    except Exception as e:
        app.logger.exception("فشل استيراد الأسئلة")
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
        user = get_current_user()
        qs = LessonQuestion.query.filter_by(is_answered=False).order_by(LessonQuestion.created_at.desc()).all()
        if user and user.role in SUPERVISOR_ROLES:
            qs = [q for q in qs if _question_in_scope(q, user)]
        return jsonify([q.to_dict() for q in qs])
    except Exception:
        db.session.rollback()
        return jsonify([])

@app.route('/admin/reply/<int:question_id>', methods=['POST'])
@can_manage_content
def reply_question(question_id):
    try:
        user = get_current_user()
        q = LessonQuestion.query.get_or_404(question_id)
        if user.role in SUPERVISOR_ROLES and not _question_in_scope(q, user):
            return jsonify({"status": "error", "message": "غير مصرح لك بالرد على سؤال خارج نطاقك"}), 403
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
                app.logger.exception("generate_plan: failed to save generated tasks")
                return jsonify({"error": f"Error saving tasks: {str(e)}"}), 500

        return jsonify({"status": "success", "plan": plan_data})

    except Exception as e:
        app.logger.exception("generate_plan: failed")
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
        app.logger.exception("reschedule_plan: failed")
        return jsonify({"error": f"Error rescheduling plan: {str(e)}"}), 500

@app.route('/api/tasks/complete/<int:task_id>', methods=['POST'])
@student_required
def complete_task(task_id):
    try:
        student_id = session['student_id']
        # ids outside Postgres INTEGER range (e.g. a frontend Date.now()
        # temp id) can never be a real row — answer 404 JSON instead of
        # letting the lookup raise 22003.
        if not (-(2 ** 31) <= task_id <= 2 ** 31 - 1):
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
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
        app.logger.exception("complete_task: failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks', methods=['GET', 'POST', 'DELETE'])
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

        if request.method == 'DELETE':
            # Bulk clear-all: delete every task of this student in one
            # transaction. The old frontend deleted tasks one by one
            # (24+ sequential DELETEs, ~75s on the free tier) with no
            # visible progress, so users re-clicked or reloaded mid-loop
            # and tasks survived; overlapping loops also raced each other
            # (same id DELETE'd 2-3 times, 200 then 404).
            deleted = UnifiedTask.query.filter_by(student_id=student_id).delete(synchronize_session=False)
            db.session.commit()
            return jsonify({"status": "success", "deleted": deleted})

        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        planned_date = data.get('planned_date')
        source = data.get('source', 'manual')
        priority = data.get('priority', 2)
        lesson_id = data.get('lesson_id')
        description = data.get('description', '')
        allocated_hours = data.get('allocated_hours', 1.0)
        is_wird = data.get('is_wird', False)

        # Idempotency guard (dedupe by unique id): the planner re-syncs
        # unsynced local tasks; if the task id already exists for this
        # student, return the existing row instead of creating a duplicate.
        # NOTE: the frontend creates new tasks with a local temp id of
        # Date.now() (13 digits, e.g. 1795367806512) which is far outside
        # Postgres INTEGER range (max 2147483647). Binding it as INTEGER
        # raises 22003, so the task was never saved and every later PUT /
        # DELETE / complete on that id failed too. An out-of-range id can
        # never be a real row: skip the lookup and create the task, and
        # the frontend swaps in the real server id from the response.
        existing_id = data.get('id')
        try:
            existing_id = int(existing_id) if existing_id is not None else None
        except (TypeError, ValueError):
            existing_id = None
        if existing_id and -(2 ** 31) <= existing_id <= 2 ** 31 - 1:
            existing = UnifiedTask.query.filter_by(id=existing_id, student_id=student_id).first()
            if existing:
                return jsonify({"status": "success", "task": existing.to_dict()})

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
        app.logger.exception("manage_unified_tasks: failed")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/tasks/<int:task_id>', methods=['PUT', 'DELETE'])
@student_required
def handle_single_task(task_id):
    student_id = session['student_id']
    # ids outside Postgres INTEGER range (e.g. a frontend Date.now()
    # temp id) can never be a real row — answer 404 JSON instead of
    # letting the lookup raise 22003 as an uncaught HTML 500, and keep
    # the lookup inside the try so any DB error returns JSON with a
    # logged traceback instead of a bare failure page.
    if not (-(2 ** 31) <= task_id <= 2 ** 31 - 1):
        return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404

    try:
        task = UnifiedTask.query.get(task_id)
        if not task:
            return jsonify({"status": "error", "message": "المهمة غير موجودة"}), 404
        if task.student_id != student_id:
            return jsonify({"status": "error", "message": "غير مصرح لك"}), 403

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
        app.logger.exception("handle_single_task: failed")
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
                "subject_id": l.subject_id,
                "chapter_name": l.chapter_name,
                "content_type": l.content_type,
                "year": l.year,
                "term": l.term,
                "college": l.college or "كلية الهندسة"
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

        last_study_day = exam_date_obj - timedelta(days=1)
        prompt = f"""
        أنت مخطط أكاديمي ذكي. وزع الدروس التالية على {delta} يوماً باعتدال وبدون إرهاق (1-2 درس يومياً بحد أقصى ساعة ونصف يومياً).
        تاريخ اليوم: {today.isoformat()}، وتاريخ الاختبار: {exam_date_obj.isoformat()}.
        أيام المذاكرة هي من {today.isoformat()} إلى {last_study_day.isoformat()} فقط — يجب أن يكون حقل "day" في كل يوم أحد هذه التواريخ، ولا تستخدم أي تاريخ آخر.
        الدروس: {json.dumps(lessons_info, ensure_ascii=False)}
        أخرج فقط JSON: {{"plan": [{{"day": "{today.isoformat()}", "tasks": [{{"lesson_id": 1, "title": "مقدمة", "priority": "متوسط", "allocated_hours": 1.0}}]}}]}}
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

        # Never trust the model's dates: derive them deterministically from
        # the exam date. Study days run from today through the day before
        # the exam (exactly `delta` days), so tasks can never land in the
        # past or on/after the exam. The model's day order is kept; only
        # the calendar dates are replaced (and written back into the
        # returned plan so the response matches what was stored).
        study_days = [today + timedelta(days=i) for i in range(delta)]
        plan_days = plan_data.get('plan') or []
        if not isinstance(plan_days, list):
            plan_days = []
        for day_index, day_obj in enumerate(plan_days):
            day_date = study_days[min(day_index, len(study_days) - 1)]
            day_obj['day'] = day_date.isoformat()

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
        app.logger.exception("generate_exam_plan: failed")
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
        # تُحتسب فقط الدروس التي بدأها الطالب فعلاً (progress_percent > 0):
        # صفوف 0% كانت تُنشأ عند حفظ حالة أي درس آخر في الصفحة ولا تعني مهمة
        # بدأها الطالب، فكان تضمينها يضخّم «إجمالي المهام» ويهبط بنسبة الإنجاز.
        total_lessons = Progress.query.filter(Progress.student_id == student_id, Progress.progress_percent > 0).count()
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
        app.logger.exception("get_profile: failed")
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
            # لا ننشئ صفاً لدرس لم يبدأه الطالب (0%): صفوف 0% كانت تُحتسب
            # ضمن «إجمالي المهام» في الملف الشخصي فتضخّم المقام دون إنجاز.
            if progress_percent == 0:
                return jsonify({"status": "success"})
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



